"""bind pipeline version to metadata profile

把冻结版本与生成时依据的元数据探查记录绑定（T-08 / T-09 的收口）。

**为什么需要这一步**：在此之前，生成阶段读「最新 Profile」、冻结阶段也读
「最新 Profile」。若两次动作之间重新探查过，就会出现「生成依据 v1、校验依据 v2」
的错配——冻结出来的指纹不再代表当初被审的那份方案，门禁结论也随之失去意义。
绑定之后冻结按记录的 ``profile_id`` 还原 Schema，依据前后一致。

``ondelete=SET NULL``：探查记录随数据源级联删除，但**版本本身必须留存**；
拿不到探查记录时由门禁判 ``failed``（见 ``app/api/routes/versions.py``），
而不是让删除数据源这个动作顺带删掉历史版本。

Revision ID: 3b8f1c07d5a2
Revises: 9c1e94ab5942
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3b8f1c07d5a2"
down_revision: str | None = "9c1e94ab5942"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_versions", sa.Column("source_profile_id", sa.UUID(), nullable=True)
    )
    op.add_column(
        "pipeline_versions", sa.Column("target_profile_id", sa.UUID(), nullable=True)
    )
    # 约束名传 None，交由 PostgreSQL 按默认规则生成，与 autogenerate 的结果一致；
    # 显式命名反而会让后续 autogenerate 认为约束对不上而反复重建。
    op.create_foreign_key(
        None,
        "pipeline_versions",
        "metadata_profiles",
        ["source_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        None,
        "pipeline_versions",
        "metadata_profiles",
        ["target_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "pipeline_versions_target_profile_id_fkey",
        "pipeline_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "pipeline_versions_source_profile_id_fkey",
        "pipeline_versions",
        type_="foreignkey",
    )
    op.drop_column("pipeline_versions", "target_profile_id")
    op.drop_column("pipeline_versions", "source_profile_id")
