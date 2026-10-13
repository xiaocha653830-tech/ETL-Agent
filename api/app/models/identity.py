"""身份与多租户模型。

对应 PRD：
  - PROJECT / MEMBERSHIP 实体（PRD 第 8 章）
  - 五角色权限矩阵（PRD 第 2 章）
  - 多租户项目隔离（FR-44）
"""

import enum
import uuid

from sqlalchemy import Boolean, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class UserRole(str, enum.Enum):
    """项目内角色 —— 严格对应 PRD 第 2 章的五角色定义。"""

    MAKER = "maker"                      # 数据工程师：发起需求、提交版本，不可自批
    CHECKER_DATA = "checker_data"        # 数据审批人：数据维度审批，不可 Commit
    CHECKER_SECURITY = "checker_security"  # 安全审批人：安全维度审批，不可 Commit
    OPERATOR = "operator"                # 系统操作员：执行 Commit，不可改 Pipeline、不可审批
    AUDITOR = "auditor"                  # 审计人员：全局只读


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Project(Base, TimestampMixin):
    """多租户边界：所有业务实体最终都挂在某个 project 之下。"""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Membership(Base, TimestampMixin):
    """用户 ↔ 项目 的成员关系；角色由此表决定（租户隔离与 RBAC 的判定依据）。"""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_membership_user_project"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False)

    user: Mapped["User"] = relationship(back_populates="memberships")
    project: Mapped["Project"] = relationship(back_populates="memberships")
