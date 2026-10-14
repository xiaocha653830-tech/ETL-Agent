"""pipeline runs

新增 M3 需求生成的运行记录表（T-06）：
  - ``pipeline_runs``：生成任务的元数据与结果快照。
    状态机的完整状态由 LangGraph 的 PostgresSaver 自管（不走 Alembic），
    本表只保留列表查询与审计所需的最小字段。
    ``id`` 同时作为 LangGraph 的 ``thread_id``，恢复断点依赖这一点。

Revision ID: 96e30b1eb964
Revises: 5d2f8a4c1b90
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "96e30b1eb964"
down_revision: str | None = "5d2f8a4c1b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_STATUS = sa.Enum(
    "RUNNING", "INTERRUPTED", "SUCCEEDED", "UNSUPPORTED", "FAILED",
    name="pipeline_run_status",
)


def upgrade() -> None:
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("source_datasource_id", sa.UUID(), nullable=True),
        sa.Column("target_datasource_id", sa.UUID(), nullable=True),
        sa.Column("status", _RUN_STATUS, nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("config_snapshot", sa.Text(), nullable=True),
        sa.Column("questions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["source_datasource_id"], ["datasources.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["target_datasource_id"], ["datasources.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_pipeline_runs_project_id"), "pipeline_runs", ["project_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_pipeline_runs_project_id"), table_name="pipeline_runs")
    op.drop_table("pipeline_runs")

    # PG 的枚举类型不随 drop_table 消失，须显式清理，否则重跑 upgrade 会报已存在
    _RUN_STATUS.drop(op.get_bind(), checkfirst=True)
