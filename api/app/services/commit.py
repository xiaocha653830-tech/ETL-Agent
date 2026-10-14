"""Commit 收口（M6，FR-25 ~ FR-28）。

三段式协议的最后一步：``prepare → approve → **commit**``。这一步做四件事，
**全部在一次数据库事务里完成**：

  1. **复核审批事实**——双维审批齐备且在时限内，否则拒（FR-25）；
  2. **复核制品指纹**——版本内容复算出的指纹必须同时等于版本自身指纹、
     准备单声明的指纹、以及调用方持有的指纹，否则拒（FR-25）；
  3. **申请执行许可**——经 PDP 判定，由安全内核签发绑定「工具 + 主体 + 环境 + 制品」
     的单次能力令牌（FR-26）；
  4. **同事务落业务事实与 Outbox**——运行记录与待投递事件一起提交（FR-28）。

**为什么指纹要复算而不是直接比两个字符串**：版本冻结后不可写（FR-19）是应用层
约定，不是数据库约束。只比 ``preparation.fingerprint == version.sha256`` 只能证明
「两处记录一致」，证明不了「内容没被动过」。复算一次，才把「制品未被篡改」这件事
从约定变成事实。

**为什么审批复核与指纹复核都要做**：审批背书的是「某个指纹的制品」。
若制品换了而审批记录还在，那枚审批就不再指向任何东西——这两种失效必须分别拦住，
否则「审批通过」会变成一张可以贴在任意制品上的通行证。
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import (
    AppError,
    FingerprintMismatch,
    PermissionDenied,
    ResourceConflict,
    ResourceNotFound,
    RiskBlocked,
    SubjectNotAuthorized,
)
from app.models.approval import Preparation, PreparationStatus
from app.models.execution import ExecutionRun, ExecutionRunStatus
from app.models.version import PipelineVersion, VersionStatus
from app.schemas.capability import CapabilityTokenOut
from app.schemas.execution import ExecutionCommand
from app.services.approval import ensure_approvals_complete
from app.services.capability import issue_capability
from app.services.outbox import EVENT_RUN_REQUESTED, enqueue, execution_run_key
from app.services.pdp import get_pdp
from app.services.versioning import compute_fingerprint

logger = logging.getLogger(__name__)

# 数据面执行工具的受管名称。它同时是能力令牌的绑定维度之一——
# 令牌只对这一个工具有效，拿到它也无法去调别的工具。
EXECUTION_TOOL = "seatunnel.submit-job"

# PDP 拒绝码 → 对外异常。PDP 不承载业务语义，但它给出的码是稳定契约，
# 这里只是把它翻译成统一错误响应；未列出的码一律按「未获授权」呈现。
_PDP_DENIAL: dict[str, type[AppError]] = {
    "POL_SUBJECT_NOT_AUTHORIZED": SubjectNotAuthorized,
    "POL_RISK_BLOCKED": RiskBlocked,
}


@dataclass(frozen=True)
class CommitResult:
    run: ExecutionRun
    capability: CapabilityTokenOut


def _recheck_fingerprint(
    version: PipelineVersion, preparation: Preparation, expected: str
) -> None:
    """三方指纹复核（FR-25）。任一处不符都拒绝，并说清是哪一处。"""
    recomputed = compute_fingerprint(version.etl_plan, version.config, version.quality_rules)

    problems: list[str] = []
    if recomputed != version.sha256:
        problems.append("版本内容与自身指纹不一致（冻结后被改写）")
    if preparation.fingerprint != version.sha256:
        problems.append("准备单背书的是另一份制品")
    if expected != version.sha256:
        problems.append("调用方持有的指纹与当前制品不一致")

    if problems:
        raise FingerprintMismatch(
            "；".join(problems),
            detail={
                "expected": expected,
                "preparation": preparation.fingerprint,
                "version": version.sha256,
                "recomputed": recomputed,
            },
        )


def _enforce_pdp(*, subject: str, environment: str, artifact: str) -> None:
    """申请执行许可（FR-26 前置）。

    **策略许可不等于可跳过审批**：这里只回答「这个主体、这个环境、这个工具、
    这件事」在准入上是否成立；审批是否齐备是上一步单独判的，二者不可互相替代。
    """
    decision = get_pdp().decide(
        subject=subject, tool=EXECUTION_TOOL, environment=environment, artifact=artifact
    )
    if decision.allow:
        return

    exc_type = _PDP_DENIAL.get(str(decision.code))
    if exc_type is None:
        raise PermissionDenied(decision.reason)
    raise exc_type(decision.reason)


def commit_preparation(
    db: Session,
    *,
    preparation: Preparation,
    user_id: uuid.UUID,
    expected_fingerprint: str,
    environment: str | None = None,
) -> CommitResult:
    """执行 Commit（API-08）。调用方负责角色与租户校验。"""
    if preparation.status is PreparationStatus.COMMITTED:
        # 不返回既有 run_id 让重复提交「看起来成功」：那会让调用方以为又拿到了一次
        # 授权，而令牌是单次且 5 分钟有效的——静默成功比报错更危险。
        raise ResourceConflict("该准备单已完成 Commit，不可重复提交")

    # 1) 审批事实复核：未齐 / 已超时一律拒（FR-25）
    ensure_approvals_complete(preparation)

    version = db.get(PipelineVersion, preparation.version_id)
    if version is None:
        raise ResourceNotFound("版本不存在")
    if version.status is not VersionStatus.FROZEN:
        raise ResourceConflict(f"版本当前状态为 {version.status.value}，不可执行")

    # 2) 制品指纹复核
    _recheck_fingerprint(version, preparation, expected_fingerprint)

    resolved_env = environment or settings.app_env
    artifact = str(version.sha256)

    # 3) 申请执行许可
    _enforce_pdp(subject=str(user_id), environment=resolved_env, artifact=artifact)

    # 4) 同事务落业务事实 + 令牌 + Outbox
    run = ExecutionRun(
        project_id=version.project_id,
        version_id=version.id,
        preparation_id=preparation.id,
        created_by=user_id,
        status=ExecutionRunStatus.PENDING,
        environment=resolved_env,
        artifact=artifact,
    )
    db.add(run)
    # 先 flush 取得 run.id——Outbox 载荷要引用它，且幂等键由它构成
    db.flush()

    capability = issue_capability(
        db,
        tool=EXECUTION_TOOL,
        subject=str(user_id),
        environment=resolved_env,
        artifact=artifact,
        preparation_id=preparation.id,
        commit=False,
    )
    run.capability_id = capability.capability_id

    command = ExecutionCommand(
        run_id=run.id,
        version_id=version.id,
        preparation_id=preparation.id,
        capability_token=capability.token,
        capability_id=capability.capability_id,
        tool=EXECUTION_TOOL,
        subject=str(user_id),
        environment=resolved_env,
        artifact=artifact,
        target_table=str((preparation.impact_scope or {}).get("target_table") or ""),
        shadow_table=str((preparation.impact_scope or {}).get("shadow_table") or ""),
        budget=dict(preparation.budget or {}),
    )
    enqueue(
        db,
        idempotency_key=execution_run_key(run.id),
        event_type=EVENT_RUN_REQUESTED,
        payload=command.model_dump(mode="json"),
    )

    preparation.status = PreparationStatus.COMMITTED
    preparation.committed_at = datetime.now(UTC)

    db.commit()
    db.refresh(run)

    logger.info(
        "Commit 已受理 run=%s version=%s artifact=%s",
        run.id,
        version.id,
        artifact[:12],
    )
    return CommitResult(run=run, capability=capability)
