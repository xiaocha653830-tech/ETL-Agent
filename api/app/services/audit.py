"""证据账本写路径（M11，T-20 / FR-39 / FR-40）。

**哈希链**：一条记录的内容（事件、主体、引用的对象、时间、脱敏后的载荷）连同
上一条的 ``entry_hash`` 一起做 SHA-256，得到本条的 ``entry_hash``。改动其中任何
一个字节，从该条起整条链都对不上——这就是「篡改可被检出」的全部机制。它**不依赖
秘密**：规范化配方、字段序、时间格式与创世哈希都是公开的，第三方拿着导出的报告
可以独立复算，这才是审计证据该有的样子。

**签名检查点**：哈希链本身防不住「谁能改库谁就能整条重算」。所以按 SPEC Q5 的
定案——逐条哈希、**按批签名**：每 N 条或每 T 秒，用 Ed25519 给当前链头签一次名。
要伪造一条自洽的链，还得能拿出私钥重签检查点。

**写入口只有 ``record_audit`` 一个**，且它**不提交事务**——提交权归调用方，
使「业务事实」与「它的证据」落在同一个事务里，不会出现一方成立、另一方丢失。
这与 ``outbox.enqueue``、``run_operation``、``fenced_update`` 是同一条纪律。
"""

import base64
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import AuditAppendConflict
from app.core.keys import get_signing_key
from app.db.types import as_utc
from app.models.approval import Preparation
from app.models.audit import (
    AUDIT_CHAIN_ID,
    GENESIS_HASH,
    AuditChainHead,
    AuditRecord,
    IntegrityStatus,
)
from app.models.capability import Capability
from app.services.redaction import redact_snapshot

logger = logging.getLogger(__name__)

# 签名信封的版本号。**将来改动哈希配方或信封格式时必须递增它**：没有版本号的话，
# 历史记录里的旧格式会被新校验器读成「被篡改」——那是把一次可预期的演进写死成
# 一条假警报。有版本号，「格式变了」与「内容被改了」才分得开。
SIGNATURE_VERSION = "v1"

# 哈希材料的字段序。**显式定序，不用 sort_keys**：字段将来增删时，
# 排序规则的变化会静默改变所有历史记录的哈希。
HASH_FIELD_ORDER = (
    "chain_id",
    "seq",
    "event_type",
    "actor",
    "project_id",
    "run_id",
    "approval_id",
    "version_id",
    "preparation_id",
    "occurred_at",
    "payload",
    "prev_hash",
)

# 时间格式。定死到微秒并强制 UTC——不用 ``isoformat()``：它会产出 ``+00:00``，
# 且行为受 ``fold`` 与子类影响，逐字节复算要求格式完全确定。
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# 事件类型（``域.动作``，与 run_events 的命名一致）。
#
# 集中定义而不是散在各个服务里：审计的覆盖率是**按清单核对**的
# （PRD「关键事件 Prepare / Approve / Commit / 执行 / 回滚 覆盖率 100%」），
# 清单一旦散开，就再没有人能回答「一共该有哪些事件」。
EVENT_TOOL_INVOKED = "tool.invoked"
EVENT_PREPARE_CREATED = "prepare.created"
EVENT_PREPARE_REISSUED = "prepare.reissued"
EVENT_APPROVAL_DECIDED = "approval.decided"
EVENT_APPROVAL_DENIED = "approval.denied"
EVENT_APPROVAL_EXPIRED = "approval.expired"
EVENT_COMMIT_ACCEPTED = "commit.accepted"
EVENT_EXECUTION_CLAIMED = "execution.claimed"
EVENT_EXECUTION_SUBMITTED = "execution.submitted"
EVENT_EXECUTION_CANCEL_REQUESTED = "execution.cancel_requested"
EVENT_EXECUTION_CANCELLED = "execution.cancelled"
EVENT_EXECUTION_QUALITY_REPORT = "execution.quality_report"
EVENT_ROLLBACK_EXECUTED = "rollback.executed"
EVENT_ROLLBACK_SKIPPED = "rollback.skipped"

