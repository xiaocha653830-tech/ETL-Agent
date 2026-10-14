"""run operations

新增幂等台账 ``run_operations``（T-14，FR-27，SPEC §5 ``RUN_OPERATION``）。

``(run_id, operation_key)`` UNIQUE 把「同一个副作用只发生一次」变成数据库层面的
承诺。适用对象是那些重复执行代价很大的动作——向数据面提交作业、在目标库建影子表。
它们重做一次可能是幂等的（同名作业被拒），也可能不是（又一次全量写入），
台账把两种情形统一成「只做一次」。

``FAILED`` **不是可重试标记**，而是「已经尝试过且失败了」的记录：对副作用而言，
「试过」本身就是事实，而重放是否安全取决于目标端留下了什么残留——
那需要人看一眼，不是自动化该猜的。

``result`` 保存的是 ``{"value": ...}`` 形式的**脱敏投影**：进程若死在「副作用已发生」
与「终态已写入」之间，下一轮凭它可以按同样的结论收尾，而不必重做副作用。

Revision ID: 630e9c2d3ace
Revises: 6cf5b19989b8
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "630e9c2d3ace"
down_revision: str | None = "6cf5b19989b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OPERATION_STATUS = sa.Enum("STARTED", "SUCCEEDED", "FAILED", name="run_operation_status")

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "run_operations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("operation_key", sa.String(length=128), nullable=False),
        sa.Column("status", _OPERATION_STATUS, nullable=False),
        sa.Column("result", _JSONB, nullable=True),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("run_id", "operation_key", name="uq_run_operation"),
    )
    op.create_index(
        op.f("ix_run_operations_run_id"), "run_operations", ["run_id"], unique=False
    )
    op.create_index(
        op.f("ix_run_operations_status"), "run_operations", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_run_operations_status"), table_name="run_operations")
    op.drop_index(op.f("ix_run_operations_run_id"), table_name="run_operations")
    op.drop_table("run_operations")

    _OPERATION_STATUS.drop(op.get_bind(), checkfirst=True)
