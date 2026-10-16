"""执行运行与事务性 Outbox（M8，FR-25 ~ FR-29，SPEC §5）。

两张表共同落实 ADR-04：**业务事实与投递在同一事务里落库**。

  - ``execution_runs``：一次受管执行的业务事实。Commit 受理时即创建（状态 ``pending``），
    由 Worker 认领后转 ``running``。租约字段（``worker_lease_owner`` /
    ``lease_expires_at``）是 fencing 的依据：旧 Worker 醒来后条件 UPDATE 影响行数为 0，
    就知道自己已经失去运行权，从而不去写回终态。
  - ``outbox_events``：待投递事件。``idempotency_key`` UNIQUE 使「同一件事只投一次」
    成为数据库层面的承诺，而不是调用方的自觉。

**为什么两条记录要同事务**：Commit 若先写运行再投递，中间失败就会留下
「运行记录存在、任务永远不被执行」的静默死单；若先投递再写运行，则可能出现
「任务开始执行、运行记录还不存在」的孤儿执行。ADR-04 选择把二者放进同一事务，
代价是投递有秒级延迟——这个代价是可以接受的，而两种撕裂都不可以。

``capability_id`` 指向本次执行所依据的能力令牌。令牌本身**不落在这里**：
它是可被消费的凭据，只随 Outbox 载荷交给执行层，不进入运行事实表。
"""

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.types import JsonColumn

if TYPE_CHECKING:
    from app.models.quality import QualityReport


class ExecutionRunStatus(enum.StrEnum):
    PENDING = "pending"  # 已受理，等待调度认领
    RUNNING = "running"  # 已被 Worker 认领并持有租约
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"  # 待投递
    DISPATCHED = "dispatched"  # 已交给执行层
    FAILED = "failed"  # 重试耗尽，需人工介入


class RunOperationStatus(enum.StrEnum):
    STARTED = "started"  # 已开始，尚未见到结果（进程中途死亡会停在这里）
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # 已尝试且失败：不允许自动重放


class ExecutionRun(Base, TimestampMixin):
    """一次受管执行（FR-29）。"""

    __tablename__ = "execution_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("pipeline_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("preparations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 令牌记录被清理时运行事实仍须留存 —— 运行是业务事实，令牌只是凭证
    capability_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("capabilities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    status: Mapped[ExecutionRunStatus] = mapped_column(
        Enum(ExecutionRunStatus, name="execution_run_status"),
        default=ExecutionRunStatus.PENDING,
        nullable=False,
        index=True,
    )
    # 令牌绑定的环境与制品在此冗余一份：运行事实必须能独立回答
    # 「这次执行是在哪个环境、对哪份制品授权的」，不必回查令牌记录
    environment: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    # ---- 租约 fencing（FR-29 / SPEC §8「Worker 心跳失败不伪造成功」）----
    worker_lease_owner: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- 协作式取消（FR-29）：Worker 自己检查该标志，不做强杀 ----
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- 运行度量（FR-30 状态回传）----
    read_rows: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    written_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    rejected_rows: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- 发布事实（FR-34 / FR-38）----
    # 影子表是否已被换到正式表名下。**这是受管回滚的安全依据**：`swap=true` 之后，
    # 影子表名下躺着的是被换下来的旧正式表，此时「丢弃影子表残留」就不再是无害的
    # 清理，而是删掉上一版生产数据。回滚必须先问过这件事，因此它必须是一个可查询
    # 的事实，而不是埋在事件快照里的一句话。
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 受管回滚的完成时刻（FR-38）。与幂等台账并存：台账保证副作用至多发生一次，
    # 这个字段让运行列表直接回答「这次运行回滚过没有」，不必回表查台账。
    rolled_back_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    events: Mapped[list["RunEvent"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="RunEvent.created_at",
    )
    # 质量报告（T-16）。一对一的依据是 ``quality_reports.run_id`` 上的 UNIQUE；
    # 它只在声明了质量规则时才存在，所以这里是可空的——「没有报告」与
    # 「报告显示 0 行被拒」是两件事（前者是没声明规则，后者是规则跑了且都通过）。
    quality_report: Mapped["QualityReport | None"] = relationship(
        back_populates="run",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            ExecutionRunStatus.SUCCEEDED,
            ExecutionRunStatus.FAILED,
            ExecutionRunStatus.CANCELLED,
        )


class RunEvent(Base, TimestampMixin):
    """运行事件时间线（FR-29 / FR-30，SPEC §5 ``RUN_EVENT``）。

    **只增不改**：事件表回答的是「当时发生了什么」，允许改写就等于允许事后修饰叙述。
    运行状态是快照（现在怎么样），事件是流水（一路怎么过来的），二者不可互相替代——
    只有状态的话，一次失败运行最后只剩一句 ``error``，看不到它曾经进展到哪一步。

    快照字段在写入前**必过脱敏**（SPEC §8），见 ``app/services/redaction.py``。
    """

    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_created", "run_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(String(512), nullable=True)

    input_snapshot: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)
    output_snapshot: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)
    # 运行度量（吞吐 / 行数 / 拒绝率）经此处回传（SPEC §8）
    metrics: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)

    # 全链路追踪标识（SPEC §8 可观测：trace_id / span_id 落 RUN_EVENT）
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped["ExecutionRun"] = relationship(back_populates="events")


class RunOperation(Base, TimestampMixin):
    """幂等台账（FR-27，SPEC §5 ``RUN_OPERATION``）。

    ``(run_id, operation_key)`` UNIQUE 让「同一个副作用只发生一次」成为数据库
    层面的承诺。适用场景是那些**重复执行代价很大**的动作：向数据面提交作业、
    在目标库建影子表。这些动作重做一次可能是幂等的（同名作业被拒），
    也可能不是（又一次全量写入），台账把它们统一变成「只做一次」。

    ``FAILED`` **不是可重试标记**：它记录的是「已经尝试过且失败了」。
    对副作用而言，「试过」本身就是事实——重放需要人工判断目标端是否留下残留，
    不是自动化该做的决定。
    """

    __tablename__ = "run_operations"
    __table_args__ = (
        UniqueConstraint("run_id", "operation_key", name="uq_run_operation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[RunOperationStatus] = mapped_column(
        Enum(RunOperationStatus, name="run_operation_status"),
        default=RunOperationStatus.STARTED,
        nullable=False,
        index=True,
    )
    result: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OutboxEvent(Base, TimestampMixin):
    """待投递事件（FR-28）。

    ``idempotency_key`` 是投递幂等的唯一依据：写入方用一个稳定的逻辑键
    （对执行任务即 ``execution-run:<run_id>``）声明「这件事只应发生一次」，
    重复插入会被唯一约束挡下，而不是靠投递方去重。
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        # 分发任务按「到期 + 待投递」扫描，复合索引让这次扫描不必回表排序
        Index("ix_outbox_status_available_at", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JsonColumn, nullable=False)

    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status"),
        default=OutboxStatus.PENDING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 下次可投递时刻：投递失败时按指数退避后移，避免失败事件占满扫描窗口
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
