"""capability

新增 M7 安全内核的单次能力令牌表（T-10，FR-26 / FR-27）：
  - ``capabilities``：四维绑定（工具 / 主体 / 环境 / 制品）+ 单次消费标记；
    ``consumed`` 默认 false，签发时 ``expires_at = now + 5min``。

Revision ID: 5d2f8a4c1b90
Revises: b7c3e91f0d24
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5d2f8a4c1b90"
down_revision: str | None = "b7c3e91f0d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capabilities",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("preparation_id", sa.UUID(), nullable=True),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("bound_subject", sa.String(length=255), nullable=False),
        sa.Column("environment", sa.String(length=128), nullable=False),
        sa.Column("bound_artifact", sa.String(length=255), nullable=False),
        sa.Column("kid", sa.String(length=64), nullable=False),
        sa.Column(
            "consumed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_capabilities_preparation_id"),
        "capabilities",
        ["preparation_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_capabilities_preparation_id"), table_name="capabilities")
    op.drop_table("capabilities")
