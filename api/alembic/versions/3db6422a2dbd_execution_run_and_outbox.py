"""execution run and outbox

新增 M8 异步执行层的两张表（T-12 / T-13，FR-25 ~ FR-29）：

  - ``execution_runs``：一次受管执行的业务事实。Commit 受理时即创建（``pending``），
    Worker 认领后转 ``running``。租约三列（``worker_lease_owner`` /
    ``lease_expires_at`` / ``heartbeat_at``）是 fencing 的依据：旧 Worker 醒来后
    条件 UPDATE 影响行数为 0，就知道自己已失去运行权，从而不写回终态。
  - ``outbox_events``：待投递事件。``idempotency_key`` UNIQUE 把「同一件事只投一次」
    变成数据库层面的承诺，而不是调用方的自觉（ADR-04）。

    ``(status, available_at)`` 复合索引对应分发任务的扫描方式——按「到期 + 待投递」
    取批，没有这个索引每次扫描都要回表排序。

**同时给 ``capabilities.preparation_id`` 补上外键**：该列在 T-10 落地时 M6 还不存在，
只能是一个裸 UUID。现在准备单表已就位，补上引用使 SPEC §5 的
``PREPARATION ||--o| CAPABILITY`` 在数据层成立；``SET NULL`` 表示准备单被清理时
令牌记录仍保留（令牌有效性只取决于四维绑定与消费状态，不取决于来源是否还在）。

Revision ID: 3db6422a2dbd
Revises: 39305a87081d
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3db6422a2dbd"
down_revision: str | None = "39305a87081d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTBOX_STATUS = sa.Enum("PENDING", "DISPATCHED", "FAILED", name="outbox_status")
_RUN_STATUS = sa.Enum(
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name="execution_run_status"
)

_JSONB = postgresql.JSONB(astext_type=sa.Text())

# PostgreSQL 为未命名外键生成的默认约束名，downgrade 时必须按名字删除
_CAPABILITY_PREPARATION_FK = "capabilities_preparation_id_fkey"


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", _JSONB, nullable=False),
        sa.Column("status", _OUTBOX_STATUS, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_outbox_events_available_at"), "outbox_events", ["available_at"], unique=False
    )
    op.create_index(
        op.f("ix_outbox_events_event_type"), "outbox_events", ["event_type"], unique=False
    )
    # 幂等键唯一：重复投递在插入时即被拒绝，而不是靠投递方去重
    op.create_index(
        op.f("ix_outbox_events_idempotency_key"),
        "outbox_events",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_outbox_status_available_at",
        "outbox_events",
        ["status", "available_at"],
        unique=False,
    )

    op.create_table(
        "execution_runs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("version_id", sa.UUID(), nullable=False),
        sa.Column("preparation_id", sa.UUID(), nullable=False),
        sa.Column("capability_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("status", _RUN_STATUS, nullable=False),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column("artifact", sa.String(length=64), nullable=False),
        sa.Column("worker_lease_owner", sa.UUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_rows", sa.BigInteger(), nullable=False),
        sa.Column("written_bytes", sa.BigInteger(), nullable=False),
        sa.Column("rejected_rows", sa.BigInteger(), nullable=False),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        # 令牌记录被清理时运行事实仍须留存：运行是业务事实，令牌只是凭证
        sa.ForeignKeyConstraint(["capability_id"], ["capabilities.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["preparation_id"], ["preparations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["pipeline_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_execution_runs_artifact"), "execution_runs", ["artifact"], unique=False
    )
    op.create_index(
        op.f("ix_execution_runs_capability_id"),
        "execution_runs",
        ["capability_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_runs_preparation_id"),
        "execution_runs",
        ["preparation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_execution_runs_project_id"), "execution_runs", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_execution_runs_status"), "execution_runs", ["status"], unique=False
    )
    op.create_index(
        op.f("ix_execution_runs_version_id"), "execution_runs", ["version_id"], unique=False
    )

    # 约束名传 None，交由 PostgreSQL 按默认规则生成（capabilities_preparation_id_fkey），
    # 与 autogenerate 的结果一致；显式命名会让后续 autogenerate 认为约束对不上而反复重建。
    op.create_foreign_key(
        None,
        "capabilities",
        "preparations",
        ["preparation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_CAPABILITY_PREPARATION_FK, "capabilities", type_="foreignkey")

    op.drop_index(op.f("ix_execution_runs_version_id"), table_name="execution_runs")
    op.drop_index(op.f("ix_execution_runs_status"), table_name="execution_runs")
    op.drop_index(op.f("ix_execution_runs_project_id"), table_name="execution_runs")
    op.drop_index(op.f("ix_execution_runs_preparation_id"), table_name="execution_runs")
    op.drop_index(op.f("ix_execution_runs_capability_id"), table_name="execution_runs")
    op.drop_index(op.f("ix_execution_runs_artifact"), table_name="execution_runs")
    op.drop_table("execution_runs")

    op.drop_index("ix_outbox_status_available_at", table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_idempotency_key"), table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_event_type"), table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_available_at"), table_name="outbox_events")
    op.drop_table("outbox_events")

    bind = op.get_bind()
    _RUN_STATUS.drop(bind, checkfirst=True)
    _OUTBOX_STATUS.drop(bind, checkfirst=True)
