"""quality report

把质量校验的结论从事件快照提升为可查询的事实（T-16，FR-33 / FR-40）。

在此之前，「这次运行分流了多少行、被拒的都是什么原因」只能从 ``run_events`` 里
那条 ``EVENT_PROGRESS`` 的 ``output_snapshot`` 中翻出来——查询要按事件类型过滤再
解析 JSON 载荷。而它本质上是一次运行的**质量结论**，不该活在一条进度事件里。

建表而非给 ``execution_runs`` 加列：报告只在声明了质量规则时才存在，做成独立表
让「没有报告」与「报告为空」天然可区分；且报告字段会随质量能力增长（错误码分布
已经是一列 JSON），塞进运行表会让本就承载租约、发布、回滚的运行事实继续膨胀。

``run_id`` UNIQUE 且 ``ON DELETE CASCADE``：报告是运行的产出物，运行没了它也就
没有意义。

Revision ID: b7f2a4c91e5d
Revises: a1c7f2e4d905
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7f2a4c91e5d"
down_revision: str | None = "a1c7f2e4d905"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "quality_reports",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("source_rows", sa.BigInteger(), nullable=False),
        sa.Column("valid_rows", sa.BigInteger(), nullable=False),
        sa.Column("rejected_rows", sa.BigInteger(), nullable=False),
        sa.Column("error_dist", _JSONB, nullable=False),
        sa.Column("error_table", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(["run_id"], ["execution_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_quality_reports_run_id"), "quality_reports", ["run_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_quality_reports_run_id"), table_name="quality_reports")
    op.drop_table("quality_reports")
