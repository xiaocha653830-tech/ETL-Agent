"""preparation and approval

新增 M6 审批与授权服务的两张表（T-11，FR-21 ~ FR-24）：

  - ``preparations``：Prepare 准备单。一个冻结版本对应唯一一份（``version_id`` UNIQUE），
    内容是审批台要判断的四项——影响范围 / 数据分级 / 执行预算 / 回滚方案。
    准备单生成**零外部副作用**（FR-22），因此它只是控制面的一次写库。
  - ``approvals``：双维审批槽位。``(version_id, dimension)`` UNIQUE —— 每版本每维度
    只有一个槽位，避免「审了两遍」产生两个互相矛盾的有效审批。

**为什么槽位先建、决定后填**：``decision`` 可空表示待审。若等审批人提交才插入行，
「这一维还差谁」就只能靠遍历用户角色去猜；先建槽位让「缺哪一维」变成一次查询。

**为什么审批也要有 ``expires_at`` 且非空**：审批是对「此刻这份制品」的背书。
没有时限的话，一份半年前的审批记录仍然能授权执行今天的方案，
而这期间数据源、Schema、算子的语义都可能已经变了（SPEC §5 要求非空）。

Revision ID: 39305a87081d
Revises: 3b8f1c07d5a2
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "39305a87081d"
down_revision: str | None = "3b8f1c07d5a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RISK_LEVEL = sa.Enum("P0", "P1", "P2", "P3", name="risk_level")
_PREPARATION_STATUS = sa.Enum(
    "PENDING", "APPROVED", "REJECTED", "COMMITTED", "EXPIRED", name="preparation_status"
)
_APPROVAL_DIMENSION = sa.Enum("DATA", "SECURITY", name="approval_dimension")
_APPROVAL_DECISION = sa.Enum("APPROVED", "REJECTED", name="approval_decision")

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "preparations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("risk_level", _RISK_LEVEL, nullable=False),
        sa.Column("risk_reasons", _JSONB, nullable=False),
        sa.Column("impact_scope", _JSONB, nullable=False),
        sa.Column("data_grading", _JSONB, nullable=False),
        sa.Column("budget", _JSONB, nullable=False),
        sa.Column("rollback_plan", _JSONB, nullable=False),
        sa.Column("status", _PREPARATION_STATUS, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["pipeline_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", name="uq_preparation_version"),
    )
    op.create_index(
        op.f("ix_preparations_project_id"), "preparations", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_preparations_version_id"), "preparations", ["version_id"], unique=False
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("preparation_id", sa.UUID(), nullable=False),
        sa.Column("dimension", _APPROVAL_DIMENSION, nullable=False),
        sa.Column("approver_id", sa.UUID(), nullable=True),
        sa.Column("decision", _APPROVAL_DECISION, nullable=True),
        sa.Column("comment", sa.String(length=512), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["approver_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["preparation_id"], ["preparations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["pipeline_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version_id", "dimension", name="uq_approval_version_dimension"),
    )
    op.create_index(
        op.f("ix_approvals_preparation_id"), "approvals", ["preparation_id"], unique=False
    )
    op.create_index(
        op.f("ix_approvals_project_id"), "approvals", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_approvals_version_id"), "approvals", ["version_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_approvals_version_id"), table_name="approvals")
    op.drop_index(op.f("ix_approvals_project_id"), table_name="approvals")
    op.drop_index(op.f("ix_approvals_preparation_id"), table_name="approvals")
    op.drop_table("approvals")

    op.drop_index(op.f("ix_preparations_version_id"), table_name="preparations")
    op.drop_index(op.f("ix_preparations_project_id"), table_name="preparations")
    op.drop_table("preparations")

    bind = op.get_bind()
    _APPROVAL_DECISION.drop(bind, checkfirst=True)
    _APPROVAL_DIMENSION.drop(bind, checkfirst=True)
    _PREPARATION_STATUS.drop(bind, checkfirst=True)
    _RISK_LEVEL.drop(bind, checkfirst=True)
