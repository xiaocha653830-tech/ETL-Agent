"""证据账本（M11，FR-39 / FR-40）。

对应 SPEC §5 的 ``AUDIT_RECORD``。它和 ``run_events`` 是两张不同的表，回答
两个不同的问题：

- ``RUN_EVENT``：**当时发生了什么** —— 时间线，也是度量回传与可观测的载体；
- ``AUDIT_RECORD``：**有人事后改过它吗** —— 带 ``seq`` / ``prev_hash`` /
  ``entry_hash`` 的哈希链，外加按批的签名检查点。

SPEC 已冻结本实体的字段与约束（``seq`` UNIQUE 递增、``prev_hash`` 指向上一条
``entry_hash``，§5），此处按它实现，并补上实现必需的几列——**每一列的理由写在
字段上方**，与 ``models/capability.py`` 的「与 SPEC 差异 + 理由」体例一致。
"""

import enum
import hashlib
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import JsonColumn

# 单链期的链标识。它参与哈希材料，将来若要分链，只需换值而不必改哈希配方。
AUDIT_CHAIN_ID = "default"

# 创世哈希：链上第一条记录的 ``prev_hash``。
#
# 不用空串或全零——空串会让「首条的 prev_hash 被清空」与「正常的首条」不可区分；
# 固定常量还让离线复算方无需信任数据库就知道链从哪里开始。带域分隔串是为了
# 避免它与任何真实记录的哈希偶合。
GENESIS_HASH = hashlib.sha256(b"etl-agent.audit.genesis.v1").hexdigest()


class IntegrityStatus(enum.StrEnum):
    """链的完整性状态。

    ``INDETERMINATE`` 与 ``BREACHED`` 必须分开：前者是「验不了」（例如签名用的
    密钥已轮换出去、公钥不在手边），后者是「验了，不通过」。把两者合并会让一次
    正常的密钥轮转显示成一次篡改告警。
    """

    EMPTY = "empty"  # 链上还没有任何记录——「没有证据」不等于「证据通过校验」
    UNVERIFIED = "unverified"  # 尚未校验过
    VERIFIED = "verified"  # 校验通过
    BREACHED = "breached"  # 校验不通过——链被改过
    INDETERMINATE = "indeterminate"  # 哈希链完好，但签名无法验证


class AuditRecord(Base, TimestampMixin):
    """一条被哈希链保护的证据。**只增不改**——本表没有 UPDATE 路径。

    ``created_at`` / ``updated_at`` 来自 ``TimestampMixin``，与其余模型一致；但
    **它们不参与哈希**（见 ``occurred_at``）。附带一个廉价线索：正常路径下
    ``updated_at`` 恒等于 ``created_at``，两者不等意味着这行被 UPDATE 过——
    校验器把它作为**非致命提示**报告，不作为断链判据。
    """

    __tablename__ = "audit_records"
    __table_args__ = (
        UniqueConstraint("seq", name="uq_audit_records_seq"),
        Index("ix_audit_records_project_seq", "project_id", "seq"),
        Index("ix_audit_records_run_seq", "run_id", "seq"),
        Index("ix_audit_records_approval_seq", "approval_id", "seq"),
        Index("ix_audit_records_occurred_at", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # ---- SPEC §5 冻结字段 ----
    # 全局唯一递增。刻意**不设为主键**：主键语义会把「缺号」变成一种合法的状态，
    # 而链上出现缺号恰恰是必须被看见的事。它是普通 UNIQUE 列，因此洞是异态。
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 主体标识。**不用外键**：主体可能是用户、Worker 进程或维护任务，跨三类主体
    # 无法用一张表的外键表达，强做只会造出几个可空列。
    # 取值形如 ``user:<uuid>`` / ``worker:<uuid>`` / ``system:<name>``。
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 仅签名检查点（锚点）非空（SPEC Q5：哈希链逐条 + 签名按批）。用 NULL 表达
    # 「这一条不是锚点」，而不是给非锚点填一个空串——空串是一种值，NULL 是一种状态。
    signature: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # ---- 实现必需的补充列 ----
    # 证据**内容**。账本必须是可读的证据（PRD 第 7 章要求审计人员看到完整审批链与
    # 执行记录）。若靠 join 业务表还原，改一张业务表就能改变「报告所见」而哈希链
    # 照样通过——内容内联进被哈希的记录，内容才与哈希绑定。
    # 落库前必过 ``redact_snapshot``（SPEC §8）。
    payload: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)

    # 租户过滤列（SPEC §5 关键词表：业务表外键带 ``project_id``）。
    # 允许为 NULL 的唯一原因：工具 Broker 的留痕无法从 ``ToolInvocation`` 反推项目。
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )

    # 被哈希的时间。**不能复用 ``created_at``**：PostgreSQL 的 ``now()`` 是事务
    # 开始时间、SQLite 的 ``CURRENT_TIMESTAMP`` 只有秒精度且不带时区，两者落到
    # 哈希材料里字节不同，跨方言就无法逐字节复算。改由应用侧统一生成 UTC。
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # 账本与业务对象之间的**引用**列（SPEC §5 的关系边：EXECUTION_RUN / APPROVAL）。
    # **刻意不建外键**：
    #   - ``ondelete=CASCADE`` 会把「删掉运行」变成「删掉运行的全部证据」，可直接清痕；
    #   - ``ondelete=SET NULL`` 则是数据库层对一行不可变记录的合法改写。
    # 两者都与 FR-39 冲突。证据应当比它引用的对象活得久。
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    preparation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    # 链标识，参与哈希材料。单链期恒为 ``AUDIT_CHAIN_ID``；写进材料是为了让一条
    # 记录自带「我属于哪条链」，将来分链时不必回头改哈希配方（一改就全链报错）。
    chain_id: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AUDIT_CHAIN_ID
    )


