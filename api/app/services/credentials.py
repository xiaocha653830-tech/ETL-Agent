"""凭据服务（SPEC §3 M13，FR-03）。

**核心约束：任何接口、任何日志、任何响应体都不得出现明文凭据。**
业务侧只持有 ``credential_ref``（形如 ``vault://secret/datasources/<uuid>``），
真正需要口令时（如调用数据面适配器）才由本服务在内存中解析。

两种后端（``settings.secret_backend``）：

| 值 | 实现 | 适用 |
|----|------|------|
| ``local`` | Fernet 加密落盘，密钥由 ``APP_SECRET_KEY`` 派生 | 开发 / 演示（SPEC §4） |
| ``vault`` | Vault KV v2 | 生产（SPEC §11 Q3：私钥与凭据统一治理） |
"""

import base64
import hashlib
import json
import logging
import threading
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings
from app.core.errors import SecretBackendUnavailable, SecretNotFound

logger = logging.getLogger(__name__)

# 本地后端的密钥派生盐/上下文 —— 变更会导致既有凭据不可解密，改动需谨慎
_KDF_SALT = b"etl-agent-credentials-v1"
_KDF_INFO = b"local-secret-provider"


# --------------------------------------------------------------------------
# 后端抽象
# --------------------------------------------------------------------------


class SecretProvider(Protocol):
    """凭据后端协议 —— 三个方法即完整契约。"""

    def put(self, name: str, payload: dict[str, str]) -> str:
        """写入明文凭据，返回可安全传递的引用。"""
        ...

    def get(self, ref: str) -> dict[str, str]:
        """按引用取回明文凭据。仅在服务端内存中使用。"""
        ...

    def delete(self, ref: str) -> None:
        """撤销引用并清除底层密文。"""
        ...


def _derive_fernet_key(master_key: str) -> bytes:
    """从会话主密钥派生本地凭据库的加密密钥。"""
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        info=_KDF_INFO,
    ).derive(master_key.encode("utf-8"))
    return base64.urlsafe_b64encode(derived)


# --------------------------------------------------------------------------
# 本地后端：Fernet 加密落盘
# --------------------------------------------------------------------------


class LocalSecretProvider:
    """开发 / 演示用的本地凭据库。

    整个凭据表以 Fernet 加密后落盘，**文件内容是密文**，
    但请注意：这不是生产级密钥管理方案——加密密钥与会话主密钥同源，
    生产环境必须切换 ``SECRET_BACKEND=vault``。
    """

    def __init__(self, path: Path, master_key: str) -> None:
        self._path = path
        self._fernet = Fernet(_derive_fernet_key(master_key))
        self._lock = threading.Lock()

    # -- 内部读写 --

    def _load(self) -> dict[str, dict[str, str]]:
        if not self._path.exists():
            return {}
        try:
            plaintext = self._fernet.decrypt(self._path.read_bytes())
        except InvalidToken as exc:
            # 主密钥被更换、或文件被外部篡改
            raise SecretBackendUnavailable(
                "本地凭据库解密失败：APP_SECRET_KEY 可能已变更或文件已损坏"
            ) from exc
        return json.loads(plaintext.decode("utf-8"))

    def _store(self, data: dict[str, dict[str, str]]) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(self._fernet.encrypt(payload))

    # -- SecretProvider --

    def put(self, name: str, payload: dict[str, str]) -> str:
        ref = f"local://{name}"
        with self._lock:
            data = self._load()
            data[ref] = payload
            self._store(data)
        return ref

    def get(self, ref: str) -> dict[str, str]:
        with self._lock:
            data = self._load()
        secret = data.get(ref)
        if secret is None:
            raise SecretNotFound(f"凭据引用不存在：{ref}")
        return secret

    def delete(self, ref: str) -> None:
        with self._lock:
            data = self._load()
            if data.pop(ref, None) is None:
                raise SecretNotFound(f"凭据引用不存在：{ref}")
            self._store(data)


# --------------------------------------------------------------------------
# Vault 后端：KV v2
# --------------------------------------------------------------------------


