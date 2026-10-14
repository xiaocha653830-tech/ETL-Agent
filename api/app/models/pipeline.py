"""需求生成运行记录（M3，FR-08 ~ FR-14）。

只存**运行元数据与结果快照**：状态机的完整状态放在 LangGraph 的
checkpoint 表里（由 PostgresSaver 自管），这里保留列表查询与审计需要的最小字段。

``id`` 同时也是 LangGraph 的 ``thread_id`` —— 二者必须一致，
否则恢复（API-04）会找不到断点。
"""

import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import JsonColumn


class PipelineRunStatus(enum.StrEnum):
    RUNNING = "running"  # 正在生成
    INTERRUPTED = "interrupted"  # 澄清中断，等待补充参数（FR-09）
    SUCCEEDED = "succeeded"  # 已产出 EtlPlan 与配置候选
    UNSUPPORTED = "unsupported"  # 受管算子清单无法实现（FR-14），属业务结论而非故障
    FAILED = "failed"


class PipelineRun(Base, TimestampMixin):
    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    source_datasource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("datasources.id", ondelete="SET NULL"), nullable=True
    )
    target_datasource_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("datasources.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[PipelineRunStatus] = mapped_column(
        Enum(PipelineRunStatus, name="pipeline_run_status"),
        default=PipelineRunStatus.RUNNING,
        nullable=False,
    )
    # EtlPlan 快照（尚未冻结，冻结后另存 PipelineVersion —— 见 T-09）
    plan: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)
    # SeaTunnel 配置候选（HOCON 文本）
    config_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 待澄清问题（FR-09）
    questions: Mapped[list | None] = mapped_column(JsonColumn, nullable=True)
    error: Mapped[str | None] = mapped_column(String(512), nullable=True)