class AuditChainHead(Base, TimestampMixin):
    """每条链的链头 —— 既是写入的串行化点，也是**防尾部截断**的锚。

    没有这张表时，只删掉链尾若干条（含最后一个锚点）会留下一条**完全自洽的短链**：
    每条记录的 ``prev_hash`` 都对得上，签名也验得过，任何记录内部都发现不了。
    把 ``last_seq`` / ``last_hash`` 独立放在这一行之后，校验器只要比对「表内
    ``max(seq)`` 与末条 ``entry_hash``」和「链头」，截断与绕过追加路径的插入就
    立刻暴露——除非篡改者连这一行也改，而那会让 ``last_hash`` 又对不上末条记录，
    除非他能用私钥重签一整条伪造链。
    """

    __tablename__ = "audit_chain_heads"

    chain_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 已写入的最大 seq。空链为 0（seq 从 1 起）。
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 末条记录的 entry_hash。空链取创世哈希。
    last_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default=GENESIS_HASH
    )

    # 最近一次锚点的位置与时刻。**按时间锚定**需要它来判断「距上次锚定多久了」——
    # 只有「每 100 条」的话，一个低频项目可能永远等不到第一个检查点。
    last_anchored_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_anchored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 最近一次校验的结论。``breached`` 会硬阻断 Commit 与回滚（见 audit_verifier），
    # 且**只能由一次成功的完整校验解除**——不做人工 ack 接口，审计面保持全只读。
    integrity_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IntegrityStatus.UNVERIFIED.value
    )
    breach_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    breach_detected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditVerification(Base, TimestampMixin):
    """一次链校验的结论。

    **不能写进它正在校验的那条链**：往链里插一条「链是好的」并不能证明链是好的，
    还会把校验动作本身变成链的一部分（于是校验器要校验自己，无解）。所以校验历史
    自成一表。

    它同时是 PRD 第 7 章「签名检查点状态」的展示来源，也是「系统确实在定期巡检」
    这件事的证据本身。
    """

    __tablename__ = "audit_verifications"
    __table_args__ = (Index("ix_audit_verifications_chain_created", "chain_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    chain_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # 触发来源：startup / periodic / manual / api
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    checked: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    from_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    to_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    head_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    head_hash: Mapped[str] = mapped_column(String(64), nullable=False, default=GENESIS_HASH)

    anchors_checked: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    anchors_verified: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # 首个失败位置。**这是运维真正要的那个数字**：知道「链在第 12345 条断了」
    # 与只知道「链是坏的」是两件事。
    first_failure_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failures: Mapped[list | None] = mapped_column(JsonColumn, nullable=True)

    # 未锚定的尾部时长（秒）。链完好但太久没锚定属于**告警**而非破链。
    unanchored_tail_seconds: Mapped[float | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
