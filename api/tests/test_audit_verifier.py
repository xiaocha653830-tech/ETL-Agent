"""T-20 证据账本：篡改可被检出（FR-39）。

这一组测试就是 FR-39 存在的理由。「哈希链 + 签名检查点」如果只证明了「正常写入时
能连成一条链」，那它什么都没证明——真正的命题是：**改动之后能发现**。所以每一种
改动方式都有一条用例，且断言到**具体的失败码与位置**，而不是笼统的「不通过」。

篡改一律绕过 ORM 用原生 SQL 做，并提交后另开会话校验：走 ORM 会把对象放进
identity map，校验器读到的可能还是内存里那份没被改的旧值，测试就假过了。
"""

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text

from app.core.config import settings
from app.core.errors import AuditIntegrityBreach
from app.core.keys import SigningKey
from app.models.audit import AUDIT_CHAIN_ID, AuditChainHead, IntegrityStatus
from app.services import audit, audit_verifier
from app.services.audit import record_audit
from app.services.audit_verifier import (
    FailureCode,
    assert_ledger_healthy,
    record_verification,
    verify_chain,
)

# 用一把固定的密钥，测试才能对签名做精确的破坏（而不是整个信封格式崩掉）
_KEY = SigningKey("test-key-1", Ed25519PrivateKey.generate())


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(audit, "get_signing_key", lambda: _KEY)
    monkeypatch.setattr(audit_verifier, "get_signing_key", lambda: _KEY)
    return _KEY


@pytest.fixture()
def chain(session_factory):
    """一条 5 条记录的干净链，每 2 条一个签名检查点。"""

    def _build(count: int = 5) -> None:
        with session_factory() as db:
            for index in range(count):
                record_audit(
                    db,
                    event_type="test.event",
                    actor=f"user:{index:08d}-0000-0000-0000-000000000000",
                    payload={"index": index},
                )
            db.commit()

    return _build


@pytest.fixture()
def checkpoint_every(monkeypatch):
    monkeypatch.setattr(settings, "audit_checkpoint_every", 2)


def _verify(session_factory):
    with session_factory() as db:
        return verify_chain(db)


def test_a_clean_chain_verifies(session_factory, chain, checkpoint_every):
    chain(5)
    result = _verify(session_factory)

    assert result.status is IntegrityStatus.VERIFIED
    assert result.checked == 5
    assert result.first_failure_seq is None
    # seq 1 因「从未锚定过」也带签名，其后每 2 条一个
    assert result.anchors_checked == result.anchors_verified == 3


def test_an_empty_chain_is_not_reported_as_verified(session_factory):
    """空链是 ``empty`` 而不是 ``verified``。

    「没有证据」与「证据通过校验」是两回事。把它们合成一个状态，一个从未记录过
    任何东西的系统会显示为「完整性校验通过」——那正是审计最不该给出的结论。
    """
    result = _verify(session_factory)

    assert result.status is IntegrityStatus.EMPTY
    assert result.checked == 0
    assert not result.healthy


def test_a_changed_actor_is_detected(session_factory, chain, checkpoint_every):
    chain(5)
    with session_factory() as db:
        db.execute(text("UPDATE audit_records SET actor='user:attacker' WHERE seq=3"))
        db.commit()

    result = _verify(session_factory)

    assert result.status is IntegrityStatus.BREACHED
    assert result.first_failure_seq == 3
    assert result.failures[0].code is FailureCode.HASH_MISMATCH


def test_a_changed_payload_is_detected(session_factory, chain, checkpoint_every):
    """改内容最隐蔽的一种：链的形状完好，只是某一条说的不是原话。"""
    chain(5)
    with session_factory() as db:
        db.execute(
            text("UPDATE audit_records SET payload='{\"index\": 99}' WHERE seq=2")
        )
        db.commit()

    result = _verify(session_factory)

    assert result.status is IntegrityStatus.BREACHED
    assert result.first_failure_seq == 2
    assert result.failures[0].code is FailureCode.HASH_MISMATCH


def test_a_deleted_middle_record_is_detected(session_factory, chain, checkpoint_every):
    chain(5)
    with session_factory() as db:
        db.execute(text("DELETE FROM audit_records WHERE seq=4"))
        db.commit()

    result = _verify(session_factory)

    assert result.status is IntegrityStatus.BREACHED
    assert result.first_failure_seq == 5
    codes = {item.code for item in result.failures}
    assert FailureCode.SEQ_GAP in codes
    assert FailureCode.BROKEN_LINK in codes


def test_a_rewritten_pointer_is_detected(session_factory, chain, checkpoint_every):
    chain(5)
    with session_factory() as db:
        db.execute(
            text("UPDATE audit_records SET prev_hash=:h WHERE seq=5"),
            {"h": "f" * 64},
        )
        db.commit()

    result = _verify(session_factory)

    assert result.status is IntegrityStatus.BREACHED
    assert result.failures[0].code is FailureCode.BROKEN_LINK


