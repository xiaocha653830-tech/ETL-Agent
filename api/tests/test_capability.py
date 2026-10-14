"""能力令牌测试（FR-26 / FR-27，M7 T-10）。

覆盖：四维绑定逐个不匹配、过期、篡改签名、格式非法、单次消费（顺序 + 并发）、
密钥持久化与开发环境零配置。全部不依赖 Vault / Redis / PostgreSQL。
"""

import base64
import json
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core import keys
from app.core.config import settings
from app.core.errors import (
    CapabilityBindingMismatch,
    CapabilityConsumed,
    CapabilityExpired,
    CapabilityMalformed,
    CapabilityNotFound,
    CapabilitySignatureInvalid,
    ErrorCode,
)
from app.core.keys import SigningKey, load_or_create_signing_key
from app.db.base import Base
from app.models.capability import CAPABILITY_TTL_SECONDS, Capability  # noqa: F401 —— 注册建表
from app.schemas.capability import BindingContext, CapabilityClaims
from app.services import credentials
from app.services.capability import (
    consume_capability,
    decode_claims,
    issue_capability,
    sign_claims,
    verify_capability,
    verify_claims,
)
from app.services.credentials import CredentialService, LocalSecretProvider

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

BASE_BINDING = {
    "tool": "etl.run",
    "subject": "user:maker",
    "environment": "staging",
    "artifact": "version:abc123",
}


@pytest.fixture()
def signing_key(tmp_path) -> SigningKey:
    """隔离的本地密钥库 —— 不触碰项目根目录的 .secrets.enc。"""
    provider = LocalSecretProvider(tmp_path / "unit-keys.enc", "unit-test-master-key")
    return load_or_create_signing_key(CredentialService(provider))


@pytest.fixture()
def db(session_factory: sessionmaker[Session]):
    with session_factory() as session:
        yield session


def _issue(db, signing_key, **overrides):
    params = {**BASE_BINDING, **overrides}
    return issue_capability(db, signing_key=signing_key, **params)


def _binding(**overrides) -> BindingContext:
    return BindingContext(**{**BASE_BINDING, **overrides})


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


# --------------------------------------------------------------------------
# 签发 - 校验往返
# --------------------------------------------------------------------------


def test_issue_then_verify_roundtrip(db, signing_key):
    issued = _issue(db, signing_key)

    claims = verify_claims(issued.token, _binding(), signing_key=signing_key)
    assert claims.tool == "etl.run"
    assert claims.exp - claims.iat == CAPABILITY_TTL_SECONDS

    capability = verify_capability(db, issued.token, _binding(), signing_key=signing_key)
    assert capability.id == issued.capability_id
    assert capability.consumed is False


def test_ttl_over_five_minutes_is_rejected(db, signing_key):
    with pytest.raises(ValueError):
        _issue(db, signing_key, ttl_seconds=CAPABILITY_TTL_SECONDS + 1)


# --------------------------------------------------------------------------
# 过期
# --------------------------------------------------------------------------


def test_expired_token_is_rejected(db, signing_key):
    now = int(time.time())
    claims = CapabilityClaims(
        jti=uuid.uuid4(),
        iat=now - 600,
        exp=now - 300,
        **BASE_BINDING,
    )
    token = sign_claims(claims, signing_key=signing_key)

    with pytest.raises(CapabilityExpired) as exc:
        verify_claims(token, _binding(), signing_key=signing_key)
    assert exc.value.code == ErrorCode.AUTH_CAPABILITY_EXPIRED

    # 控制面路径同样在过期处短路
    with pytest.raises(CapabilityExpired):
        verify_capability(db, token, _binding(), signing_key=signing_key)


# --------------------------------------------------------------------------
# 篡改 / 格式非法 / 未知记录
# --------------------------------------------------------------------------


def test_tampered_payload_fails_signature(db, signing_key):
    issued = _issue(db, signing_key)
    header_b64, payload_b64, signature_b64 = issued.token.split(".")

    payload = json.loads(_b64url_decode(payload_b64))
    payload["tool"] = "evil.tool"
    forged_payload = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    forged = f"{header_b64}.{forged_payload}.{signature_b64}"

    with pytest.raises(CapabilitySignatureInvalid) as exc:
        decode_claims(forged, signing_key=signing_key)
    assert exc.value.code == ErrorCode.AUTH_CAPABILITY_SIGNATURE_INVALID


def test_malformed_token_is_rejected(signing_key):
    with pytest.raises(CapabilityMalformed) as exc:
        decode_claims("not-a-valid-token", signing_key=signing_key)
    assert exc.value.code == ErrorCode.AUTH_CAPABILITY_MALFORMED


