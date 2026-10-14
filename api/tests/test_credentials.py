"""凭据服务测试（FR-03）。

最关键的一条：**明文不得以任何形式落到磁盘上**。
"""

import pytest

from app.core.errors import SecretBackendUnavailable, SecretNotFound
from app.services.credentials import CredentialService, LocalSecretProvider

MASTER_KEY = "test-master-key-32-bytes-long-ok"


@pytest.fixture()
def service(tmp_path) -> CredentialService:
    return CredentialService(LocalSecretProvider(tmp_path / "secrets.enc", MASTER_KEY))


def test_issue_and_resolve_roundtrip(service: CredentialService):
    payload = {"username": "etl_reader", "password": "s3cr3t!"}
    ref = service.issue_ref(payload, name="ds-1")

    assert ref == "local://ds-1"
    assert service.resolve(ref) == payload


def test_stored_file_contains_no_plaintext(service: CredentialService, tmp_path):
    service.issue_ref({"username": "reader", "password": "super-secret-value"}, name="ds-plain")

    raw = (tmp_path / "secrets.enc").read_bytes()
    assert b"super-secret-value" not in raw
    assert b"password" not in raw
    assert b"reader" not in raw


def test_auto_generated_ref_when_name_omitted(service: CredentialService):
    ref = service.issue_ref({"token": "t"})

    assert ref.startswith("local://")
    assert len(ref.removeprefix("local://")) == 36  # 标准 UUID 文本长度


def test_revoke_removes_secret(service: CredentialService):
    ref = service.issue_ref({"token": "t"}, name="ds-revoke")
    service.revoke(ref)

    with pytest.raises(SecretNotFound):
        service.resolve(ref)
    with pytest.raises(SecretNotFound):
        service.revoke(ref)  # 重复撤销同样报不存在


def test_unknown_ref_raises(service: CredentialService):
    with pytest.raises(SecretNotFound):
        service.resolve("local://does-not-exist")


def test_rotation_overwrites_same_name(service: CredentialService):
    """同名写入即轮转 —— 数据源改口令时不应遗留旧凭据。"""
    service.issue_ref({"password": "old"}, name="ds-rot")
    ref = service.issue_ref({"password": "new"}, name="ds-rot")

    assert service.resolve(ref) == {"password": "new"}


def test_empty_payload_is_rejected(service: CredentialService):
    with pytest.raises(ValueError):
        service.issue_ref({})


def test_wrong_master_key_cannot_decrypt(tmp_path):
    """更换 APP_SECRET_KEY 后旧凭据库不可读 —— 必须显式报错而非静默返回空。"""
    path = tmp_path / "secrets.enc"
    CredentialService(LocalSecretProvider(path, "key-AAAAAAAAAAAAAAAAAAAAAAA")).issue_ref(
        {"password": "x"}, name="ds"
    )

    stranger = CredentialService(
        LocalSecretProvider(path, "key-BBBBBBBBBBBBBBBBBBBBBBB")
    )
    with pytest.raises(SecretBackendUnavailable):
        stranger.resolve("local://ds")


def test_fingerprint_is_stable_and_leaks_nothing():
    """指纹用于审计比对轮转，必须可重复且不可逆。"""
    payload = {"username": "reader", "password": "abc"}

    first = CredentialService.fingerprint(payload)
    assert first == CredentialService.fingerprint({"password": "abc", "username": "reader"})
    assert len(first) == 16
    assert "abc" not in first

    assert first != CredentialService.fingerprint({"username": "reader", "password": "abd"})