def test_a_flipped_signature_bit_is_detected(session_factory, chain, checkpoint_every):
    chain(5)
    with session_factory() as db:
        # seq=2 是锚点（seq=1 因「从未锚定过」也签，其后每 2 条一个：1, 2, 4）
        envelope = db.execute(
            text("SELECT signature FROM audit_records WHERE seq=2")
        ).scalar()
        version, kid, encoded = envelope.split(":", 2)
        raw = bytearray(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        raw[0] ^= 0x01
        broken = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
        db.execute(
            text("UPDATE audit_records SET signature=:s WHERE seq=2"),
            {"s": f"{version}:{kid}:{broken}"},
        )
        db.commit()

    result = _verify(session_factory)

    assert result.status is IntegrityStatus.BREACHED
    assert result.first_failure_seq == 2
    assert result.failures[0].code is FailureCode.SIGNATURE_INVALID


def test_a_rotated_kid_is_indeterminate_not_breached(
    session_factory, chain, checkpoint_every
):
    """**「验不了」不等于「验不过」。**

    密钥轮换出去之后，旧检查点在新密钥下无法验证。如果把它报成篡改，一次正常的
    轮转就会拉起一串假警报——而会误报的告警，很快就会没人再看。
    """
    chain(5)
    with session_factory() as db:
        db.execute(
            text(
                "UPDATE audit_records SET signature="
                "replace(signature, 'test-key-1', 'rotated-key-9') WHERE seq=2"
            )
        )
        db.commit()

    result = _verify(session_factory)

    assert result.status is IntegrityStatus.INDETERMINATE
    assert result.first_failure_seq is None
    assert result.anchors_checked == 3
    assert result.anchors_verified == 2


def test_a_truncated_tail_is_detected(session_factory, chain, checkpoint_every):
    """**尾部截断**——这条是链头那张表存在的全部理由。

    删掉链尾（连同最后的检查点）之后，剩下的每条记录 prev_hash 都对得上、签名也
    验得过：它是一条完全自洽的短链，任何记录内部都发现不了。只有把表内实际内容
    与链头记载比对一次，才暴露得出来。
    """
    chain(5)
    with session_factory() as db:
        db.execute(text("DELETE FROM audit_records WHERE seq >= 4"))
        db.commit()

    result = _verify(session_factory)

    assert result.status is IntegrityStatus.BREACHED
    codes = {item.code for item in result.failures}
    assert FailureCode.HEAD_MISMATCH in codes
    # 短链自身是自洽的——所以问题只能由链头报出
    assert not any(
        item.code in (FailureCode.BROKEN_LINK, FailureCode.HASH_MISMATCH)
        for item in result.failures
    )


def test_a_record_inserted_outside_the_append_path_is_detected(
    session_factory, chain, checkpoint_every
):
    """绕过 ``record_audit`` 直接塞一条进来。

    这类插入不会推进链头，所以表内 max(seq) 会超过链头——同样是 HEAD_MISMATCH。
    """
    chain(3)
    with session_factory() as db:
        db.execute(
            text(
                "INSERT INTO audit_records "
                "(id, seq, event_type, actor, prev_hash, entry_hash, "
                "occurred_at, chain_id, created_at, updated_at) "
                "VALUES (:id, 99, 'injected', 'user:attacker', :ph, :eh, :ts, :chain, :ts, :ts)"
            ),
            {
                "id": str(uuid.uuid4()),
                "ph": "a" * 64,
                "eh": "b" * 64,
                "ts": datetime.now(UTC).replace(tzinfo=None).isoformat(sep=" "),
                "chain": AUDIT_CHAIN_ID,
            },
        )
        db.commit()

    result = _verify(session_factory)

    assert result.status is IntegrityStatus.BREACHED
    codes = {item.code for item in result.failures}
    assert FailureCode.SEQ_GAP in codes
    assert FailureCode.HEAD_MISMATCH in codes


def test_a_breach_blocks_authorization_until_a_clean_verification(
    session_factory, chain, checkpoint_every
):
    """破链后授权类动作被拒；修复并重新校验成功后恢复。"""
    chain(4)
    with session_factory() as db:
        record_verification(db, verify_chain(db), trigger="manual")
    with session_factory() as db:
        assert_ledger_healthy(db)  # 干净时放行

    with session_factory() as db:
        original = db.execute(
            text("SELECT actor FROM audit_records WHERE seq=2")
        ).scalar()
        db.execute(text("UPDATE audit_records SET actor='user:attacker' WHERE seq=2"))
        db.commit()

    with session_factory() as db:
        record_verification(db, verify_chain(db), trigger="manual")
        head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
        assert head.integrity_status == IntegrityStatus.BREACHED.value
        assert head.breach_seq == 2
        with pytest.raises(AuditIntegrityBreach):
            assert_ledger_healthy(db)

    # 把内容改回去——链重新自洽，一次成功的完整校验解除阻断
    with session_factory() as db:
        db.execute(
            text("UPDATE audit_records SET actor=:a WHERE seq=2"), {"a": original}
        )
        db.commit()
    with session_factory() as db:
        result = verify_chain(db)
        record_verification(db, result, trigger="manual")
        head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
        assert head.integrity_status == IntegrityStatus.VERIFIED.value
        assert head.breach_seq is None
        assert_ledger_healthy(db)


def test_an_unanchored_tail_is_a_warning_not_a_breach(
    session_factory, chain, checkpoint_every
):
    """链完好但久未锚定：告警，不是破链。

    把「该签没签」升级成「链坏了」，会让运维去查一桩并不存在的篡改。
    """
    chain(3)
    with session_factory() as db:
        head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
        head.last_anchored_at = datetime.now(UTC) - timedelta(hours=2)
        db.commit()

    with session_factory() as db:
        result = verify_chain(db)

    assert result.status is IntegrityStatus.VERIFIED
    assert result.unanchored_tail_seconds is not None
    assert result.unanchored_tail_seconds > 3600


def test_verification_history_records_the_first_failure_position(
    session_factory, chain, checkpoint_every
):
    """校验历史要能回答「从第几条开始坏」。"""
    chain(4)
    with session_factory() as db:
        db.execute(text("DELETE FROM audit_records WHERE seq=3"))
        db.commit()

    with session_factory() as db:
        row = record_verification(db, verify_chain(db), trigger="periodic")
        assert row.status == IntegrityStatus.BREACHED.value
        assert row.first_failure_seq == 4
        assert row.trigger == "periodic"
        assert row.checked == 3
        assert row.to_seq == 4
