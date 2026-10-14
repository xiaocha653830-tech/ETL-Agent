"""datasource and metadata profile

新增 M2 元数据与数据源服务的两张表（T-03 / T-04）：
  - ``datasources``：连接登记，凭据只存引用与不可逆指纹（FR-03）
  - ``metadata_profiles``：只读探查留档，样本值落库前已脱敏（FR-05 ~ FR-07）

Revision ID: b7c3e91f0d24
Revises: 04175090a9b1
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7c3e91f0d24"
down_revision: str | None = "04175090a9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DATASOURCE_TYPE = sa.Enum(
    "MYSQL", "POSTGRESQL", "ORACLE", "DORIS", "CLICKHOUSE",
    "CSV", "EXCEL", "JSON", "PARQUET", "S3", "REST_API",
    name="datasource_type",
)
_DATASOURCE_STATUS = sa.Enum(
    "REGISTERED", "CONNECTED", "UNREACHABLE", "PROFILED",
    name="datasource_status",
)


def upgrade() -> None:
    op.create_table(
        "datasources",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("type", _DATASOURCE_TYPE, nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("credential_ref", sa.String(length=255), nullable=True),
        sa.Column("credential_fingerprint", sa.String(length=32), nullable=True),
        sa.Column("status", _DATASOURCE_STATUS, nullable=False),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_datasources_project_id"), "datasources", ["project_id"], unique=False
    )

    op.create_table(
        "metadata_profiles",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("datasource_id", sa.UUID(), nullable=False),
        sa.Column("schema_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "sensitivity_tags", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("max_level", sa.String(length=2), nullable=False),
        sa.Column(
            "sampled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("tables_scanned", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(["datasource_id"], ["datasources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_metadata_profiles_datasource_id"),
        "metadata_profiles",
        ["datasource_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_metadata_profiles_datasource_id"), table_name="metadata_profiles")
    op.drop_table("metadata_profiles")
    op.drop_index(op.f("ix_datasources_project_id"), table_name="datasources")
    op.drop_table("datasources")

    # PG 的枚举类型不会随 drop_table 自动消失，须显式清理，否则重跑 upgrade 会报已存在
    _DATASOURCE_STATUS.drop(op.get_bind(), checkfirst=True)
    _DATASOURCE_TYPE.drop(op.get_bind(), checkfirst=True)
