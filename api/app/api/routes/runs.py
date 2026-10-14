"""运行状态查询（API-09，FR-29 / FR-30）。

只读接口：前端运行监控页轮询这里拿「跑得怎么样」。

**这里不返回能力令牌**：令牌在 Commit 时随 Outbox 交给执行层，
它是一次性凭据而不是可查询的状态。运行事实里也只留 ``capability_id``。
"""

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, ensure_role, require_project_member
from app.core.errors import ResourceNotFound
from app.models.execution import ExecutionRun, ExecutionRunStatus
from app.models.identity import Membership, User, UserRole
from app.schemas.execution import ExecutionRunOut
from app.services.execution import request_cancel

router = APIRouter(prefix="/runs", tags=["runs"])


def _authorized_run(
    db: Session, run_id: uuid.UUID, user: User
) -> tuple[ExecutionRun, Membership]:
    run = db.get(ExecutionRun, run_id)
    if run is None:
        raise ResourceNotFound("运行记录不存在")
    return run, require_project_member(run.project_id, user, db)


@router.get("", response_model=list[ExecutionRunOut])
def list_runs(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    run_status: str | None = Query(default=None, alias="status"),
) -> list[ExecutionRun]:
    require_project_member(project_id, user, db)

    stmt = select(ExecutionRun).where(ExecutionRun.project_id == project_id)
    if run_status:
        stmt = stmt.where(ExecutionRun.status == ExecutionRunStatus(run_status))
    stmt = stmt.order_by(ExecutionRun.created_at.desc()).limit(100)
    return list(db.scalars(stmt))


@router.get("/{run_id}", response_model=ExecutionRunOut)
def get_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ExecutionRun:
    run, _ = _authorized_run(db, run_id, user)
    return run


@router.post("/{run_id}/cancel", response_model=ExecutionRunOut)
def cancel_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ExecutionRun:
    """请求取消一次运行（FR-29）。

    **是请求而不是命令**：运行中的作业正在数据面上搬运数据，从控制面强杀会留下
    未知残留。真正的停止由持有租约的 Worker 在安全点上完成，因此这里返回的
    可能仍是 ``running`` 且带着 ``cancel_requested=true``——前端据此展示
    「正在停止」而不是「已停止」。
    """
    run, membership = _authorized_run(db, run_id, user)
    ensure_role(membership, UserRole.OPERATOR)

    return request_cancel(db, run=run, user_id=user.id)
