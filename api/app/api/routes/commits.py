"""Commit 接口（API-08，FR-25 ~ FR-28）。

三段式协议的收口动作，独立于审批台（FR-25）：审批全部完成后由**系统操作员**
发起，而不是由审批人顺手点一下——提出申请、批准申请、放行执行是三个人的三件事。
"""


from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession, ensure_role, require_project_member
from app.core.errors import ResourceNotFound
from app.models.approval import Preparation
from app.models.identity import UserRole
from app.schemas.execution import CommitAcceptedOut, CommitRequest
from app.services.commit import commit_preparation

router = APIRouter(prefix="/commits", tags=["commits"])


@router.post("", response_model=CommitAcceptedOut, status_code=status.HTTP_202_ACCEPTED)
def create_commit(
    payload: CommitRequest, user: CurrentUser, db: DbSession
) -> CommitAcceptedOut:
    """API-08：复核审批与指纹 → 签发能力令牌 → 同事务写运行事实与 Outbox。

    返回 202 而非 200：任务尚未执行，只是**已受理**。真正的执行由 Worker 经
    Outbox 认领后开始，调用方拿 ``run_id`` 去查状态。
    """
    preparation = db.get(Preparation, payload.preparation_id)
    if preparation is None:
        raise ResourceNotFound("准备单不存在")

    membership = require_project_member(preparation.project_id, user, db)
    # 操作员不可改 Pipeline、不可审批、不可冻结——只被授权「把已经批准的事放行」
    ensure_role(membership, UserRole.OPERATOR)

    result = commit_preparation(
        db,
        preparation=preparation,
        user_id=user.id,
        expected_fingerprint=payload.expected_fingerprint,
    )
    return CommitAcceptedOut(
        run_id=result.run.id,
        status=result.run.status.value,
        preparation_id=preparation.id,
        capability_id=result.capability.capability_id,
        capability_expires_at=result.capability.expires_at,
        environment=result.run.environment,
        artifact=result.run.artifact,
    )
