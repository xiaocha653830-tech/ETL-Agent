"""run events

新增运行事件时间线 ``run_events``（T-13，FR-29 / FR-30，SPEC §5 ``RUN_EVENT``）。

运行状态是快照（现在怎么样），事件是流水（一路怎么过来的）。只有状态的话，
一次失败运行最后只剩一句 ``error``：看不到它被谁认领、提交了什么、进展到哪一步。
排障需要的恰恰是这条线。

**只增不改**：事件回答「当时发生了什么」，允许改写就等于允许事后修饰叙述。

``input_snapshot`` / ``output_snapshot`` / ``metrics`` 三列在写入前**必过脱敏**
（SPEC §8），实现见 ``app/services/redaction.py``——脱敏放在写入路径上而不是
各调用点，因为调用点会随适配器变多。

``(run_id, created_at)`` 复合索引对应时间线的读取方式——按运行取一段按序的流水。

Revision ID: 6cf5b19989b8
Revises: 3db6422a2dbd
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6cf5b19989b8"
down_revision: str | None = "3db6422a2dbd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "run_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.String(length=512), nullable=True),
        sa.Column("input_snapshot", _JSONB, nullable=True),
        sa.Column("output_snapshot", _JSONB, nullable=True),
        sa.Column("metrics", _JSONB, nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("span_id", sa.String(length=64), nullable=True),
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
    op.create_index(op.f("ix_run_events_event_type"), "run_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_run_events_run_id"), "run_events", ["run_id"], unique=False)
    op.create_index("ix_run_events_run_created", "run_events", ["run_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_run_events_run_created", table_name="run_events")
    op.drop_index(op.f("ix_run_events_run_id"), table_name="run_events")
    op.drop_index(op.f("ix_run_events_event_type"), table_name="run_events")
    op.drop_table("run_events")
