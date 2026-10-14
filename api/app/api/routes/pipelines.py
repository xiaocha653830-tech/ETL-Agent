"""需求生成接口（API-03 / API-04，FR-08 ~ FR-14）。

**当前为同步实现**：模型调用在请求内完成。SPEC §8 要求「生成走异步任务 +
前端轮询/流式」，待 T-13 引入 Celery 后改为后台任务——届时只改实现，
不改契约（提交拿 ``run_id``、再轮询查询的语义保持不变）。
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, ensure_role, require_project_member
from app.api.routes.datasources import authorized_datasource
from app.core.errors import ResourceConflict, ResourceNotFound
from app.models.datasource import MetadataProfile
from app.models.identity import Membership, User, UserRole
from app.models.pipeline import PipelineRun, PipelineRunStatus
from app.models.version import PipelineVersion
from app.schemas.pipeline import (
    ClarificationQuestion,
    ConfigCandidate,
    EtlPlan,
    GenerateRequest,
    GenerationResult,
    GenerationStatus,
    ResumeRequest,
)
from app.services.endpoint_context import EndpointContext
from app.services.llm import get_llm_client
from app.services.orchestrator import (
    build_generation_graph,
    build_initial_state,
    get_checkpointer,
)
from app.services.versioning import create_draft

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


# --------------------------------------------------------------------------
# 上下文装配
# --------------------------------------------------------------------------


def _latest_profile(db: Session, datasource_id: uuid.UUID) -> MetadataProfile | None:
    return db.scalar(
        select(MetadataProfile)
        .where(MetadataProfile.datasource_id == datasource_id)
        .order_by(MetadataProfile.sampled_at.desc())
        .limit(1)
    )


def build_endpoint(db: Session, datasource_id: uuid.UUID, user: User) -> EndpointContext:
    """把数据源及其最新探查结果打包成生成阶段的输入。

    只取非敏感连接参数与 Schema；**凭据完全不进入本结构**。
    """
    datasource, _ = authorized_datasource(db, datasource_id, user)
    profile = _latest_profile(db, datasource.id)
    tables = ((profile.schema_json or {}).get("tables") or []) if profile else []
    return EndpointContext(
        datasource_id=str(datasource.id),
        name=datasource.name,
        type=datasource.type.value,
        config=datasource.config or {},
        tables=tables,
    )


def _maybe_create_draft(
    db: Session, run: PipelineRun, result: GenerationResult, user: User
) -> None:
    """生成成功后自动落一条草稿版本（T-09 的冻结入口）。

    同一 run 只建一次——重复 resume 或重复读取都不应产生重复版本。
    """
    if result.status is not GenerationStatus.SUCCEEDED or result.plan is None:
        return
    if db.scalar(select(PipelineVersion).where(PipelineVersion.run_id == run.id)) is not None:
        return

    create_draft(
        db,
        project_id=run.project_id,
        run_id=run.id,
        created_by=user.id,
        plan=result.plan.model_dump(mode="json"),
        config=result.config.content if result.config else "",
        quality_rules=[rule.model_dump() for rule in result.plan.quality_rules],
    )


def _authorized_run(
    db: Session, run_id: uuid.UUID, user: User
) -> tuple[PipelineRun, Membership]:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise ResourceNotFound("生成任务不存在")
    membership = require_project_member(run.project_id, user, db)
    return run, membership


# --------------------------------------------------------------------------
# 图的调用
# --------------------------------------------------------------------------


def _thread_config(run_id: uuid.UUID) -> dict[str, Any]:
    """thread_id 必须与 PipelineRun.id 一致，否则恢复时找不到断点。"""
    return {"configurable": {"thread_id": str(run_id)}}


def _invoke_graph(
    run_id: uuid.UUID,
    state: dict[str, Any] | None = None,
    *,
    resume_value: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from langgraph.types import Command

    graph = build_generation_graph(get_llm_client(), get_checkpointer())
    config = _thread_config(run_id)
    if resume_value is not None:
        return graph.invoke(Command(resume=resume_value), config)
    return graph.invoke(state or {}, config)


def _collect_questions(result: dict[str, Any]) -> list[dict[str, Any]]:
    """从图返回值里取出待澄清问题（interrupt 时挂在 ``__interrupt__`` 上）。"""
    questions: list[dict[str, Any]] = []
    for item in result.get("__interrupt__") or ():
        value = getattr(item, "value", None) or {}
        if isinstance(value, dict):
            questions.extend(value.get("questions") or [])
    return questions


# --------------------------------------------------------------------------
# 结果组装
# --------------------------------------------------------------------------


def _build_result(run: PipelineRun, state: dict[str, Any]) -> GenerationResult:
    raw_questions = _collect_questions(state) or (state.get("pending_questions") or [])
    questions: list[ClarificationQuestion] = []
    for item in raw_questions:
        try:
            questions.append(ClarificationQuestion.model_validate(item))
        except ValidationError:
            logger.warning("丢弃一条不合规的澄清问题：%s", item)

    etl_plan: EtlPlan | None = None
    dag = None
    plan_payload = state.get("plan")
    if plan_payload:
        try:
            etl_plan = EtlPlan.model_validate(plan_payload)
            dag = etl_plan.to_dag()
        except ValidationError as exc:
            # 宁可拒绝，也不把不合规的方案交给下游门禁（T-08）
            logger.warning("生成的方案不符合契约，已拒绝：%s", exc)
            return GenerationResult(
                run_id=run.id,
                status=GenerationStatus.FAILED,
                error="生成的方案不符合结构化契约，已被拒绝（详见服务端日志）",
            )

    if questions:
        gen_status = GenerationStatus.INTERRUPTED
    elif not state.get("supported", True):
        gen_status = GenerationStatus.UNSUPPORTED
    elif etl_plan is not None:
        gen_status = GenerationStatus.SUCCEEDED
    else:
        gen_status = GenerationStatus.FAILED

    config_text = state.get("config")
    return GenerationResult(
        run_id=run.id,
        status=gen_status,
        plan=etl_plan,
        config=ConfigCandidate(content=config_text) if config_text else None,
        dag=dag,
        questions=questions,
        warnings=[str(item) for item in (state.get("warnings") or [])],
        unsupported_reason=(state.get("unsupported_reason") or None),
    )


_STATUS_MAP = {
    GenerationStatus.RUNNING: PipelineRunStatus.RUNNING,
    GenerationStatus.SUCCEEDED: PipelineRunStatus.SUCCEEDED,
    GenerationStatus.INTERRUPTED: PipelineRunStatus.INTERRUPTED,
    GenerationStatus.UNSUPPORTED: PipelineRunStatus.UNSUPPORTED,
    GenerationStatus.FAILED: PipelineRunStatus.FAILED,
}


def _persist(db: Session, run: PipelineRun, result: GenerationResult) -> None:
    run.status = _STATUS_MAP[result.status]
    run.plan = result.plan.model_dump(mode="json") if result.plan else None
    run.config_snapshot = result.config.content if result.config else None
    run.questions = [item.model_dump() for item in result.questions] or None
    run.error = result.unsupported_reason or result.error
    db.commit()


def _result_from_run(run: PipelineRun) -> GenerationResult:
    """从落库结果重建响应（不重放状态机）。"""
    plan: EtlPlan | None = None
    dag = None
    if run.plan:
        try:
            plan = EtlPlan.model_validate(run.plan)
            dag = plan.to_dag()
        except ValidationError:
            logger.warning("历史方案不符合契约，run=%s", run.id)

    return GenerationResult(
        run_id=run.id,
        status=GenerationStatus(run.status.value),
        plan=plan,
        config=ConfigCandidate(content=run.config_snapshot) if run.config_snapshot else None,
        dag=dag,
        questions=[ClarificationQuestion.model_validate(item) for item in (run.questions or [])],
        unsupported_reason=run.error if run.status is PipelineRunStatus.UNSUPPORTED else None,
        error=run.error if run.status is PipelineRunStatus.FAILED else None,
    )


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------


@router.post("/generate", response_model=GenerationResult, status_code=status.HTTP_201_CREATED)
def generate(
    payload: GenerateRequest, user: CurrentUser, db: DbSession
) -> GenerationResult:
    """API-03：提交自然语言需求（FR-08）。

    可能直接产出方案，也可能返回「澄清中断」——前端按 ``status`` 分支。
    """
    membership = require_project_member(payload.project_id, user, db)
    ensure_role(membership, UserRole.MAKER)

    source = build_endpoint(db, payload.source_datasource_id, user)
    target = build_endpoint(db, payload.target_datasource_id, user)

    run = PipelineRun(
        project_id=payload.project_id,
        created_by=user.id,
        requirement=payload.requirement,
        source_datasource_id=payload.source_datasource_id,
        target_datasource_id=payload.target_datasource_id,
        status=PipelineRunStatus.RUNNING,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    state = build_initial_state(
        run_id=str(run.id),
        requirement=payload.requirement,
        source=source,
        target=target,
    )
    result = _build_result(run, _invoke_graph(run.id, state))
    _persist(db, run, result)
    _maybe_create_draft(db, run, result, user)
    return result


@router.post("/{run_id}/resume", response_model=GenerationResult)
def resume(
    run_id: uuid.UUID,
    payload: ResumeRequest,
    user: CurrentUser,
    db: DbSession,
) -> GenerationResult:
    """API-04：补充澄清信息后从断点继续（FR-10），**不重跑已完成阶段**。"""
    run, membership = _authorized_run(db, run_id, user)
    ensure_role(membership, UserRole.MAKER)

    if run.status is not PipelineRunStatus.INTERRUPTED:
        raise ResourceConflict(f"该生成任务当前状态为 {run.status.value}，不在等待澄清")

    if not payload.answers:
        raise ResourceConflict("未提供任何补充信息")

    result = _build_result(
        run, _invoke_graph(run_id, resume_value={"answers": payload.answers})
    )
    _persist(db, run, result)
    _maybe_create_draft(db, run, result, user)
    return result


@router.get("/{run_id}", response_model=GenerationResult)
def get_generation(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> GenerationResult:
    """查询生成结果（供前端轮询）。"""
    run, _ = _authorized_run(db, run_id, user)
    return _result_from_run(run)


@router.get("", response_model=list[GenerationResult])
def list_generations(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> list[GenerationResult]:
    """列出项目内的生成任务（前端 Studio 列表页）。"""
    require_project_member(project_id, user, db)
    stmt = (
        select(PipelineRun)
        .where(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.created_at.desc())
        .limit(100)
    )
    return [_result_from_run(run) for run in db.scalars(stmt)]
