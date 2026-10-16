"""run publication and rollback

给 ``execution_runs`` 补两个事实列（T-18，FR-34 / FR-38）：

  - ``published_at``：影子表是否已被换到正式表名下。**这是受管回滚的安全依据**——
    ``swap=true`` 之后影子表名下躺着的是被换下来的旧正式表，此时「丢弃影子表残留」
    会变成删掉上一版生产数据。回滚要能先问过这件事，所以它必须是列而不是日志里
    的一句话。
  - ``rolled_back_at``：残留是否已清理。让运行列表直接回答「回滚过没有」，
    不必回表查 ``run_operations`` 台账。

两列都可空且无默认值：**「不知道」不能与「没有」混为一谈**——历史运行没有这两列
的信息，回填成 false/epoch 会凭空造出「确定没发布过」的假结论，而回滚正是据此
决定要不要 ``DROP TABLE``。

Revision ID: a1c7f2e4d905
Revises: 630e9c2d3ace
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c7f2e4d905"
down_revision: str | None = "630e9c2d3ace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_runs",
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "execution_runs",
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("execution_runs", "rolled_back_at")
    op.drop_column("execution_runs", "published_at")
