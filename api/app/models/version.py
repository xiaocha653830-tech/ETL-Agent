"""不可变版本（M5，FR-18 ~ FR-20）。

对应 SPEC §5 的 ``PIPELINE_VERSION`` 实体，关键约束：
  - ``sha256`` UNIQUE —— 相同内容的版本不会重复落库；
  - **冻结后禁止 UPDATE** —— 由应用层在 :mod:`app.services.versioning` 中强制。

生成成功时先落一条 ``draft``，门禁通过后由 ``freeze`` 转为不可变版本（T-09）。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import JsonColumn


class VersionStatus(enum.StrEnum):
    DRAFT = "draft"  # 已生成，未过门禁
    FROZEN = "frozen"  # 门禁通过并冻结，不可再改（FR-19）
    REJECTED = "rejected"  # 门禁未通过，保留失败原因供修复


class PipelineVersion(Base, TimestampMixin):
    __tablename__ = "pipeline_versions"
    __table_args__ = (
        # 项目内版本号递增，前端展示「v3」这类标识
        UniqueConstraint("project_id", "version_no", name="uq_version_project_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 来源生成任务；任务被清理后版本仍应保留，故为 SET NULL
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status"),
        default=VersionStatus.DRAFT,
        nullable=False,
    )

    # 冻结时计算的内容摘要（FR-19）；draft 阶段为空
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True, index=True)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    etl_plan: Mapped[dict] = mapped_column(JsonColumn, nullable=False)
    config: Mapped[str] = mapped_column(Text, nullable=False)
    quality_rules: Mapped[list] = mapped_column(JsonColumn, nullable=False)

    # 门禁报告（T-08）：逐项结论 + 失败详情，冻结后作为证据留存
    gate_report: Mapped[dict | None] = mapped_column(JsonColumn, nullable=True)

    @property
    def is_immutable(self) -> bool:
        """冻结版本不可修改（FR-19）。"""
        return self.status is VersionStatus.FROZEN
