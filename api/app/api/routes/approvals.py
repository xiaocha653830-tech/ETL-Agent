"""Prepare 与双维审批接口（API-06 / API-07，FR-21 ~ FR-24）。

**Prepare 零外部副作用**（FR-22）：生成准备单只是一次控制面写库，
不连数据源、不提交作业。审批台展示的「影响范围 / 数据分级 / 执行预算 / 回滚方案」
全部来自已冻结版本的内容，不依赖任何运行期探测。

**Commit 不在这里**（FR-25）：它是一个独立动作，落在审批全部完成之后，
见 ``app/api/routes/commits.py``。
"""

import uuid

from fastapi import APIRouter, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, ensure_role, require_project_member
from app.core.errors import ResourceNotFound
from app.models.approval import Approval, Preparation, PreparationStatus
from app.models.identity import Membership, User, UserRole
from app.models.version import PipelineVersion
from app.schemas.approval import (
    ApprovalDecisionRequest,
    PreparationCreateRequest,
    PreparationOut,
    PreparationSummaryOut,
)
from app.services.approval import create_preparation, decide

preparation_router = APIRouter(prefix="/preparations", tags=["preparations"])
approval_router = APIRouter(prefix="/approvals", tags=["approvals"])


def _authorized_preparation(
    db: Session, preparation_id: uuid.UUID, user: User
) -> tuple[Preparation, Membership]:
    preparation = db.get(Preparation, preparation_id)
    if preparation is None:
        raise ResourceNotFound("准备单不存在")
    membership = require_project_member(preparation.project_id, user, db)
    return preparation, membership


@preparation_router.post(
    "", response_model=PreparationOut, status_code=status.HTTP_201_CREATED
)
def create(
    payload: PreparationCreateRequest, user: CurrentUser, db: DbSession
) -> Preparation:
    """API-06：为已冻结版本生成准备单并开启双维审批（FR-21 / FR-22）。

    反复调用返回同一份准备单（幂等）——审批人多次打开审批台不应产生多份准备单。
    """
    version = db.get(PipelineVersion, payload.version_id)
    if version is None:
        raise ResourceNotFound("版本不存在")

    membership = require_project_member(version.project_id, user, db)
    # 只有发起方能提交审批；审批人自己生成准备单等于把「申请」与「审批」并到一处
    ensure_role(membership, UserRole.MAKER)

    return create_preparation(db, version=version, user_id=user.id)


@preparation_router.get("", response_model=list[PreparationSummaryOut])
def list_preparations(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    prep_status: str | None = Query(default=None, alias="status"),
) -> list[Preparation]:
    require_project_member(project_id, user, db)

    stmt = select(Preparation).where(Preparation.project_id == project_id)
    if prep_status:
        stmt = stmt.where(Preparation.status == PreparationStatus(prep_status))
    stmt = stmt.order_by(Preparation.created_at.desc())
    return list(db.scalars(stmt))


@preparation_router.get("/{preparation_id}", response_model=PreparationOut)
def get_preparation(
    preparation_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Preparation:
    preparation, _ = _authorized_preparation(db, preparation_id, user)
    return preparation


@approval_router.post("/{approval_id}/decide", response_model=PreparationOut)
def submit_decision(
    approval_id: uuid.UUID,
    payload: ApprovalDecisionRequest,
    user: CurrentUser,
    db: DbSession,
) -> Preparation:
    """API-07：提交一次审批决定（FR-23 / FR-24）。

    返回更新后的**整份准备单**：审批台的下一步动作取决于「另一维还差不差」，
    只回一个槽位会逼前端再查一次。
    """
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise ResourceNotFound("审批槽位不存在")

    membership = require_project_member(approval.project_id, user, db)
    return decide(
        db,
        approval=approval,
        membership=membership,
        user_id=user.id,
        decision=payload.decision,
        comment=payload.comment,
    )