def test_unknown_signed_token_has_no_record(db, signing_key):
    """签名有效但库中无签发记录（非本系统签发）。"""
    now = int(time.time())
    claims = CapabilityClaims(
        jti=uuid.uuid4(), iat=now, exp=now + 60, **BASE_BINDING
    )
    token = sign_claims(claims, signing_key=signing_key)

    with pytest.raises(CapabilityNotFound) as exc:
        verify_capability(db, token, _binding(), signing_key=signing_key)
    assert exc.value.code == ErrorCode.BIZ_CAPABILITY_NOT_FOUND


# --------------------------------------------------------------------------
# 四维绑定逐个不匹配
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dimension", "override"),
    [
        ("tool", {"tool": "etl.other"}),
        ("subject", {"subject": "user:attacker"}),
        ("environment", {"environment": "production"}),
        ("artifact", {"artifact": "version:evil"}),
    ],
)
def test_binding_dimension_mismatch(db, signing_key, dimension, override):
    issued = _issue(db, signing_key)

    with pytest.raises(CapabilityBindingMismatch) as exc:
        verify_capability(db, issued.token, _binding(**override), signing_key=signing_key)

    assert exc.value.code == ErrorCode.AUTH_CAPABILITY_BINDING_MISMATCH
    assert exc.value.detail["dimension"] == [dimension]


# --------------------------------------------------------------------------
# 单次消费（FR-27）
# --------------------------------------------------------------------------


def test_sequential_replay_is_rejected(db, signing_key):
    issued = _issue(db, signing_key)

    first = verify_capability(db, issued.token, _binding(), consume=True, signing_key=signing_key)
    assert first.consumed is True

    with pytest.raises(CapabilityConsumed) as exc:
        verify_capability(db, issued.token, _binding(), consume=True, signing_key=signing_key)
    assert exc.value.code == ErrorCode.AUTH_CAPABILITY_CONSUMED


def test_consumed_flag_blocks_verify(db, signing_key):
    issued = _issue(db, signing_key)
    consume_capability(db, issued.capability_id)

    with pytest.raises(CapabilityConsumed):
        verify_capability(db, issued.token, _binding(), signing_key=signing_key)


def test_concurrent_consume_only_one_succeeds(tmp_path, signing_key):
    """并发消费：条件 UPDATE 保证仅一个成功，其余收到「已被消费」。"""
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'cap.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)

    with factory() as session:
        issued = _issue(session, signing_key)
    capability_id = issued.capability_id

    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _consume() -> None:
        session = factory()
        try:
            barrier.wait()
            try:
                consume_capability(session, capability_id)
                result = "consumed-ok"
            except CapabilityConsumed:
                result = "replay-rejected"
            with lock:
                outcomes.append(result)
        finally:
            session.close()

    threads = [threading.Thread(target=_consume) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    engine.dispose()

    assert outcomes.count("consumed-ok") == 1
    assert outcomes.count("replay-rejected") == workers - 1


# --------------------------------------------------------------------------
# 密钥持久化 / 零配置
# --------------------------------------------------------------------------


def test_signing_key_persists_across_reload(tmp_path):
    """同一密钥库重复加载必须得到相同公钥（进程重启不换密钥）。"""
    path = tmp_path / "keys.enc"
    first = load_or_create_signing_key(
        CredentialService(LocalSecretProvider(path, "master-key"))
    )
    second = load_or_create_signing_key(
        CredentialService(LocalSecretProvider(path, "master-key"))
    )

    assert first.public_key_raw() == second.public_key_raw()
    assert first.kid == second.kid


def test_public_key_is_exportable(signing_key):
    pem = signing_key.public_key_pem()
    assert pem.startswith(b"-----BEGIN PUBLIC KEY-----")
    assert len(signing_key.public_key_raw()) == 32


def test_dev_environment_works_with_zero_config(tmp_path, monkeypatch):
    """开发环境（local 后端）零配置可用：不依赖 Vault，且密钥稳定。"""
    monkeypatch.setattr(settings, "secret_backend", "local")
    monkeypatch.setattr(settings, "local_secret_file", str(tmp_path / "dev-keys.enc"))
    credentials.get_credential_service.cache_clear()
    keys.reset_signing_key_cache()
    try:
        first = keys.get_signing_key()
        keys.reset_signing_key_cache()
        second = keys.get_signing_key()

        assert first.public_key_raw() == second.public_key_raw()
        assert keys.export_public_key_pem().startswith(b"-----BEGIN PUBLIC KEY-----")
        assert keys.current_kid() == first.kid
    finally:
        keys.reset_signing_key_cache()
        credentials.get_credential_service.cache_clear()
