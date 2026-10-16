"""证据账本校验器（M11，T-20 / FR-39）。

校验是**只读扫描**：按 ``seq`` 逐条复算 ``entry_hash``、比对 ``prev_hash`` 的衔接、
验证检查点签名，最后把表内实际内容与链头记载比对一次。

最后那一步是这张表存在的理由。只删掉链尾若干条（连同最后一个检查点）会留下一条
**完全自洽的短链**——每条 ``prev_hash`` 都对得上、签名也验得过，任何记录内部都
发现不了。链头把末条的 ``seq`` 与 ``hash`` 独立记着，截断于是无处遁形。

**失败分级**（``BREACHED`` 与 ``INDETERMINATE`` 必须分开）：签名用的密钥轮换出去
之后，旧检查点会验不了——那是「无法验证」，不是「验证不通过」。把两者合并，一次
正常的密钥轮转就会显示成一次篡改告警，而告警一旦会误报就再也没人认真看了。
"""

import base64
import enum
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AuditIntegrityBreach
from app.core.keys import get_signing_key
from app.db.types import as_utc
from app.models.audit import (
    AUDIT_CHAIN_ID,
    GENESIS_HASH,
    AuditChainHead,
    AuditRecord,
    AuditVerification,
    IntegrityStatus,
)
from app.services.audit import (
    SIGNATURE_VERSION,
    anchor_bytes,
    compute_entry_hash,
    parse_signature,
)

logger = logging.getLogger(__name__)


class FailureCode(enum.StrEnum):
    """校验失败的具体原因。**能区分才有用**——只说「链坏了」无从下手。"""

    SEQ_GAP = "SEQ_GAP"  # 缺号：有记录被删
    SEQ_ORDER = "SEQ_ORDER"  # 乱序或重复
    GENESIS_MISMATCH = "GENESIS_MISMATCH"  # 首条没有接在创世上
    BROKEN_LINK = "BROKEN_LINK"  # prev_hash 与上一条的 entry_hash 对不上
    HASH_MISMATCH = "HASH_MISMATCH"  # 内容被改：复算的哈希与记录不符
    SIGNATURE_INVALID = "SIGNATURE_INVALID"  # 检查点签名验不过
    SIGNATURE_MALFORMED = "SIGNATURE_MALFORMED"  # 签名信封格式非法/版本未知
    ANCHOR_LINK_BROKEN = "ANCHOR_LINK_BROKEN"  # 检查点之间的衔接断了
    HEAD_MISMATCH = "HEAD_MISMATCH"  # 表内内容与链头不符（含尾部截断）


@dataclass(frozen=True)
class Failure:
    seq: int
    code: FailureCode
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"seq": self.seq, "code": self.code.value, "detail": self.detail}


@dataclass(frozen=True)
class VerificationResult:
    """一次校验的完整结论。

    ``failures`` 只是**收集上限**内的明细；``checked`` 与 ``first_failure_seq``
    始终是扫描完整条链之后的结果——报得出「第几条开始坏」才有排查价值。
    """

    status: IntegrityStatus
    checked: int
    from_seq: int | None
    to_seq: int | None
    head_seq: int
    head_hash: str
    anchors_checked: int
    anchors_verified: int
    first_failure_seq: int | None
    failures: tuple[Failure, ...]
    unanchored_tail_seconds: float | None
    duration_ms: int

    @property
    def healthy(self) -> bool:
        """只有 ``VERIFIED`` 算健康。``EMPTY`` 不是——没证据不等于证据可信。"""
        return self.status is IntegrityStatus.VERIFIED

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "checked": self.checked,
            "from_seq": self.from_seq,
            "to_seq": self.to_seq,
            "head_seq": self.head_seq,
            "head_hash": self.head_hash,
            "anchors_checked": self.anchors_checked,
            "anchors_verified": self.anchors_verified,
            "first_failure_seq": self.first_failure_seq,
            "failures": [item.as_dict() for item in self.failures],
            "unanchored_tail_seconds": self.unanchored_tail_seconds,
            "duration_ms": self.duration_ms,
        }