class VaultSecretProvider:
    """Vault KV v2 后端（生产形态，SPEC §11 Q3）。

    路径约定：``<mount_point>/<prefix>/<name>``，
    ``prefix`` 由凭据类别决定（数据源凭据统一为 ``datasources``）。
    """

    def __init__(
        self, addr: str, token: str, mount_point: str, prefix: str = "datasources"
    ) -> None:
        import hvac  # 延迟导入：dev 环境未启用 Vault 时无需加载

        self._client = hvac.Client(url=addr, token=token)
        self._mount = mount_point
        self._prefix = prefix

    def _path_of(self, name: str) -> str:
        return f"{self._prefix}/{name}"

    def _name_of(self, ref: str) -> str:
        prefix = f"vault://{self._mount}/"
        if not ref.startswith(prefix):
            raise SecretNotFound(f"凭据引用格式不合法：{ref}")
        return ref[len(prefix) :]

    def _ensure_available(self) -> None:
        try:
            if not self._client.is_authenticated():
                raise SecretBackendUnavailable("Vault 认证失败，请检查 VAULT_TOKEN")
        except SecretBackendUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 —— 网络/连接类错误统一转 503
            raise SecretBackendUnavailable(f"Vault 不可达：{settings.vault_addr}") from exc

    def put(self, name: str, payload: dict[str, str]) -> str:
        self._ensure_available()
        path = self._path_of(name)
        try:
            self._client.secrets.kv.v2.create_secret(
                path=path, secret=payload, mount_point=self._mount
            )
        except Exception as exc:  # noqa: BLE001
            raise SecretBackendUnavailable("Vault 写入凭据失败") from exc
        return f"vault://{self._mount}/{path}"

    def get(self, ref: str) -> dict[str, str]:
        self._ensure_available()
        path = self._name_of(ref)
        try:
            response = self._client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self._mount, raise_on_deleted_version=True
            )
        except Exception as exc:  # noqa: BLE001
            raise SecretNotFound(f"凭据引用不存在：{ref}") from exc
        return dict(response["data"]["data"])

    def delete(self, ref: str) -> None:
        self._ensure_available()
        path = self._name_of(ref)
        try:
            # 删除全部版本与元数据，避免残留可恢复的旧口令
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=path, mount_point=self._mount
            )
        except Exception as exc:  # noqa: BLE001
            raise SecretNotFound(f"凭据引用不存在：{ref}") from exc


# --------------------------------------------------------------------------
# 面向业务的服务对象
# --------------------------------------------------------------------------


class CredentialService:
    """凭据服务对外接口。

    业务代码只应通过本类接触凭据，**不得**直接实例化具体 Provider。
    """

    def __init__(self, provider: SecretProvider) -> None:
        self._provider = provider

    def issue_ref(self, payload: dict[str, str], *, name: str | None = None) -> str:
        """存入明文凭据并签发引用。

        ``name`` 省略时自动生成 UUID；同一 ``name`` 重复写入会覆盖旧值
        （便于数据源凭据轮转）。
        """
        if not payload:
            raise ValueError("凭据内容不能为空")
        return self._provider.put(name or str(uuid.uuid4()), payload)

    def resolve(self, ref: str) -> dict[str, str]:
        return self._provider.get(ref)

    def revoke(self, ref: str) -> None:
        self._provider.delete(ref)

    @staticmethod
    def fingerprint(payload: dict[str, str]) -> str:
        """凭据指纹 —— 用于审计留痕，**不可逆**。

        PRD 第 7 章 P04/P07 需要展示「凭据已配置」而不暴露内容，
        用该指纹即可比对是否发生过轮转。
        """
        material = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------


def _build_provider() -> SecretProvider:
    backend: Any = settings.secret_backend
    if backend == "vault":
        return VaultSecretProvider(
            addr=settings.vault_addr,
            token=settings.vault_token,
            mount_point=settings.vault_mount_point,
        )
    if backend == "local":
        return LocalSecretProvider(Path(settings.local_secret_file), settings.app_secret_key)
    raise SecretBackendUnavailable(f"未知的凭据后端：{backend}")


@lru_cache
def get_credential_service() -> CredentialService:
    """进程级单例 —— 本地后端依赖它缓存加密器与文件锁。"""
    provider = _build_provider()
    logger.info("凭据后端已加载：%s", settings.secret_backend)
    return CredentialService(provider)
