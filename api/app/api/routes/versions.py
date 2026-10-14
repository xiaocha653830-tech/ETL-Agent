"""版本冻结与 Diff（API-05，FR-18 ~ FR-20）。

冻结是「生成」与「审批」之间的闸门：只有确定性门禁全部通过的方案，
才会变成不可变版本并进入审批流程（SPEC §7.1 主链路）。
"""

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, ensure_role, require_project_member
from app.api.routes.pipelines import build_endpoint
from app.core.errors import ResourceNotFound
from app.models.identity import Membership, User, UserRole
from app.models.version import PipelineVersion, VersionStatus
from app.schemas.version import VersionDiffOut, VersionOut, VersionSummaryOut
from app.services.gate import run_gate
from app.services.versioning import diff_versions, freeze_version

router = APIRouter(prefix="/versions", tags=["versions"])


def _authorized_version(
    db: Session, version_id: uuid.UUID, user: User
) -> tuple[PipelineVersion, Membership]:
    version = db.get(PipelineVersion, version_id)
    if version is None:
        raise ResourceNotFound("版本不存在")
    membership = require_project_member(version.project_id, user, db)
    return version, membership


def _endpoint_for(db: Session, datasource_id: str | None, user: User):
    """按版本里记录的源 / 目标数据源还原探查上下文，供 Schema 对齐检查使用。"""
    if not datasource_id:
        return None
    try:
        return build_endpoint(db, uuid.UUID(str(datasource_id)), user)
    except Exception:  # noqa: BLE001 —— 数据源已删除等情况不该阻断冻结
        return None


@router.post("/{version_id}/freeze", response_model=VersionOut)
def freeze(
    version_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> PipelineVersion:
    """API-05：执行确定性门禁并冻结版本（FR-18 / FR-19）。

    门禁不通过时返回 409 并把版本标记为 ``rejected``，**不会产生半冻结版本**。
    """
    version, membership = _authorized_version(db, version_id, user)
    ensure_role(membership, UserRole.MAKER)

    source = _endpoint_for(db, (version.etl_plan or {}).get("source_datasource_id"), user)
    target = _endpoint_for(db, (version.etl_plan or {}).get("target_datasource_id"), user)

    report = run_gate(
        plan=version.etl_plan or {},
        config=version.config or "",
        source=source,
        target=target,
    )
    return freeze_version(db, version=version, report=report)


@router.get("", response_model=list[VersionSummaryOut])
def list_versions(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    status: str | None = Query(default=None),
) -> list[PipelineVersion]:
    require_project_member(project_id, user, db)

    stmt = select(PipelineVersion).where(PipelineVersion.project_id == project_id)
    if status:
        stmt = stmt.where(PipelineVersion.status == VersionStatus(status))
    stmt = stmt.order_by(PipelineVersion.version_no.desc())
    return list(db.scalars(stmt))


@router.get("/{version_id}", response_model=VersionOut)
def get_version(
    version_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> PipelineVersion:
    version, _ = _authorized_version(db, version_id, user)
    return version


@router.get("/{version_id}/diff", response_model=VersionDiffOut)
def diff(
    version_id: uuid.UUID,
    against: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict:
    """版本间 Diff（FR-20）。

    ``version_id`` 是基准（较旧），``against`` 是对照（较新）。
    """
    left, _ = _authorized_version(db, version_id, user)
    right, _ = _authorized_version(db, against, user)

    if left.project_id != right.project_id:
        raise ResourceNotFound("两个版本不属于同一项目，无法比较")

    return diff_versions(left, right)
