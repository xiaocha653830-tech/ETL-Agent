"""Ed25519 签名密钥的加载与内存缓存（SPEC §11 Q3 定案）。

Q3 定案原文：私钥存 **凭据后端**（Vault KV v2 / 开发为本地加密库），启动加载到内存；
公钥分发给数据面独立验签；预留 ``kid``，15 天内单密钥不轮转。

本模块**复用** ``app.services.credentials`` 的 ``SecretProvider`` 体系——私钥与业务
凭据走同一套治理，避免密钥文件散落。开发环境（``secret_backend == "local"``）零配置
即可工作，不强制依赖 Vault。

**密钥只生成一次**：首次加载时若后端无密钥则以固定名称写入，之后每次加载读回同一把
私钥，因此进程重启不会更换公钥（数据面的缓存公钥不会失效）。
"""

import base64
import logging
import threading

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from app.core.config import settings
from app.core.errors import SecretNotFound, SigningKeyUnavailable
from app.services.credentials import CredentialService, get_credential_service

logger = logging.getLogger(__name__)

# 后端中的固定条目名 —— 改名等于换密钥，务必谨慎
_KEY_NAME = "capability-signing-key-v1"
# 当前默认 kid（单密钥期）；轮转期为兼容新旧公钥会同时分发多把
_DEFAULT_KID = "cap-1"
_ALGORITHM = "Ed25519"


class SigningKey:
    """内存中的签名密钥对。

    **私钥永不外泄**；``public_key_*`` 系列可安全分发给数据面独立验签。
    """

    __slots__ = ("_kid", "_private_key")

    def __init__(self, kid: str, private_key: Ed25519PrivateKey) -> None:
        self._kid = kid
        self._private_key = private_key

    @property
    def kid(self) -> str:
        return self._kid

    @property
    def algorithm(self) -> str:
        return _ALGORITHM

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message)

    def verify(self, message: bytes, signature: bytes) -> None:
        """验签失败抛出 ``InvalidSignature``（由调用方转成业务错误）。"""
        self.public_key.verify(signature, message)

    def public_key_raw(self) -> bytes:
        """裸 32 字节公钥（数据面紧凑分发用）。"""
        return self.public_key.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def public_key_pem(self) -> bytes:
        """PEM 公钥（含算法标识，便于跨语言分发）。"""
        return self.public_key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


# --------------------------------------------------------------------------
# 持久化：从凭据后端加载 / 首次生成
# --------------------------------------------------------------------------


def _secret_ref() -> str:
    """当前后端下密钥条目的引用。

    这里镜像了 ``credentials.py`` 两个 Provider 的引用构造规则：
    ``LocalSecretProvider`` 用 ``local://<name>``，``VaultSecretProvider`` 用
    ``vault://<mount>/<prefix>/<name>``（默认 prefix 为 ``datasources``）。
    若凭据后端的引用格式变更，此处需同步。
    """
    if settings.secret_backend == "vault":
        return f"vault://{settings.vault_mount_point}/datasources/{_KEY_NAME}"
    return f"local://{_KEY_NAME}"


def _decode_payload(secret: dict[str, str]) -> SigningKey:
    try:
        raw = base64.b64decode(secret["private_key"])
        private_key = Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:  # noqa: BLE001 —— 任何解析失败都归为密钥不可用
        raise SigningKeyUnavailable("签名私钥解析失败，凭据可能已损毁") from exc
    return SigningKey(secret.get("kid", _DEFAULT_KID), private_key)


def load_or_create_signing_key(service: CredentialService | None = None) -> SigningKey:
    """读取已有密钥；不存在则生成一把并写回后端。

    ``service`` 可注入（测试用隔离的本地库）；缺省使用进程级凭据服务单例。
    """
    service = service or get_credential_service()
    try:
        secret = service.resolve(_secret_ref())
    except SecretNotFound:
        secret = None

    if secret is None:
        private_key = Ed25519PrivateKey.generate()
        raw = private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        payload = {
            "private_key": base64.b64encode(raw).decode("ascii"),
            "kid": _DEFAULT_KID,
            "algorithm": _ALGORITHM,
        }
        # 用固定名称写入，保证下次加载命中同一条目
        service.issue_ref(payload, name=_KEY_NAME)
        logger.info("已生成新的能力令牌签名密钥（kid=%s）", _DEFAULT_KID)
        return SigningKey(_DEFAULT_KID, private_key)

    return _decode_payload(secret)


# --------------------------------------------------------------------------
# 进程级单例
# --------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached_key: SigningKey | None = None


def get_signing_key() -> SigningKey:
    """进程级单例 —— 启动加载一次，运行期复用内存中的私钥。"""
    global _cached_key
    if _cached_key is None:
        with _cache_lock:
            if _cached_key is None:
                _cached_key = load_or_create_signing_key()
    return _cached_key


def reset_signing_key_cache() -> None:
    """清空单例缓存（仅测试与密钥轮转场景使用）。"""
    global _cached_key
    with _cache_lock:
        _cached_key = None


# --------------------------------------------------------------------------
# 公钥分发
# --------------------------------------------------------------------------


def export_public_key_pem() -> bytes:
    """导出当前公钥（PEM），供数据面独立验签（Q3）。"""
    return get_signing_key().public_key_pem()


def current_kid() -> str:
    """当前签名密钥标识（Q3 预留，供令牌头与轮转期选择公钥）。"""
    return get_signing_key().kid
