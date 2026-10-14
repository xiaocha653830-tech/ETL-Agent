"""init identity

T-01 工程骨架的初始迁移：身份与多租户三张表（FR-44）。
``memberships`` 承载项目内角色，是 RBAC 与租户隔离的判定依据。

Revision ID: 04175090a9b1
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "04175090a9b1"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")

_USER_ROLE = sa.Enum(
    "MAKER",
    "CHECKER_DATA",
    "CHECKER_SECURITY",
    "OPERATOR",
    "AUDITOR",
    name="user_role",
)


def _timestamp_columns() -> list[sa.Column]:
    """created_at / updated_at —— 三张表共用，由数据库侧生成。"""
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=_NOW,
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=_NOW,
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_projects_tenant_id"), "projects", ["tenant_id"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)

    op.create_table(
        "memberships",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("role", _USER_ROLE, nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "project_id", name="uq_membership_user_project"),
    )
    op.create_index(
        op.f("ix_memberships_project_id"), "memberships", ["project_id"], unique=False
    )
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_memberships_user_id"), table_name="memberships")
    op.drop_index(op.f("ix_memberships_project_id"), table_name="memberships")
    op.drop_table("memberships")

    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")

    op.drop_index(op.f("ix_projects_tenant_id"), table_name="projects")
    op.drop_table("projects")

    # PG 的枚举类型不随 drop_table 消失，须显式清理
    _USER_ROLE.drop(op.get_bind(), checkfirst=True)