def verify_chain(
    db: Session,
    *,
    chain_id: str = AUDIT_CHAIN_ID,
    trigger: str = "manual",
) -> VerificationResult:
    """完整扫一遍链并给出结论。**只读**——不改任何 ``AUDIT_RECORD``。"""
    started = time.monotonic()
    head = db.get(AuditChainHead, chain_id)

    failures: list[Failure] = []
    first_failure_seq: int | None = None
    checked = 0
    anchors_checked = 0
    anchors_verified = 0
    expected_seq = 1
    expected_prev = GENESIS_HASH
    last_anchor_hash = GENESIS_HASH
    max_seq = 0
    last_hash = GENESIS_HASH
    unknown_kid = False

    def fail(seq: int, code: FailureCode, detail: str) -> None:
        nonlocal first_failure_seq
        if first_failure_seq is None:
            first_failure_seq = seq
        if len(failures) < settings.audit_verify_max_failures:
            failures.append(Failure(seq=seq, code=code, detail=detail))

    # keyset 分批：链会持续增长，一次性载入内存的做法迟早会撞上规模。
    after = 0
    while True:
        batch = list(
            db.scalars(
                select(AuditRecord)
                .where(AuditRecord.chain_id == chain_id, AuditRecord.seq > after)
                .order_by(AuditRecord.seq)
                .limit(settings.audit_verify_batch_size)
            )
        )
        if not batch:
            break

        for record in batch:
            checked += 1
            after = record.seq
            max_seq = record.seq
            last_hash = record.entry_hash

            if record.seq != expected_seq:
                code = (
                    FailureCode.SEQ_GAP
                    if record.seq > expected_seq
                    else FailureCode.SEQ_ORDER
                )
                fail(
                    record.seq,
                    code,
                    f"期望 seq={expected_seq}，实际 {record.seq}",
                )

            if record.seq == 1:
                if record.prev_hash != GENESIS_HASH:
                    fail(record.seq, FailureCode.GENESIS_MISMATCH, "首条未接在创世哈希上")
            elif record.prev_hash != expected_prev:
                fail(record.seq, FailureCode.BROKEN_LINK, "prev_hash 与上一条的 entry_hash 不符")

            recomputed = compute_entry_hash(
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
            if recomputed != record.entry_hash:
                fail(record.seq, FailureCode.HASH_MISMATCH, "记录内容与 entry_hash 不符")

            if record.signature:
                anchors_checked += 1
                parsed = parse_signature(record.signature)
                if parsed is None:
                    fail(record.seq, FailureCode.SIGNATURE_MALFORMED, "签名信封格式非法")
                else:
                    version, kid, encoded = parsed
                    key = get_signing_key()
                    if version != SIGNATURE_VERSION:
                        fail(
                            record.seq,
                            FailureCode.SIGNATURE_MALFORMED,
                            f"未知的签名信封版本 {version}",
                        )
                    elif kid != key.kid:
                        # 密钥轮转出去的旧检查点：**验不了 ≠ 验不过**
                        unknown_kid = True
                    else:
                        padded = encoded + "=" * (-len(encoded) % 4)
                        message = anchor_bytes(
                            chain_id=record.chain_id,
                            seq=record.seq,
                            entry_hash=record.entry_hash,
                            prev_anchor_hash=last_anchor_hash,
                            anchored_at=record.occurred_at,
                        )
                        try:
                            key.verify(message, base64.urlsafe_b64decode(padded))
                            anchors_verified += 1
                        except (InvalidSignature, ValueError):
                            fail(
                                record.seq,
                                FailureCode.SIGNATURE_INVALID,
                                "检查点签名验证失败",
                            )
                last_anchor_hash = record.entry_hash

            expected_prev = record.entry_hash
            expected_seq = record.seq + 1

    # 与链头对账——这一步专治「整条自洽的短链」
    if head is None:
        if max_seq:
            fail(max_seq, FailureCode.HEAD_MISMATCH, "存在记录但没有链头")
    else:
        if head.last_seq != max_seq:
            fail(
                max_seq,
                FailureCode.HEAD_MISMATCH,
                f"链头 last_seq={head.last_seq}，表内 max(seq)={max_seq}（疑似尾部截断）",
            )
        elif head.last_hash != last_hash:
            fail(max_seq, FailureCode.HEAD_MISMATCH, "链头 last_hash 与末条记录不符")

    unanchored_tail: float | None = None
    if head is not None and head.last_anchored_at is not None:
        unanchored_tail = (
            datetime.now(UTC) - as_utc(head.last_anchored_at)
        ).total_seconds()

    if failures:
        status = IntegrityStatus.BREACHED
    elif unknown_kid:
        status = IntegrityStatus.INDETERMINATE
    elif checked == 0:
        status = IntegrityStatus.EMPTY
    else:
        status = IntegrityStatus.VERIFIED

    result = VerificationResult(
        status=status,
        checked=checked,
        from_seq=1 if checked else None,
        to_seq=max_seq or None,
        head_seq=max_seq,
        head_hash=last_hash,
        anchors_checked=anchors_checked,
        anchors_verified=anchors_verified,
        first_failure_seq=first_failure_seq,
        failures=tuple(failures),
        unanchored_tail_seconds=unanchored_tail,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    _log_result(result, trigger=trigger, chain_id=chain_id)
    return result


def _log_result(result: VerificationResult, *, trigger: str, chain_id: str) -> None:
    if result.status is IntegrityStatus.BREACHED:
        logger.error(
            "证据链校验不通过 chain=%s trigger=%s 首个失败 seq=%s 失败码=%s",
            chain_id,
            trigger,
            result.first_failure_seq,
            [item.code.value for item in result.failures[:5]],
        )
    elif result.status is IntegrityStatus.INDETERMINATE:
        logger.warning(
            "证据链哈希完好但签名无法验证 chain=%s trigger=%s 已检查锚点=%s",
            chain_id,
            trigger,
            result.anchors_checked,
        )
    else:
        logger.info(
            "证据链校验完成 chain=%s trigger=%s status=%s 记录=%s 锚点=%s",
            chain_id,
            trigger,
            result.status.value,
            result.checked,
            result.anchors_checked,
        )


def record_verification(
    db: Session,
    result: VerificationResult,
    *,
    chain_id: str = AUDIT_CHAIN_ID,
    trigger: str = "manual",
) -> AuditVerification:
    """落一条校验历史，并把结论写回链头。

    **这里自行提交**：校验是一个独立的运维动作，不依附任何业务事务——它与
    业务的关系恰恰在于「即使业务全停了，也能回答链还好不好」。

    链头状态的更新是**唯一**的解除路径：``breached`` 只有被一次成功（``verified``）
    的完整校验覆盖才会清掉，没有人工 ack 接口。
    """
    row = AuditVerification(
        chain_id=chain_id,
        trigger=trigger,
        status=result.status.value,
        checked=result.checked,
        from_seq=result.from_seq,
        to_seq=result.to_seq,
        head_seq=result.head_seq,
        head_hash=result.head_hash,
        anchors_checked=result.anchors_checked,
        anchors_verified=result.anchors_verified,
        first_failure_seq=result.first_failure_seq,
        failures=[item.as_dict() for item in result.failures] or None,
        unanchored_tail_seconds=result.unanchored_tail_seconds,
        duration_ms=result.duration_ms,
    )
    db.add(row)

    head = db.get(AuditChainHead, chain_id)
    if head is not None:
        head.integrity_status = result.status.value
        if result.status is IntegrityStatus.BREACHED:
            head.breach_seq = result.first_failure_seq
            head.breach_detected_at = datetime.now(UTC)
        else:
            head.breach_seq = None
            head.breach_detected_at = None

    db.commit()
    return row


def latest_verification(
    db: Session, *, chain_id: str = AUDIT_CHAIN_ID
) -> AuditVerification | None:
    return db.scalar(
        select(AuditVerification)
        .where(AuditVerification.chain_id == chain_id)
        .order_by(AuditVerification.created_at.desc())
        .limit(1)
    )


def assert_ledger_healthy(
    db: Session, *, chain_id: str = AUDIT_CHAIN_ID
) -> None:
    """授权类动作（Commit / 回滚）在动手之前先问一句「证据还可信吗」。

    只读一次链头（主键查找）。**只挡授权类动作**：把只读接口也一起挡住，等于
    让攻击者通过制造一次篡改来关停整个系统，而那时他恰好最需要系统停下来。
    """
    if not settings.audit_breach_blocks_authorization:
        return
    head = db.get(AuditChainHead, chain_id)
    if head is not None and head.integrity_status == IntegrityStatus.BREACHED.value:
        raise AuditIntegrityBreach(
            f"证据链自 seq={head.breach_seq} 起校验不通过；"
            "需先完成一次成功的完整校验，授权类操作才会恢复"
        )
