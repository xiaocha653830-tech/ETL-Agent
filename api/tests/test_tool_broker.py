"""Tool Broker 测试（FR-26 / FR-27，M7 T-10）。

覆盖：未注册工具被拒、绑定不匹配、单次消费（防重放）、副作用调用留痕、
只读工具不留痕、处理器异常仍消费令牌并记录失败。
"""

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.errors import (
    CapabilityBindingMismatch,
    CapabilityConsumed,
    ErrorCode,
    ToolNotRegistered,
)
from app.core.keys import SigningKey, load_or_create_signing_key
from app.models.capability import Capability  # noqa: F401 —— 注册建表
from app.services.capability import issue_capability
from app.services.credentials import CredentialService, LocalSecretProvider
from app.services.tool_broker import InMemoryInvocationSink, ToolBroker

BINDING = {
    "tool": "etl.run",
    "subject": "user:maker",
    "environment": "staging",
    "artifact": "version:abc123",
}


@pytest.fixture()
def signing_key(tmp_path) -> SigningKey:
    provider = LocalSecretProvider(tmp_path / "broker-keys.enc", "unit-test-master-key")
    return load_or_create_signing_key(CredentialService(provider))


@pytest.fixture()
def db(session_factory: sessionmaker[Session]):
    with session_factory() as session:
        yield session


@pytest.fixture()
def sink() -> InMemoryInvocationSink:
    return InMemoryInvocationSink()


@pytest.fixture()
def broker(sink, signing_key) -> ToolBroker:
    return ToolBroker(sink=sink, signing_key=signing_key)


@pytest.fixture()
def token(db, signing_key) -> str:
    return issue_capability(db, signing_key=signing_key, **BINDING).token


def _token_for(db, signing_key, **overrides) -> str:
    return issue_capability(db, signing_key=signing_key, **{**BINDING, **overrides}).token


# --------------------------------------------------------------------------
# 注册与准入
# --------------------------------------------------------------------------


def test_unregistered_tool_is_rejected(broker, db, token):
    with pytest.raises(ToolNotRegistered) as exc:
        broker.execute("never.registered", {}, token, db=db)
    assert exc.value.code == ErrorCode.BIZ_TOOL_NOT_REGISTERED


def test_unregistered_tool_does_not_consume_token(broker, db, signing_key):
    issued = issue_capability(db, signing_key=signing_key, **BINDING)
    with pytest.raises(ToolNotRegistered):
        broker.execute("never.registered", {}, issued.token, db=db)

    capability = db.get(Capability, issued.capability_id)
    assert capability.consumed is False


def test_tool_binding_mismatch(broker, db, signing_key):
    broker.register_tool("other.tool", lambda: "ok")
    token = _token_for(db, signing_key)  # 绑定 etl.run

    with pytest.raises(CapabilityBindingMismatch) as exc:
        broker.execute("other.tool", {}, token, db=db)
    assert exc.value.code == ErrorCode.AUTH_CAPABILITY_BINDING_MISMATCH


def test_duplicate_registration_is_refused(broker):
    broker.register_tool("etl.run", lambda: 1)
    with pytest.raises(ValueError):
        broker.register_tool("etl.run", lambda: 2)


# --------------------------------------------------------------------------
# 执行与留痕
# --------------------------------------------------------------------------


def test_side_effect_invocation_is_recorded(broker, sink, db, signing_key):
    issued = issue_capability(db, signing_key=signing_key, **BINDING)
    broker.register_tool("etl.run", lambda **kwargs: {"rows": kwargs.get("rows", 0)})

    result = broker.execute("etl.run", {"rows": 100}, issued.token, db=db)

    assert result == {"rows": 100}
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.capability_id == issued.capability_id
    assert record.tool == "etl.run"
    assert record.subject == "user:maker"
    assert record.environment == "staging"
    assert record.artifact == "version:abc123"
    assert record.side_effect is True
    assert record.succeeded is True
    assert len(record.arguments_digest) == 64


def test_read_only_tool_is_not_recorded(broker, sink, db, signing_key):
    broker.register_tool("etl.preview", lambda: "dry-run", side_effect=False)
    token = _token_for(db, signing_key, tool="etl.preview")

    assert broker.execute("etl.preview", {}, token, db=db) == "dry-run"
    assert sink.records == []


def test_replay_through_broker_is_rejected(broker, db, signing_key):
    broker.register_tool("etl.run", lambda: "done")
    token = _token_for(db, signing_key)

    assert broker.execute("etl.run", {}, token, db=db) == "done"
    with pytest.raises(CapabilityConsumed) as exc:
        broker.execute("etl.run", {}, token, db=db)
    assert exc.value.code == ErrorCode.AUTH_CAPABILITY_CONSUMED


def test_handler_failure_is_recorded_and_token_consumed(broker, sink, db, signing_key):
    def _boom(**kwargs):
        raise RuntimeError("下游失败")

    broker.register_tool("etl.run", _boom)
    token = _token_for(db, signing_key)

    with pytest.raises(RuntimeError):
        broker.execute("etl.run", {"x": 1}, token, db=db)

    assert len(sink.records) == 1
    assert sink.records[0].succeeded is False

    # 令牌已被消费：失败也不允许重放
    with pytest.raises(CapabilityConsumed):
        broker.execute("etl.run", {"x": 1}, token, db=db)
