"""Prepare 准备单、风险评级与双维审批（M6，FR-21 ~ FR-24）。

**Prepare 零外部副作用**（FR-22）：本模块只读已冻结的版本、算风险、写准备单与
两个空审批槽位。不连数据源、不提交作业、不签发令牌——准备单生成路径上不存在任何
Tool Broker 调用，这是可以静态检查的性质，而不是一句承诺。

**风险评级是确定性计算**（与门禁同理）：相同方案永远得到相同等级与相同理由。
调模型给出「风险高/中/低」无法复现、无法解释，也就无法在审批台上回答
「为什么是 P1」——而这个问题恰恰是审批人唯一需要被回答的。

**职责分离（FR-24）在写入前判定**：申请人不得审批自己提交的版本；同一人不得
占用两个审批槽位。第一条拦住「自批」，第二条拦住「一人分饰两角」——
后者在正常角色矩阵下被结构性挡住（一个成员在一个项目内只有一个角色），
但如果有人先以数据审批人身份审完、再被改成安全审批人接着审，角色矩阵就挡不住了，
因此仍要显式判定。
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import (
    ApprovalIncomplete,
    PermissionDenied,
    ResourceConflict,
    SelfApprovalDenied,
)
from app.models.approval import (
    APPROVAL_DIMENSION_LABEL,
    Approval,
    ApprovalDecision,
    ApprovalDimension,
    Preparation,
    PreparationStatus,
    RiskLevel,
)
from app.models.identity import Membership, UserRole
from app.models.version import PipelineVersion, VersionStatus

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 风险评级（FR-21）
# --------------------------------------------------------------------------

# 敏感分级由 M2 探查阶段定出（PRD 附录 C），此处只做序关系比较
_SENSITIVITY_RANK = {"L1": 1, "L2": 2, "L3": 3, "L4": 4}

# 写入即**替换正式表**的汇端（ADR-06：影子表 + 原子 Swap）。
# 这类写入失败不会污染正式表，但一旦成功就直接改变生产数据的可见内容，
# 因此同样敏感度下风险高一级。
_COVERING_SINKS = {"seatunnel.sink.Doris"}

# 持续增量源（CDC）：任务的每一次推进都直接作用于生产链路，
# 风险不随批次结束而归零，故不低于 P2。
_STREAMING_SOURCES = {
    "seatunnel.source.MySQL-CDC",
    "seatunnel.source.Postgres-CDC",
}

_RISK_BY_RANK = {
    0: RiskLevel.P0,
    1: RiskLevel.P1,
    2: RiskLevel.P2,
    3: RiskLevel.P3,
}

# 双维审批各自的角色门槛（PRD 第 2 章五角色矩阵）
DIMENSION_ROLE = {
    ApprovalDimension.DATA: UserRole.CHECKER_DATA,
    ApprovalDimension.SECURITY: UserRole.CHECKER_SECURITY,
}
DIMENSION_LABEL = APPROVAL_DIMENSION_LABEL


@dataclass(frozen=True)
class RiskAssessment:
    level: RiskLevel
    reasons: list[str] = field(default_factory=list)


def _steps(plan: dict[str, Any], role: str) -> set[str]:
    return {
        str(step.get("operator_id") or "")
        for step in (plan.get("steps") or [])
        if str(step.get("role") or "") == role
    }


def _highest_sensitivity(plan: dict[str, Any]) -> int:
    rank = 0
    for mapping in plan.get("field_mappings") or []:
        rank = max(rank, _SENSITIVITY_RANK.get(str(mapping.get("sensitivity_level") or ""), 0))
    return rank


def rate_risk(version: PipelineVersion) -> RiskAssessment:
    """按方案内容评级（FR-21）。

    三个输入，全部取自被审方案本身，不依赖任何运行期状态：

    - **数据分级**：字段映射中携带的敏感级别，取最高一级；
    - **写入语义**：汇端是否直接替换正式表（Doris 影子表 + 原子 Swap）；
    - **执行模式**：源端是否为持续增量（CDC）。

    评级阶梯（确定性、可复现）：

    | 最高分级 | 覆盖正式表 | 结果 |
    |---------|-----------|------|
    | L4 | 任意 | P0 |
    | L3 | 是 | P0 |
    | L3 | 否 | P1 |
    | L2 | 是 | P1 |
    | L2 | 否 | P2 |
    | 无 / L1 | 是 | P2 |
    | 无 / L1 | 否 | P3 |

    增量任务（CDC 源）再取 `min(等级, P2)`——持续作用在生产链路上的任务
    不因单批结束而降低风险。
    """
    plan = version.etl_plan or {}
    top = _highest_sensitivity(plan)
    sinks = _steps(plan, "sink")
    sources = _steps(plan, "source")

    covering = bool(sinks & _COVERING_SINKS)
    streaming = bool(sources & _STREAMING_SOURCES)

    reasons: list[str] = []

    if top == 0:
        rank = 2 if covering else 3
        reasons.append("方案未标注字段敏感级别，按最低依据评级")
    elif top >= 4:
        rank = 0
        reasons.append("涉及 L4（绝密）字段")
    elif top == 3:
        rank = 0 if covering else 1
        reasons.append("涉及 L3（机密）字段")
    elif top == 2:
        rank = 1 if covering else 2
        reasons.append("涉及 L2（内部）字段")
    else:
        rank = 2 if covering else 3

    if covering:
        reasons.append("汇端为覆盖写（影子表 + 原子 Swap），发布即改变正式表内容")
    if streaming:
        if rank > 2:
            reasons.append("源端为持续增量同步（CDC），风险不低于 P2")
        rank = min(rank, 2)

    return RiskAssessment(level=_RISK_BY_RANK[rank], reasons=reasons)


# --------------------------------------------------------------------------
# 准备单四项（FR-22）
# --------------------------------------------------------------------------


def _shadow_table(plan: dict[str, Any], version: PipelineVersion) -> str:
    """影子表名。

    带版本号是刻意的：并发或重试的不同版本各有自己的影子表，
    一次失败留下的残留不会挡住下一次执行（FR-38 的清理对象也因此可判）。
    """
    target = str(plan.get("target_table") or "unknown")
    return f"{target}__shadow_v{version.version_no}"


def build_impact_scope(plan: dict[str, Any], version: PipelineVersion) -> dict[str, Any]:
    """影响范围：这次执行会动到哪些对象、用什么方式发布。"""
    return {
        "source_table": str(plan.get("source_table") or ""),
        "target_table": str(plan.get("target_table") or ""),
        "target_datasource_id": str(plan.get("target_datasource_id") or ""),
        "shadow_table": _shadow_table(plan, version),
        # 发布方式取自 ADR-06 的定案，不是每版可选参数——写死以免准备单与实际不符
        "publish_strategy": "shadow-swap",
        "publishes_to_production_table": True,
    }


def build_data_grading(plan: dict[str, Any]) -> dict[str, Any]:
    """数据分级摘要。

    **只统计不列名**：准备单会展示在审批台与审计报告里，把敏感字段名抄一份进去
    等于给敏感信息多开一个出口。需要看具体字段时，审批人有版本详情可读。
    """
    counts: dict[str, int] = {}
    sensitive = 0
    for mapping in plan.get("field_mappings") or []:
        level = str(mapping.get("sensitivity_level") or "L1")
        counts[level] = counts.get(level, 0) + 1
        if _SENSITIVITY_RANK.get(level, 1) >= 3:
            sensitive += 1

    top = max((_SENSITIVITY_RANK.get(level, 1) for level in counts), default=1)
    return {
        "max_level": f"L{top}",
        "level_counts": dict(sorted(counts.items())),
        "sensitive_field_count": sensitive,
        "masked": True,
    }


def build_budget() -> dict[str, Any]:
    """执行预算（FR-35 的监督契约）。

    当前取平台默认值；待数据工程师可显式声明预算时，此处改为读方案中的声明值。
    """
    return {
        "max_rows": settings.budget_max_rows,
        "max_bytes": settings.budget_max_bytes,
        "max_duration_seconds": settings.budget_max_duration_seconds,
        "max_reject_ratio": settings.budget_max_reject_ratio,
        "max_amplification_ratio": settings.budget_max_amplification_ratio,
    }


def build_rollback_plan(plan: dict[str, Any], version: PipelineVersion) -> dict[str, Any]:
    """回滚方案（FR-38）。

    影子表方案下回滚天然是「丢弃未发布的影子表」——正式表从未被改动，
    因此不需要反向数据搬运，也就不会出现「回滚失败导致数据半新半旧」。
    """
    return {
        "strategy": "discard-shadow",
        "description": "丢弃影子表残留并释放资源，正式表保持不变",
        "shadow_table": _shadow_table(plan, version),
    }


# --------------------------------------------------------------------------
# 准备单生成
# --------------------------------------------------------------------------


def _new_deadline() -> datetime:
    return datetime.now(UTC) + timedelta(hours=settings.approval_ttl_hours)


def create_preparation(
    db: Session, *, version: PipelineVersion, user_id: uuid.UUID
) -> Preparation:
    """生成（或取回）版本的准备单，并建出两个待审槽位。

    **幂等**：同一版本重复调用返回同一份准备单——审批人可能反复打开审批台，
    每次打开都新造一份准备单会让「审的是哪一份」失去唯一答案。

    唯一需要重置的情形是**审批已超时**：此时旧槽位不再构成有效授权，
    重新 Prepare 相当于重新发起一轮审批（换新时限、清空决定）。
    """
    if version.status is not VersionStatus.FROZEN:
        raise ResourceConflict("版本尚未通过门禁冻结，无法生成准备单")

    existing = db.scalar(
        select(Preparation).where(Preparation.version_id == version.id)
    )
    if existing is not None:
        if existing.status is PreparationStatus.COMMITTED:
            return existing
        if not existing.is_expired:
            return existing
        # 已超时：重置时限与槽位，让审批重新开始
        existing.expires_at = _new_deadline()
        existing.status = PreparationStatus.PENDING
        for approval in existing.approvals:
            approval.decision = None
            approval.approver_id = None
            approval.comment = None
            approval.decided_at = None
            approval.expires_at = existing.expires_at
        db.commit()
        db.refresh(existing)
        logger.info("准备单已超时重置 preparation=%s", existing.id)
        return existing

    plan = version.etl_plan or {}
    assessment = rate_risk(version)
    deadline = _new_deadline()

    preparation = Preparation(
        project_id=version.project_id,
        version_id=version.id,
        created_by=user_id,
        fingerprint=str(version.sha256 or ""),
        risk_level=assessment.level,
        risk_reasons=assessment.reasons,
        impact_scope=build_impact_scope(plan, version),
        data_grading=build_data_grading(plan),
        budget=build_budget(),
        rollback_plan=build_rollback_plan(plan, version),
        status=PreparationStatus.PENDING,
        expires_at=deadline,
    )
    preparation.approvals = [
        Approval(
            project_id=version.project_id,
            version_id=version.id,
            dimension=dimension,
            # 两个槽位一起建出：审批台据此展示「谁还没审」，而不是等审批人提交才发现缺一维
            expires_at=deadline,
        )
        for dimension in (ApprovalDimension.DATA, ApprovalDimension.SECURITY)
    ]
    db.add(preparation)
    db.commit()
    db.refresh(preparation)
    logger.info(
        "已生成准备单 version=v%s risk=%s preparation=%s",
        version.version_no,
        assessment.level,
        preparation.id,
    )
    return preparation


# --------------------------------------------------------------------------
# 审批（FR-23 / FR-24）
# --------------------------------------------------------------------------


def _other_of(preparation: Preparation, dimension: ApprovalDimension) -> Approval | None:
    for approval in preparation.approvals:
        if approval.dimension is not dimension:
            return approval
    return None


def decide(
    db: Session,
    *,
    approval: Approval,
    membership: Membership,
    user_id: uuid.UUID,
    decision: ApprovalDecision,
    comment: str | None = None,
) -> Preparation:
    """记录一次审批决定（FR-23），写入前完成职责分离校验（FR-24）。"""
    preparation = approval.preparation
    version = db.get(PipelineVersion, preparation.version_id)

    if preparation.status is PreparationStatus.COMMITTED:
        raise ResourceConflict("该准备单已完成 Commit，审批事实不可再变更")
    if preparation.is_expired:
        # 超时要落库而不只是报错，否则下次调用还会看到一个「看似可审」的槽位
        preparation.status = PreparationStatus.EXPIRED
        db.commit()
        raise ApprovalIncomplete("审批已超时失效，请重新发起 Prepare")

    # 职责分离之一：维度角色门槛（数据维度只能由数据审批人审，反之亦然）
    expected_role = DIMENSION_ROLE[approval.dimension]
    if membership.role is not expected_role:
        raise PermissionDenied(
            f"{DIMENSION_LABEL[approval.dimension]}需由 {expected_role.value} 审批"
        )

    # 职责分离之二：不可自批
    if user_id == preparation.created_by:
        raise SelfApprovalDenied("职责分离：申请人不可以审批自己提交的版本")

    # 职责分离之三：同一人不可占两个槽位。
    # 正常角色矩阵下结构性不可达，但角色变更可以绕过它——显式判定兜底。
    other = _other_of(preparation, approval.dimension)
    if other is not None and other.approver_id == user_id:
        raise SelfApprovalDenied(
            f"职责分离：同一人不可同时占用{DIMENSION_LABEL[other.dimension]}与"
            f"{DIMENSION_LABEL[approval.dimension]}两个审批槽位"
        )

    approval.decision = decision
    approval.approver_id = user_id
    approval.comment = comment
    approval.decided_at = datetime.now(UTC)

    _refresh_status(preparation)
    db.commit()
    db.refresh(preparation)
    if version is not None:
        logger.info(
            "审批已记录 version=v%s %s=%s",
            version.version_no,
            DIMENSION_LABEL[approval.dimension],
            decision,
        )
    return preparation


def _refresh_status(preparation: Preparation) -> None:
    """按两个槽位的当前决定重算准备单状态。

    驳回优先：只要有一维驳回，整体即驳回；两维都通过才是通过。
    允许「驳回 → 重新批准」把状态推回 `approved`——审批人在拿到补充说明后
    改判是正常流程，而口径始终是「槽位的当前决定」这一唯一事实。
    """
    decisions = {approval.dimension: approval.decision for approval in preparation.approvals}

    if any(value is ApprovalDecision.REJECTED for value in decisions.values()):
        preparation.status = PreparationStatus.REJECTED
    elif all(value is ApprovalDecision.APPROVED for value in decisions.values()):
        preparation.status = PreparationStatus.APPROVED
    else:
        preparation.status = PreparationStatus.PENDING


def ensure_approvals_complete(preparation: Preparation) -> None:
    """Commit 前的审批事实复核（FR-25）。未齐即拒，并指明缺哪一维。"""
    if preparation.is_expired:
        raise ApprovalIncomplete("准备单已超时失效，请重新发起 Prepare")

    missing = preparation.missing_dimensions
    if missing:
        raise ApprovalIncomplete(
            "审批未完成，缺失：" + "、".join(missing),
            detail={"missing_dimensions": missing},
        )
