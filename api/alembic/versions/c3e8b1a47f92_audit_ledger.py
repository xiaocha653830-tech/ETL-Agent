"""audit ledger

证据账本：哈希链落库（T-20，FR-39 / FR-40）。

三张表必须一起建，它们互为前提才成立：

- ``audit_records`` —— SPEC §5 冻结的 ``AUDIT_RECORD``。规格字段照搬，另补
  ``payload`` / ``project_id`` / ``occurred_at`` / 四个引用列 / ``chain_id``，
  每一列的理由写在模型文件里；
- ``audit_chain_heads`` —— 链头。它不只是一个游标：只删掉链尾若干条会留下一条
  **完全自洽的短链**（每条 ``prev_hash`` 都对得上、签名也验得过），任何记录内部
  都发现不了。把末条的 ``seq`` 与 ``hash`` 独立放在这一行，截断与绕过追加路径
  的插入才暴露得出来。它同时也是并发写入的串行化点；
- ``audit_verifications`` —— 校验历史。校验结论**不能写进它正在校验的那条链**，
  否则校验器要校验自己。

``project_id`` 建外键（SPEC §5 关键词表：业务表外键带 ``project_id``）；而
``run_id`` / ``approval_id`` / ``version_id`` / ``preparation_id`` **刻意不建**——
级联删除会把「删掉运行」变成「删掉运行的全部证据」，可直接清痕；置空则是数据库层
对一行不可变记录的合法改写。两者都与 FR-39 的不可篡改性冲突。

Revision ID: c3e8b1a47f92
Revises: b7f2a4c91e5d
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c3e8b1a47f92"
down_revision: str | None = "b7f2a4c91e5d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    op.create_table(
        "audit_records",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=160), nullable=True),
        sa.Column("payload", _JSONB, nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("approval_id", sa.UUID(), nullable=True),
        sa.Column("version_id", sa.UUID(), nullable=True),
        sa.Column("preparation_id", sa.UUID(), nullable=True),
        sa.Column("chain_id", sa.String(length=32), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("seq", name="uq_audit_records_seq"),
    )
    op.create_index(
        op.f("ix_audit_records_event_type"), "audit_records", ["event_type"]
    )
    op.create_index(
        op.f("ix_audit_records_entry_hash"), "audit_records", ["entry_hash"]
    )
    op.create_index("ix_audit_records_project_seq", "audit_records", ["project_id", "seq"])
    op.create_index("ix_audit_records_run_seq", "audit_records", ["run_id", "seq"])
    op.create_index(
        "ix_audit_records_approval_seq", "audit_records", ["approval_id", "seq"]
    )
    op.create_index("ix_audit_records_occurred_at", "audit_records", ["occurred_at"])

    op.create_table(
        "audit_chain_heads",
        sa.Column("chain_id", sa.String(length=32), nullable=False),
        sa.Column("last_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_hash", sa.String(length=64), nullable=False),
        sa.Column("last_anchored_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_anchored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("integrity_status", sa.String(length=16), nullable=False),
        sa.Column("breach_seq", sa.BigInteger(), nullable=True),
        sa.Column("breach_detected_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("chain_id"),
    )

    op.create_table(
        "audit_verifications",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("chain_id", sa.String(length=32), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("checked", sa.BigInteger(), nullable=False),
        sa.Column("from_seq", sa.BigInteger(), nullable=True),
        sa.Column("to_seq", sa.BigInteger(), nullable=True),
        sa.Column("head_seq", sa.BigInteger(), nullable=False),
        sa.Column("head_hash", sa.String(length=64), nullable=False),
        sa.Column("anchors_checked", sa.BigInteger(), nullable=False),
        sa.Column("anchors_verified", sa.BigInteger(), nullable=False),
        sa.Column("first_failure_seq", sa.BigInteger(), nullable=True),
        sa.Column("failures", _JSONB, nullable=True),
        sa.Column("unanchored_tail_seconds", sa.Float(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_verifications_chain_created",
        "audit_verifications",
        ["chain_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_verifications_chain_created", table_name="audit_verifications"
    )
    op.drop_table("audit_verifications")

    op.drop_table("audit_chain_heads")

    op.drop_index("ix_audit_records_occurred_at", table_name="audit_records")
    op.drop_index("ix_audit_records_approval_seq", table_name="audit_records")
    op.drop_index("ix_audit_records_run_seq", table_name="audit_records")
    op.drop_index("ix_audit_records_project_seq", table_name="audit_records")
    op.drop_index(op.f("ix_audit_records_entry_hash"), table_name="audit_records")
    op.drop_index(op.f("ix_audit_records_event_type"), table_name="audit_records")
    op.drop_table("audit_records")
