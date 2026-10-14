"""pipeline versions

新增 M5 门禁与版本服务的不可变版本表（T-08 / T-09）：
  - ``pipeline_versions``：方案 + 配置 + 质量规则的冻结快照。
    关键约束沿用 SPEC §5：``sha256`` UNIQUE（同样内容不重复落库）、
    ``(project_id, version_no)`` UNIQUE（项目内版本号递增）。
    **冻结后禁止 UPDATE** 由应用层强制（见 ``app/services/versioning.py``）。

Revision ID: 9c1e94ab5942
Revises: 96e30b1eb964
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9c1e94ab5942"
down_revision: str | None = "96e30b1eb964"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VERSION_STATUS = sa.Enum("DRAFT", "FROZEN", "REJECTED", name="version_status")


def upgrade() -> None:
    op.create_table(
        "pipeline_versions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("status", _VERSION_STATUS, nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etl_plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config", sa.Text(), nullable=False),
        sa.Column("quality_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("gate_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "version_no", name="uq_version_project_no"),
    )
    op.create_index(
        op.f("ix_pipeline_versions_project_id"),
        "pipeline_versions",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_pipeline_versions_run_id"), "pipeline_versions", ["run_id"], unique=False
    )
    # sha256 唯一：内容完全相同的版本不得重复入库
    op.create_index(
        op.f("ix_pipeline_versions_sha256"), "pipeline_versions", ["sha256"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_pipeline_versions_sha256"), table_name="pipeline_versions")
    op.drop_index(op.f("ix_pipeline_versions_run_id"), table_name="pipeline_versions")
    op.drop_index(op.f("ix_pipeline_versions_project_id"), table_name="pipeline_versions")
    op.drop_table("pipeline_versions")

    _VERSION_STATUS.drop(op.get_bind(), checkfirst=True)