# 终态事件按状态拼出：``execution.succeeded`` / ``execution.failed`` / ``execution.cancelled``。
# 不为三种终态各写一个常量，是因为它们本来就是「同一个动作的不同结局」。
EXECUTION_TERMINAL_PREFIX = "execution."


def _encode_actor(actor: str) -> str:
    """把「主体」规范化成 ``类型:标识``。

    取值三类：``user:<uuid>`` / ``worker:<uuid>`` / ``system:<名字>``。已经带冒号的
    原样透传（``maintenance`` 传进来的 ``system:maintenance`` 正属于这一类）；
    裸标识按用户处理，因为绝大多数调用方传的是 user_id。
    """
    return actor if ":" in actor else f"user:{actor}"


def _timestamp(value: datetime) -> str:
    return as_utc(value).strftime(TIMESTAMP_FORMAT)


def _normalize(value: Any) -> Any:
    """把嵌套结构里的字典按键名排序，其余原样。

    **这一步不能省**：PostgreSQL 的 JSONB **不保留键序**（它按键名长度与字节序
    存储），所以同一条载荷在写入时与读回来时的 ``json.dumps`` 结果可能不同，
    **哈希因此对不上**。

    这不是理论担忧——真实环境上一条 9 条记录的链，除首条外全部报
    ``HASH_MISMATCH``；而 SQLite 的 JSON 恰好保留插入序，以至于整套单元测试
    完全测不出来。顶层字段的顺序仍由 ``HASH_FIELD_ORDER`` 固定（那是配方的一部
    分，要对外公开），这里只规范化嵌套的业务载荷——它们的键序没有语义。
    """
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def _canonical(material: dict[str, Any]) -> bytes:
    """规范化序列化 —— 哈希的输入，也是对外公开的复算配方。"""
    return json.dumps(
        material, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def compute_entry_hash(
    *,
    chain_id: str,
    seq: int,
    event_type: str,
    actor: str,
    project_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    approval_id: uuid.UUID | None,
    version_id: uuid.UUID | None,
    preparation_id: uuid.UUID | None,
    occurred_at: datetime,
    payload: Any,
    prev_hash: str,
) -> str:
    """本条记录的 ``entry_hash``。纯函数——校验器用它复算。"""
    material = {
        "chain_id": chain_id,
        "seq": seq,
        "event_type": event_type,
        "actor": actor,
        "project_id": str(project_id) if project_id else None,
        "run_id": str(run_id) if run_id else None,
        "approval_id": str(approval_id) if approval_id else None,
        "version_id": str(version_id) if version_id else None,
        "preparation_id": str(preparation_id) if preparation_id else None,
        "occurred_at": _timestamp(occurred_at),
        # 载荷必须归一化：它存进 JSONB 后键序会变，见 _normalize 的说明
        "payload": _normalize(payload),
        "prev_hash": prev_hash,
    }
    return hashlib.sha256(_canonical(material)).hexdigest()


def anchor_bytes(
    *,
    chain_id: str,
    seq: int,
    entry_hash: str,
    prev_anchor_hash: str,
    anchored_at: datetime,
) -> bytes:
    """检查点签名消息。

    含 **上一个锚点的 entry_hash**：这样删掉或重排一整段检查点也会被发现，
    而不只是「某一条记录被改」。
    """
    return _canonical(
        {
            "chain_id": chain_id,
            "seq": seq,
            "entry_hash": entry_hash,
            "prev_anchor_hash": prev_anchor_hash,
            "anchored_at": _timestamp(anchored_at),
        }
    )


def encode_signature(kid: str, signature: bytes) -> str:
    """签名信封。用 ``base64url`` 且去 padding：单行、URL / JSON / CSV 都安全。

    普通 base64 含 ``+`` 与 ``/``，导出成 CSV 或塞进查询串时会被截断或转义。
    """
    encoded = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{SIGNATURE_VERSION}:{kid}:{encoded}"


def parse_signature(envelope: str) -> tuple[str, str, str] | None:
    """拆开信封，返回 ``(版本, kid, base64url 签名)``；格式非法返回 ``None``。"""
    parts = envelope.split(":", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def _anchor_due(*, seq: int, head: AuditChainHead, now: datetime) -> bool:
    """本条是否该成为签名检查点。

    「从未锚定过」也算到期——否则一个只写过几条记录的系统永远等不到第一个检查点。
    """
    if seq % settings.audit_checkpoint_every == 0:
        return True
    if head.last_anchored_at is None:
        return True
    elapsed = (as_utc(now) - as_utc(head.last_anchored_at)).total_seconds()
    return elapsed >= settings.audit_checkpoint_seconds


def _lock_head(db: Session, chain_id: str) -> AuditChainHead:
    """取链头并锁住它 —— 这是并发追加的串行化点。

    PostgreSQL 上 ``FOR UPDATE`` 让后来的追加者阻塞在这里，``seq`` 的分叉因此在
    物理上不可能发生；SQLite 没有行锁，该子句被静默忽略，正确性退回给 ``seq``
    的 UNIQUE 约束与有界重试（见 ``record_audit``）。两份保证叠起来，两种方言
    都无洞、无分叉。
    """
    stmt = (
        select(AuditChainHead)
        .where(AuditChainHead.chain_id == chain_id)
        .with_for_update()
    )
    head = db.scalar(stmt)
    if head is not None:
        return head

    # 惰性创建：链头在第一次追加时才出现。并发首建靠主键冲突收敛——与
    # ``outbox.enqueue`` 的「保存点吞冲突后重查」是同一范式。
    try:
        with db.begin_nested():
            head = AuditChainHead(
                chain_id=chain_id,
                last_seq=0,
                last_hash=GENESIS_HASH,
                last_anchored_seq=0,
                integrity_status=IntegrityStatus.UNVERIFIED.value,
            )
            db.add(head)
    except IntegrityError:
        head = db.scalar(stmt)
        if head is None:
            raise
    return head


def _maybe_anchor(
    db: Session,
    *,
    chain_id: str,
    seq: int,
    entry_hash: str,
    head: AuditChainHead,
    occurred_at: datetime,
) -> str | None:
    """到期就给这条记录签一个检查点，并推进链头的锚点簿记。

    返回签名信封；未到期返回 ``None``。注意它有副作用（改 ``head``）——签名与
    链头的前进必须同时发生，否则会出现「锚点写了但链头没记」的半截状态。
    """
    if not _anchor_due(seq=seq, head=head, now=occurred_at):
        return None

    prev_anchor_hash = GENESIS_HASH
    if head.last_anchored_seq:
        previous = db.execute(
            select(AuditRecord.entry_hash).where(
                AuditRecord.chain_id == chain_id,
                AuditRecord.seq == head.last_anchored_seq,
            )
        ).scalar()
        if previous:
            prev_anchor_hash = str(previous)

    key = get_signing_key()
    signature = key.sign(
        anchor_bytes(
            chain_id=chain_id,
            seq=seq,
            entry_hash=entry_hash,
            prev_anchor_hash=prev_anchor_hash,
            anchored_at=occurred_at,
        )
    )
    head.last_anchored_seq = seq
    head.last_anchored_at = occurred_at
    return encode_signature(key.kid, signature)


def record_audit(
    db: Session,
    *,
    event_type: str,
    actor: str,
    payload: Any = None,
    project_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
    approval_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    preparation_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
    chain_id: str = AUDIT_CHAIN_ID,
) -> AuditRecord:
    """追加一条证据记录。

    **不提交事务**：提交权归调用方，使业务事实与它的证据同生共死。调用方若不提交，
    这条记录随事务一起消失——那说明业务动作本身也没发生，账本与事实仍然一致。

    载荷在落库前**必过** ``redact_snapshot``（SPEC §8）。脱敏在这里做而不是在各
    调用点，理由与 ``record_event`` 相同：调用点会变多，而凭据一旦沉淀进证据表，
    就是一个不受凭据服务管辖的长期出口。
    """
    head = _lock_head(db, chain_id)
    occurred = as_utc(occurred_at) if occurred_at is not None else datetime.now(UTC)
    safe_payload = redact_snapshot(payload) if payload is not None else None
    qualified = _encode_actor(actor)

    for _ in range(max(1, settings.audit_append_max_attempts)):
        seq = head.last_seq + 1
        prev_hash = head.last_hash
        entry_hash = compute_entry_hash(
            chain_id=chain_id,
            seq=seq,
            event_type=event_type,
            actor=qualified,
            project_id=project_id,
            run_id=run_id,
            approval_id=approval_id,
            version_id=version_id,
            preparation_id=preparation_id,
            occurred_at=occurred,
            payload=safe_payload,
            prev_hash=prev_hash,
        )
        try:
            with db.begin_nested():
                record = AuditRecord(
                    seq=seq,
                    event_type=event_type,
                    actor=qualified,
                    prev_hash=prev_hash,
                    entry_hash=entry_hash,
                    payload=safe_payload,
                    project_id=project_id,
                    occurred_at=occurred,
                    run_id=run_id,
                    approval_id=approval_id,
                    version_id=version_id,
                    preparation_id=preparation_id,
                    chain_id=chain_id,
                )
                db.add(record)
                head.last_seq = seq
                head.last_hash = entry_hash
                record.signature = _maybe_anchor(
                    db,
                    chain_id=chain_id,
                    seq=seq,
                    entry_hash=entry_hash,
                    head=head,
                    occurred_at=occurred,
                )
            return record
        except IntegrityError:
            # seq 被别的写入者抢走了（SQLite 下没有行锁，这里是正常的竞争路径）。
            # 重读链头后重算——重算而不是重试插入，因为 prev_hash 也跟着变了。
            db.expire(head)
            head = _lock_head(db, chain_id)

    raise AuditAppendConflict(
        f"证据账本追加在 {settings.audit_append_max_attempts} 次尝试后仍未成功"
    )


def _project_of_capability(db: Session, capability_id: uuid.UUID) -> uuid.UUID | None:
    """从能力令牌反查它所属的项目。

    工具调用只带四维绑定、不带业务概念（见 ``models/capability.py`` 的职责边界），
    所以项目要从准备单绕一次。**查不到不是错误**：令牌可能不是针对某份准备单签发
    的，此时记录仍然成立，只是没有租户归属——不该为了补一个过滤列而拒绝留痕。
    """
    return db.execute(
        select(Preparation.project_id)
        .join(Capability, Capability.preparation_id == Preparation.id)
        .where(Capability.id == capability_id)
    ).scalar()


class LedgerInvocationSink:
    """把副作用工具调用写进证据账本（T-20 的 Broker 接入面）。

    SPEC §10.1 把默认的 ``InMemoryInvocationSink`` 不落库称为「待补的真实缺陷」，
    这里补上。只记参数**指纹**不记原文——``arguments_digest`` 是不可逆的，
    这是 ``tool_broker`` 的既定选择，这里沿用。
    """

    def record(self, invocation: Any, *, db: Session | None = None) -> None:
        if db is None:
            # 没有会话就写不了库。**不抛异常**：Broker 是在 ``finally`` 里调用本方法，
            # 抛出会顶掉真正需要上抛的业务异常，把一次失败的执行伪装成一次留痕失败。
            logger.warning(
                "工具调用留痕缺少数据库会话，本次未写入证据账本 tool=%s",
                getattr(invocation, "tool", "?"),
            )
            return

        record_audit(
            db,
            event_type=EVENT_TOOL_INVOKED,
            actor=str(invocation.subject),
            project_id=_project_of_capability(db, invocation.capability_id),
            payload={
                "tool": invocation.tool,
                "capability_id": str(invocation.capability_id),
                "environment": invocation.environment,
                "artifact": invocation.artifact,
                "arguments_digest": invocation.arguments_digest,
                "side_effect": invocation.side_effect,
                "succeeded": invocation.succeeded,
            },
            occurred_at=invocation.invoked_at,
        )
