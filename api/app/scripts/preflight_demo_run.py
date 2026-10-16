"""只读预检准备单的运行时配置；不签令牌、不提交 SeaTunnel 作业。"""

import argparse
import hashlib
import uuid

from app.core.config import settings
from app.db.session import SessionLocal
from app.models.approval import Preparation
from app.models.version import PipelineVersion
from app.schemas.execution import ExecutionCommand
from app.services.commit import EXECUTION_TOOL
from app.services.credentials import get_credential_service
from app.services.quality import compile_quality_config, error_table_name
from app.services.seatunnel import render_runtime_config


def main() -> None:
    parser = argparse.ArgumentParser(description="受管作业冻结制品与运行时注入预检")
    parser.add_argument("preparation_id", type=uuid.UUID)
    args = parser.parse_args()
    if settings.app_env not in ("dev", "demo"):
        parser.error("预检脚本仅用于开发/演示环境")

    with SessionLocal() as db:
        preparation = db.get(Preparation, args.preparation_id)
        if preparation is None:
            parser.error("准备单不存在")
        version = db.get(PipelineVersion, preparation.version_id)
        if version is None or not version.sha256:
            parser.error("冻结版本不存在")
        command = ExecutionCommand(
            run_id=uuid.uuid4(),
            version_id=version.id,
            preparation_id=preparation.id,
            capability_token="preflight-only",
            capability_id=uuid.uuid4(),
            tool=EXECUTION_TOOL,
            subject="preflight-only",
            environment=settings.app_env,
            artifact=version.sha256,
            target_table=str(preparation.impact_scope["target_table"]),
            shadow_table=str(preparation.impact_scope["shadow_table"]),
            budget=dict(preparation.budget or {}),
        )
        config = render_runtime_config(
            db, command, credentials=get_credential_service()
        )
        rules = list(version.quality_rules or [])
        compiled = compile_quality_config(
            config,
            source_table=str((version.etl_plan or {}).get("source_table") or ""),
            shadow_table=command.shadow_table,
            rules=rules,
        )
        allowed_placeholder = "${table_name}" if rules else ""
        unresolved = [
            line for line in compiled.splitlines()
            if not line.lstrip().startswith("#")
            and "${" in line.replace(allowed_placeholder, "")
        ]
        if unresolved:
            parser.error("运行时配置仍有未注入的占位符")
        print(
            f"预检通过：version=v{version.version_no} rules={len(rules)} "
            f"shadow={command.shadow_table} "
            f"error={error_table_name(command.shadow_table) if rules else '(无)'} "
            f"config_sha256={hashlib.sha256(compiled.encode('utf-8')).hexdigest()[:12]}"
        )


if __name__ == "__main__":
    main()
