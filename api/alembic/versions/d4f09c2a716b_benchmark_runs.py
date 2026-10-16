"""平台评测结果（T-21）。

Revision ID: d4f09c2a716b
Revises: c3e8b1a47f92
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4f09c2a716b"
down_revision: str | None = "c3e8b1a47f92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "benchmark_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("dataset_version", sa.String(32), nullable=False),
        sa.Column("dataset_sha256", sa.String(64), nullable=False),
        sa.Column("backend", sa.String(16), nullable=False),
        sa.Column("cases_total", sa.Integer(), nullable=False),
        sa.Column("cases_passed", sa.Integer(), nullable=False),
        sa.Column("operator_accuracy", sa.Float(), nullable=False),
        sa.Column("attribute_accuracy", sa.Float(), nullable=False),
        sa.Column("guardrail_rate", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("cases_total > 0", name="ck_benchmark_nonempty"),
        sa.CheckConstraint(
            "cases_passed >= 0 AND cases_passed <= cases_total", name="ck_benchmark_case_counts"
        ),
        sa.CheckConstraint(
            "operator_accuracy >= 0 AND operator_accuracy <= 1", name="ck_benchmark_operator_ratio"
        ),
        sa.CheckConstraint(
            "attribute_accuracy >= 0 AND attribute_accuracy <= 1",
            name="ck_benchmark_attribute_ratio",
        ),
        sa.CheckConstraint(
            "guardrail_rate >= 0 AND guardrail_rate <= 1", name="ck_benchmark_guardrail_ratio"
        ),
    )
    op.create_index(
        "ix_benchmark_dataset_backend_created",
        "benchmark_runs",
        ["dataset_version", "backend", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_benchmark_dataset_backend_created", table_name="benchmark_runs")
    op.drop_table("benchmark_runs")
