"""Prepare 准备单与双维审批（M6，FR-21 ~ FR-24，SPEC §5）。

对应 SPEC §5 的 ``PREPARATION`` 与 ``APPROVAL`` 两个实体。三条硬约束在这里落成结构：

  - ``PREPARATION.version_id`` **UNIQUE** —— 一个冻结版本对应唯一一份准备单。
    准备单是「这次执行到底要做什么」的完整陈述，同一版本产出两份准备单会让
    「审的到底是哪一份」失去唯一答案。
  - ``APPROVAL.(version_id, dimension)`` **UNIQUE** —— 每版本每维度仅一个槽位。
    槽位在 Prepare 时即建出（``decision`` 为空 = 待审），使「谁还没审」可以查询，
    而不是等审批人提交才知道需要几个审批人。
  - ``expires_at`` **非空** —— 审批带时限。超时的审批不再是有效授权，
    Commit 复核时必须拒（FR-25）；准备单过期后重新 Prepare 会重置槽位与时限。

**职责分离（FR-24）不在本表的约束里表达**：它要求「申请人不可是审批人」「同一人
不可占两个槽位」，前者跨 ``PREPARATION.created_by`` 与 ``APPROVAL.approver_id``、
后者跨同版本两个维度的行，都不是单表约束能覆盖的，故由
:mod:`app.services.approval` 在写入前判定。

**风险评级（FR-21）是纯确定性计算**，不调模型（与门禁同样的理由：结论必须可复现、
可解释）。评级依据与理由一并存进 ``risk_reasons``，供审批界面解释「为什么是 P1」。
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.types import JsonColumn, as_utc


class RiskLevel(enum.StrEnum):
    """风险等级（FR-21）。P0 最高，P3 最低。

    P0 / P1 属 PRD 定义的高危操作：未经安全维度审批一律不得执行
    （「策略许可 ≠ 可跳过审批」）。
    """

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class PreparationStatus(enum.StrEnum):
    PENDING = "pending"  # 已生成准备单，双维审批未齐
    APPROVED = "approved"  # 双维通过，可 Commit
    REJECTED = "rejected"  # 任一维度驳回
    COMMITTED = "committed"  # 已 Commit 收口，审批事实冻结
    EXPIRED = "expired"  # 审批超时失效


class ApprovalDimension(enum.StrEnum):
    """双维审批的两个维度（FR-23）—— 数据与安全互相独立、互不替代。"""

    DATA = "data"
    SECURITY = "security"


class ApprovalDecision(enum.StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


APPROVAL_DIMENSION_LABEL = {
    ApprovalDimension.DATA: "数据维度",
    ApprovalDimension.SECURITY: "安全维度",
}


class Preparation(Base, TimestampMixin):
    """执行准备单（FR-22）—— **零外部副作用**。

    本表只由控制面读写：不连数据源、不提交作业、不占用数据面资源。
    「零副作用」是可验证的性质——准备单生成路径上没有任何 Tool Broker 调用。
    """

    __tablename__ = "preparations"
    __table_args__ = (
        UniqueConstraint("version_id", name="uq_preparation_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 冗余 project_id：跨租户查询在 SQL 层直接过滤，不必每次回表 join 版本
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
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    # 生成准备单时版本的指纹。Commit 时与版本当前指纹比对（FR-25）：
    # 二者不一致说明「准备单陈述的制品」与「即将执行的制品」不是同一个。
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    risk_level: Mapped[RiskLevel] = mapped_column(
        Enum(RiskLevel, name="risk_level"), nullable=False
    )
    risk_reasons: Mapped[list] = mapped_column(JsonColumn, nullable=False, default=list)

    # 准备单四项（PRD P08 页面关键元素）
    impact_scope: Mapped[dict] = mapped_column(JsonColumn, nullable=False)
    data_grading: Mapped[dict] = mapped_column(JsonColumn, nullable=False)
    budget: Mapped[dict] = mapped_column(JsonColumn, nullable=False)
    rollback_plan: Mapped[dict] = mapped_column(JsonColumn, nullable=False)

    status: Mapped[PreparationStatus] = mapped_column(
        Enum(PreparationStatus, name="preparation_status"),
        default=PreparationStatus.PENDING,
        nullable=False,
    )
    # 审批时限（SPEC §5：非空）。超时即失效，Commit 复核时拒。
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="preparation",
        cascade="all, delete-orphan",
        order_by="Approval.dimension",
    )

    # -- 领域判定（放在实体上，避免「什么算有效审批」的规则散落到各处）--

    @property
    def is_expired(self) -> bool:
        return as_utc(self.expires_at) <= datetime.now(UTC)

    @property
    def missing_dimensions(self) -> list[str]:
        """尚未构成有效授权的维度标签：未审 / 驳回 / 已超时。

        「已通过但审批本身超时」也算缺失——审批人是在时限内点的通过，
        但 Commit 发生在时限外，那份授权已经不成立了。

        固定按「数据 → 安全」的次序输出，不依赖数据库对枚举列的排序：
        消息顺序会出现在接口响应里，跨方言飘忽会让前端提示忽前忽后。
        """
        now = datetime.now(UTC)
        by_dimension = {approval.dimension: approval for approval in self.approvals}

        missing: list[str] = []
        for dimension, label in APPROVAL_DIMENSION_LABEL.items():
            approval = by_dimension.get(dimension)
            if approval is None or approval.decision is not ApprovalDecision.APPROVED:
                missing.append(label)
            elif as_utc(approval.expires_at) <= now:
                missing.append(f"{label}（审批已超时）")
        return missing


class Approval(Base, TimestampMixin):
    """一个审批槽位（FR-23 / FR-24）。

    ``decision`` 为空表示**待审**：槽位在 Prepare 时建出，审批人提交后填写。
    尚未有人认领时 ``approver_id`` 也为空——「谁审的」只能由审批人自己写入，
    不允许代填。
    """

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("version_id", "dimension", name="uq_approval_version_dimension"),
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

    dimension: Mapped[ApprovalDimension] = mapped_column(
        Enum(ApprovalDimension, name="approval_dimension"), nullable=False
    )
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    decision: Mapped[ApprovalDecision | None] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision"), nullable=True
    )
    comment: Mapped[str | None] = mapped_column(String(512), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 与准备单同步的时限；单独存一份，避免判定「这一维是否还有效」时依赖父行
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    preparation: Mapped["Preparation"] = relationship(back_populates="approvals")

    @property
    def is_pending(self) -> bool:
        return self.decision is None

    @property
    def status(self) -> str:
        """审批槽位状态：``pending`` / ``approved`` / ``rejected``。

        前端按这一个字段分支即可，不必自己把「决定为空」翻译成「待审」。
        """
        return self.decision.value if self.decision is not None else "pending"
