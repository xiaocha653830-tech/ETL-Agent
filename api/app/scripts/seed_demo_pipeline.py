"""开发环境固定方案种子：仅生成草稿，审批与执行仍必须走公开 API。"""

import argparse
import re
import uuid

from sqlalchemy import select

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.datasource import Datasource, DatasourceType, MetadataProfile
from app.models.execution import ExecutionRun, ExecutionRunStatus
from app.models.identity import Membership, User, UserRole
from app.models.version import PipelineVersion
from app.services.config_renderer import render_seatunnel_config
from app.services.endpoint_context import EndpointContext
from app.services.versioning import create_draft

_DEMO_TABLE = re.compile(r"ods_order_wide__formal_pipeline_demo_[A-Za-z0-9_]{1,24}\Z")


def _latest_profile(db, datasource_id: uuid.UUID) -> MetadataProfile | None:
    return db.scalar(
        select(MetadataProfile)
        .where(MetadataProfile.datasource_id == datasource_id)
        .order_by(MetadataProfile.sampled_at.desc())
        .limit(1)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="固定 MySQL→Doris 方案草稿种子")
    parser.add_argument("source_id", type=uuid.UUID)
    parser.add_argument("target_id", type=uuid.UUID)
    parser.add_argument("target_table")
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="仅当该目标的既有演示运行全部失败/取消时，新建版本重试",
    )
    args = parser.parse_args()
    if settings.app_env not in ("dev", "demo"):
        parser.error("固定方案种子仅允许开发/演示环境使用")
    if not _DEMO_TABLE.fullmatch(args.target_table):
        parser.error("目标必须是独立的 ods_order_wide__formal_pipeline_demo_* 表")

    with SessionLocal() as db:
        source = db.get(Datasource, args.source_id)
        target = db.get(Datasource, args.target_id)
        maker = db.scalar(select(User).where(User.email == "maker@example.com"))
        if (
            source is None or target is None or maker is None
            or source.type is not DatasourceType.MYSQL
            or target.type is not DatasourceType.DORIS
            or source.project_id != target.project_id
        ):
            parser.error("数据源或演示账号不存在、类型不匹配、跨项目")
        membership = db.scalar(
            select(Membership).where(
                Membership.project_id == source.project_id,
                Membership.user_id == maker.id,
                Membership.role == UserRole.MAKER,
            )
        )
        if membership is None:
            parser.error("演示工程师不是该项目的 Maker")
        existing = list(db.scalars(
            select(PipelineVersion).where(PipelineVersion.project_id == source.project_id)
        ))
        matching = [
            version for version in existing
            if (version.etl_plan or {}).get("target_table") == args.target_table
        ]
        if matching and not args.retry_failed:
            version = matching[-1]
            print(f"已存在演示版本 id={version.id} v{version.version_no}，不重复创建")
            return
        if matching:
            runs = list(db.scalars(
                select(ExecutionRun).where(
                    ExecutionRun.version_id.in_([version.id for version in matching])
                )
            ))
            # 「没有任何运行记录」与「运行全部失败」同等放行：前者意味着这个目标
            # **从未被执行过**（典型情形是草稿只被门禁拒绝过），不存在重复写入的
            # 风险。把这种情况一并拒掉，只会让一次门禁失败永久占住一个目标表名。
            if any(
                run.status not in (ExecutionRunStatus.FAILED, ExecutionRunStatus.CANCELLED)
                for run in runs
            ):
                parser.error("仅允许对已失败/取消且无活跃运行的演示目标重建版本")

        source_profile = _latest_profile(db, source.id)
        target_profile = _latest_profile(db, target.id)
        columns = ("order_id", "user_name", "order_amount", "order_date")
        plan = {
            "name": "演示：MySQL 订单到 Doris 受管发布",
            "description": "固定方案仅用于验证 T-15/T-16；不代替自然语言模型验收",
            "source_datasource_id": str(source.id),
            "target_datasource_id": str(target.id),
            "source_table": "orders",
            "target_table": args.target_table,
            "steps": [
                {
                    "step_id": "source_jdbc",
                    "operator_id": "seatunnel.source.Jdbc",
                    "role": "source",
                    "attributes": {
                        "driver": "com.mysql.cj.jdbc.Driver",
                        "query": "SELECT order_id, user_name, order_amount, order_date FROM orders",
                    },
                },
                {
                    "step_id": "sink_doris",
                    "operator_id": "seatunnel.sink.Doris",
                    "role": "sink",
                    "attributes": {"database": "ods", "table": args.target_table},
                },
            ],
            "field_mappings": [
                {"source_field": name, "target_field": name, "transform": None,
                 "sensitivity_level": "L2"}
                for name in columns
            ],
            "quality_rules": [],
        }
        source_context = EndpointContext(
            datasource_id=str(source.id), name=source.name, type=source.type.value,
            config=source.config, tables=[],
        )
        target_context = EndpointContext(
            datasource_id=str(target.id), name=target.name, type=target.type.value,
            config=target.config, tables=[],
        )
        config = render_seatunnel_config(
            plan=plan, source=source_context, target=target_context,
            run_id=str(uuid.uuid4()),
        )
        version = create_draft(
            db,
            project_id=source.project_id,
            run_id=None,
            created_by=maker.id,
            plan=plan,
            config=config,
            quality_rules=[],
            source_profile_id=source_profile.id if source_profile else None,
            target_profile_id=target_profile.id if target_profile else None,
        )
        print(
            f"草稿版本 id={version.id} v{version.version_no}；"
            "后续须经质量规则、冻结、双审和 Commit"
        )


if __name__ == "__main__":
    main()
