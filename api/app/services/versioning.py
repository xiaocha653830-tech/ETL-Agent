"""版本冻结与版本 Diff（M5，FR-19 / FR-20）。

**不可变性**由本模块强制：一旦冻结，任何写操作都会被拒绝。
``sha256`` 指纹是「方案 + 配置 + 质量规则」三者规范化后的摘要——
任何一处改动都会得到不同的指纹，这是 FR-19「不可变版本」的技术基础，
也是后续 Commit 阶段复核制品一致性的依据（FR-25）。
"""

import difflib
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import PermissionDenied, ResourceConflict
from app.models.version import PipelineVersion, VersionStatus
from app.schemas.pipeline import QualityRule
from app.services.gate import GateReport

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 指纹
# --------------------------------------------------------------------------


def compute_fingerprint(plan: dict[str, Any], config: str, quality_rules: list[Any]) -> str:
    """计算版本指纹。

    用**规范化 JSON**（键排序、紧凑分隔符、保留中文）保证：同样的内容永远得到
    同样的摘要，不受字典插入顺序或空白差异影响——否则版本 Diff 会产生假阳性。
    """
    canonical = json.dumps(
        {"plan": plan, "config": config, "quality_rules": quality_rules},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 创建与冻结
# --------------------------------------------------------------------------


def next_version_no(db: Session, project_id: uuid.UUID) -> int:
    """项目内递增的版本号（前端展示「v3」）。"""
    # 版本号仅用于人类阅读；真正的内容身份由冻结后的 sha256 指纹承担。
    current = db.scalar(
        select(func.max(PipelineVersion.version_no)).where(
            PipelineVersion.project_id == project_id
        )
    )
    return int(current or 0) + 1


def create_draft(
    db: Session,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID | None,
    created_by: uuid.UUID,
    plan: dict[str, Any],
    config: str,
    quality_rules: list[Any] | None = None,
    source_profile_id: uuid.UUID | None = None,
    target_profile_id: uuid.UUID | None = None,
) -> PipelineVersion:
    """生成成功后落一条草稿版本；此时尚无指纹（未过门禁）。

    同时绑定本次生成所依据的探查记录，供冻结阶段按同一版本还原 Schema。
    """
    version = PipelineVersion(
        project_id=project_id,
        run_id=run_id,
        created_by=created_by,
        version_no=next_version_no(db, project_id),
        status=VersionStatus.DRAFT,
        etl_plan=plan,
        config=config,
        quality_rules=list(quality_rules or []),
        source_profile_id=source_profile_id,
        target_profile_id=target_profile_id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def freeze_version(
    db: Session, *, version: PipelineVersion, report: GateReport
) -> PipelineVersion:
    """门禁通过后冻结版本（FR-19）。

    门禁不通过时版本会被标记为 ``rejected`` 并保留报告——**不留下半冻结状态**。
    """
    if version.is_immutable:
        raise ResourceConflict("该版本已冻结，不可重复冻结")

    version.gate_report = report.to_dict()

    if not report.passed:
        # 先持久化 rejected 与完整报告再抛业务异常，确保失败也能追溯。
        version.status = VersionStatus.REJECTED
        db.commit()
        failed = "；".join(f"{c.name}：{c.message}" for c in report.failed_checks)
        raise ResourceConflict(f"门禁未通过，版本已被拒绝（{failed}）")

    version.sha256 = compute_fingerprint(
        version.etl_plan, version.config, version.quality_rules
    )
    version.status = VersionStatus.FROZEN
    version.frozen_at = datetime.now(UTC)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # sha256 有唯一约束：内容完全相同的版本不应重复存在
        raise ResourceConflict("已存在内容完全相同的冻结版本（指纹重复）") from exc

    db.refresh(version)
    logger.info("版本已冻结 v%s sha256=%s", version.version_no, version.sha256[:12])
    return version


def ensure_mutable(version: PipelineVersion) -> None:
    """写操作前的守卫 —— 冻结版本一律拒绝修改（FR-19）。"""
    if version.is_immutable:
        raise ResourceConflict("冻结版本不可修改")


def set_quality_rules(
    db: Session,
    *,
    version: PipelineVersion,
    user_id: uuid.UUID,
    rules: list[QualityRule],
) -> PipelineVersion:
    """工程师在冻结前显式声明质量契约（FR-32）。"""
    if version.created_by != user_id:
        raise PermissionDenied("只能编辑本人创建的方案版本")
    if version.status is not VersionStatus.DRAFT:
        raise ResourceConflict("只有草稿版本可以修改质量规则")

    plan = dict(version.etl_plan or {})
    target_fields = {
        str(mapping.get("target_field"))
        for mapping in plan.get("field_mappings") or []
        if mapping.get("target_field")
    }
    unknown = sorted({rule.field for rule in rules if rule.field not in target_fields})
    if unknown:
        raise ResourceConflict(f"质量规则字段不在已审查的目标字段映射中：{'、'.join(unknown)}")

    error_codes = [rule.error_code for rule in rules]
    if len(error_codes) != len(set(error_codes)):
        raise ResourceConflict("质量规则的错误码必须唯一")

    snapshots = [rule.model_dump(mode="json") for rule in rules]
    # 两处快照同步更新；指纹覆盖二者，冻结后审批看到的内容必须一致。
    plan["quality_rules"] = snapshots
    version.etl_plan = plan
    version.quality_rules = snapshots
    db.commit()
    db.refresh(version)
    return version


# --------------------------------------------------------------------------
# Diff（FR-20）
# --------------------------------------------------------------------------


def _mapping_index(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """按**源字段**索引字段映射。

    以源字段为身份键，是为了让「同一个源字段改映射到别的目标字段」被识别为
    ``changed`` 而不是一对 added/removed——后者会把一处改动报成两处，误导审阅者。
    P0 场景下源字段到目标字段是 1:1；若将来支持一对多，此处需改为按 (源,目标) 索引。
    """
    return {
        str(item.get("source_field")): item
        for item in (plan.get("field_mappings") or [])
    }


def diff_versions(left: PipelineVersion, right: PipelineVersion) -> dict[str, Any]:
    """计算两个版本在方案 / 配置 / 质量规则三个维度的差异。

    ``left`` 是基准（较旧），``right`` 是对照（较新）。
    """
    left_plan = left.etl_plan or {}
    right_plan = right.etl_plan or {}

    left_mappings = _mapping_index(left_plan)
    right_mappings = _mapping_index(right_plan)

    added = [right_mappings[key] for key in sorted(right_mappings.keys() - left_mappings.keys())]
    removed = [left_mappings[key] for key in sorted(left_mappings.keys() - right_mappings.keys())]
    changed = [
        {
            "before": left_mappings[key],
            "after": right_mappings[key],
        }
        for key in sorted(left_mappings.keys() & right_mappings.keys())
        if left_mappings[key] != right_mappings[key]
    ]

    left_steps = [str(step.get("operator_id")) for step in (left_plan.get("steps") or [])]
    right_steps = [str(step.get("operator_id")) for step in (right_plan.get("steps") or [])]

    config_diff = list(
        # 统一 diff 直接供审阅界面展示，保留行级上下文而不是只返回 changed=true。
        difflib.unified_diff(
            (left.config or "").splitlines(),
            (right.config or "").splitlines(),
            fromfile=f"v{left.version_no}",
            tofile=f"v{right.version_no}",
            lineterm="",
        )
    )

    return {
        "left": left,
        "right": right,
        "fingerprint_equal": bool(left.sha256) and left.sha256 == right.sha256,
        "plan_changed": left_plan != right_plan,
        "config_changed": (left.config or "") != (right.config or ""),
        "quality_rules_changed": (left.quality_rules or []) != (right.quality_rules or []),
        "mappings_added": added,
        "mappings_removed": removed,
        "mappings_changed": changed,
        "steps_added": [step for step in right_steps if step not in left_steps],
        "steps_removed": [step for step in left_steps if step not in right_steps],
        "config_diff": config_diff,
    }
