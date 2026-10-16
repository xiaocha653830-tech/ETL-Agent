"""T-20 证据账本：哈希链写路径（FR-39）。

这里测的是「证据能不能被信任」的前半段——写入侧。篡改能否被**检出**在
``test_audit_verifier.py``，那才是这套机制存在的理由。

断言一律写在会话块**内**：``commit()`` 会让对象过期，出块之后再读属性会触发
重新加载，而那时会话已经关掉了。
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from app.core.config import settings
from app.core.keys import SigningKey
from app.db.types import as_utc
from app.models.audit import AUDIT_CHAIN_ID, GENESIS_HASH, AuditChainHead, AuditRecord
from app.services import audit
from app.services.audit import compute_entry_hash, parse_signature, record_audit


@pytest.fixture()
def signing_key(monkeypatch):
    """隔离的签名密钥。

    不禁这一个单例的话，测试会去碰凭据后端并在工作区留下 ``.secrets.enc``——
    单元测试不该有文件副作用。这个夹具只影响 ``audit`` 模块取密钥的入口。
    """
    key = SigningKey("test-key-1", Ed25519PrivateKey.generate())
    monkeypatch.setattr(audit, "get_signing_key", lambda: key)
    return key


def _chain(db) -> list[AuditRecord]:
    return list(db.scalars(select(AuditRecord).order_by(AuditRecord.seq)))


def _write(db, **overrides) -> AuditRecord:
    fields = {
        "event_type": "test.event",
        "actor": "11111111-1111-1111-1111-111111111111",
    }
    fields.update(overrides)
    return record_audit(db, **fields)


def test_the_first_record_links_to_the_genesis_hash(session_factory, signing_key):
    """首条的 prev_hash 是创世常量，不是空串。

    空串会让「首条的 prev_hash 被清空」与「正常的首条」变得不可区分；固定常量
    还让离线复算方无需信任数据库就知道链从哪里开始。
    """
    with session_factory() as db:
        record = _write(db)
        db.commit()

        assert record.seq == 1
        assert record.prev_hash == GENESIS_HASH
        assert len(record.entry_hash) == 64


def test_each_record_links_to_the_previous_entry(session_factory, signing_key):
    with session_factory() as db:
        for _ in range(5):
            _write(db)
        db.commit()

        records = _chain(db)
        assert [r.seq for r in records] == [1, 2, 3, 4, 5]
        for previous, current in zip(records, records[1:], strict=False):
            assert current.prev_hash == previous.entry_hash


def test_the_hash_survives_an_independent_recomputation(session_factory, signing_key):
    """用公开的配方手工重算一遍。

    校验器与第三方复算方都只能靠这个配方——它必须是对外可见、且逐字节确定的。
    这条测试把配方钉住：字段序、分隔符、时间格式、空值表示，任何一处漂移都会让
    所有历史记录的哈希对不上，而那时看起来就像整条链被篡改过。
    """
    with session_factory() as db:
        record = _write(db, payload={"a": 1})
        db.commit()

        material = {
            "chain_id": AUDIT_CHAIN_ID,
            "seq": 1,
            "event_type": "test.event",
            "actor": "user:11111111-1111-1111-1111-111111111111",
            "project_id": None,
            "run_id": None,
            "approval_id": None,
            "version_id": None,
            "preparation_id": None,
            # SQLite 读回来是 naive，必须过 as_utc 才能得到与写入时同一个时刻
            "occurred_at": as_utc(record.occurred_at).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "payload": {"a": 1},
            "prev_hash": GENESIS_HASH,
        }
        expected = hashlib.sha256(
            json.dumps(material, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        assert record.entry_hash == expected


def test_payload_is_redacted_before_it_is_hashed(session_factory, signing_key):
    """脱敏必须发生在**哈希之前**。

    先哈希再脱敏的话，落库的内容与参与哈希的内容就不是同一份，校验器永远复算
    不上——一条永远报错的链比没有链更糟，因为它让人不再相信告警。
    """
    with session_factory() as db:
        record = _write(
            db,
            payload={
                "password": "s3cr3t",
                "connection_string": "mysql://u:p@h/db",
                "note": "ok",
            },
        )
        db.commit()

        assert record.payload["password"] == "***"
        assert record.payload["connection_string"] == "***"
        assert record.payload["note"] == "ok"
        # 用**落库后**的载荷复算，应当与落库的哈希一致
        assert record.entry_hash == compute_entry_hash(
            chain_id=record.chain_id,
            seq=record.seq,
            event_type=record.event_type,
            actor=record.actor,
            project_id=record.project_id,
            run_id=record.run_id,
            approval_id=record.approval_id,
            version_id=record.version_id,
            preparation_id=record.preparation_id,
            occurred_at=record.occurred_at,
            payload=record.payload,
            prev_hash=record.prev_hash,
        )


def test_actor_is_qualified_with_its_kind(session_factory, signing_key):
    """``user:`` / ``worker:`` / ``system:`` 三种主体要能区分。

    只留一个裸 UUID 的话，事后无法回答「这条是用户点的还是 Worker 自己写的」——
    而这两者承担的责任完全不同。
    """
    with session_factory() as db:
        bare = _write(db, actor="11111111-1111-1111-1111-111111111111")
        worker = _write(db, actor="worker:22222222-2222-2222-2222-222222222222")
        system = _write(db, actor="system:maintenance")
        db.commit()

        assert bare.actor == "user:11111111-1111-1111-1111-111111111111"
        assert worker.actor == "worker:22222222-2222-2222-2222-222222222222"
        assert system.actor == "system:maintenance"


def test_a_checkpoint_is_signed_every_n_records(
    session_factory, signing_key, monkeypatch
):
    monkeypatch.setattr(settings, "audit_checkpoint_every", 3)

    with session_factory() as db:
        for _ in range(6):
            _write(db)
        db.commit()

        records = _chain(db)
        # seq 1 也带签名：系统从未锚定过，第一条就是它第一个可签的位置
        assert [r.seq for r in records if r.signature] == [1, 3, 6]

        for record in records:
            if not record.signature:
                continue
            parsed = parse_signature(record.signature)
            assert parsed is not None
            version, kid, _ = parsed
            assert version == "v1"
            assert kid == "test-key-1"


def test_a_checkpoint_is_also_signed_by_elapsed_time(session_factory, signing_key):
    """低频系统也必须被锚定。

    只看「每 100 条」的话，一个总共写不了几条记录的系统永远等不到第一个检查点，
    于是「链上从无签名」与「链被整条重写」在事后分不出来。
    """
    with session_factory() as db:
        _write(db)
        db.commit()

        head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
        head.last_anchored_at = datetime.now(UTC) - timedelta(
            seconds=settings.audit_checkpoint_seconds + 60
        )
        db.commit()

        later = _write(db)
        db.commit()

        assert later.signature is not None


def test_a_stale_head_view_is_reconciled_by_retry(session_factory, signing_key):
    """链头被别人抢先推进后，追加要落到正确的 seq 上。

    SQLite 没有行锁（``FOR UPDATE`` 被静默忽略），这条路径就是它唯一的正确性
    来源。这里用「过期的链头视图」确定性地驱动它：拿到 head 之后由另一个写入者
    先插一条，再看后继者是否接在正确的位置。
    """
    with session_factory() as db:
        first = _write(db)
        db.commit()

        head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
        assert head.last_seq == 1

        # 模拟另一个写入者：直接把链头推进一格并插入对应记录
        other_hash = "a" * 64
        db.add(
            AuditRecord(
                seq=2,
                event_type="other.event",
                actor="user:other",
                prev_hash=first.entry_hash,
                entry_hash=other_hash,
                occurred_at=datetime.now(UTC),
                chain_id=AUDIT_CHAIN_ID,
            )
        )
        head.last_seq = 2
        head.last_hash = other_hash
        db.commit()

        third = _write(db)
        db.commit()

        assert third.seq == 3
        assert third.prev_hash == other_hash


def test_the_chain_head_is_created_lazily(session_factory, signing_key):
    with session_factory() as db:
        assert db.get(AuditChainHead, AUDIT_CHAIN_ID) is None
        _write(db)
        db.commit()

        head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
        assert head.last_seq == 1
        assert head.last_hash != GENESIS_HASH


def test_an_explicit_time_is_hashed_as_given(session_factory, signing_key):
    """``occurred_at`` 由调用方给定时必须原样参与哈希。

    工具留痕用的是 ``invocation.invoked_at``（调用发生的那一刻），而不是入库时刻——
    两者在排队场景下会差出可观的时长，记错了就等于记错了「什么时候被调用」。
    """
    moment = datetime(2026, 10, 16, 3, 14, 15, 926535, tzinfo=UTC)
    with session_factory() as db:
        record = _write(db, occurred_at=moment)
        db.commit()

        assert as_utc(record.occurred_at) == moment
        assert record.entry_hash == compute_entry_hash(
            chain_id=record.chain_id,
            seq=record.seq,
            event_type=record.event_type,
            actor=record.actor,
            project_id=None,
            run_id=None,
            approval_id=None,
            version_id=None,
            preparation_id=None,
            occurred_at=moment,
            payload=None,
            prev_hash=GENESIS_HASH,
        )


def test_the_sink_records_a_tool_invocation(session_factory, signing_key):
    """Broker 的留痕接入面（SPEC §10.1 曾称之为「待补的真实缺陷」）。"""

    class _Invocation:
        capability_id = uuid.uuid4()
        tool = "execution.run"
        subject = "user:33333333-3333-3333-3333-333333333333"
        environment = "dev"
        artifact = "a" * 64
        arguments_digest = "d" * 64
        side_effect = True
        succeeded = True
        invoked_at = datetime.now(UTC)

    with session_factory() as db:
        audit.LedgerInvocationSink().record(_Invocation(), db=db)
        db.commit()

        records = _chain(db)
        assert len(records) == 1
        assert records[0].event_type == "tool.invoked"
        assert records[0].actor == "user:33333333-3333-3333-3333-333333333333"
        # 参数只留指纹，不留原文
        assert records[0].payload["arguments_digest"] == "d" * 64
        assert "password" not in json.dumps(records[0].payload)


def test_the_sink_without_a_session_does_not_raise(session_factory, signing_key):
    """没有会话时只告警不抛——Broker 是在 ``finally`` 里调用它的。

    抛出会顶掉正在上抛的业务异常，把「执行失败」伪装成「留痕失败」。
    """

    class _Invocation:
        tool = "execution.run"
        subject = "user:x"
        capability_id = uuid.uuid4()
        environment = "dev"
        artifact = "a" * 64
        arguments_digest = "d" * 64
        side_effect = True
        succeeded = False
        invoked_at = datetime.now(UTC)

    audit.LedgerInvocationSink().record(_Invocation(), db=None)


def test_the_hash_ignores_key_order_inside_the_payload():
    """载荷内部的键序不影响哈希。

    **这条是真实环境教给我们的。** PostgreSQL 的 JSONB 不保留键序（按键名长度
    与字节序存储），同一条载荷写进去再读出来，``json.dumps`` 的结果就可能不同——
    哈希于是对不上，整条链从该条起全部报 ``HASH_MISMATCH``。而 SQLite 的 JSON
    恰好保留插入序，所以这个 bug 在整套单元测试下完全不可见，只在真库上炸。

    顶层字段的顺序仍由 ``HASH_FIELD_ORDER`` 固定；这里锁住的是嵌套载荷。
    """
    moment = datetime(2026, 10, 16, 3, 0, 0, tzinfo=UTC)
    common = {
        "chain_id": AUDIT_CHAIN_ID,
        "seq": 1,
        "event_type": "e",
        "actor": "user:a",
        "project_id": None,
        "run_id": None,
        "approval_id": None,
        "version_id": None,
        "preparation_id": None,
        "occurred_at": moment,
        "prev_hash": GENESIS_HASH,
    }

    assert compute_entry_hash(**common, payload={"beta": 1, "alpha": 2}) == (
        compute_entry_hash(**common, payload={"alpha": 2, "beta": 1})
    )
    assert compute_entry_hash(
        **common, payload={"outer": {"z": 1, "a": 2}, "list": [{"b": 1, "a": 2}]}
    ) == compute_entry_hash(
        **common, payload={"list": [{"a": 2, "b": 1}], "outer": {"a": 2, "z": 1}}
    )
    # 归一化不能顺手把「键不同」也抹平——那样篡改就检不出来了
    assert compute_entry_hash(**common, payload={"a": 1}) != (
        compute_entry_hash(**common, payload={"a": 2})
    )
    assert compute_entry_hash(**common, payload={"a": 1}) != (
        compute_entry_hash(**common, payload={"b": 1})
    )
