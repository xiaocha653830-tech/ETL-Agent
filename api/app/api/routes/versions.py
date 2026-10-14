"""版本冻结与 Diff（API-05，FR-18 ~ FR-20）。

冻结是「生成」与「审批」之间的闸门：只有确定性门禁全部通过的方案，
才会变成不可变版本并进入审批流程（SPEC §7.1 主链路）。
"""

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, ensure_role, require_project_member
from app.api.routes.pipelines import build_endpoint_from_profile
from app.core.config import settings
from app.core.errors import ResourceNotFound
from app.models.identity import Membership, User, UserRole
from app.models.version import PipelineVersion, VersionStatus
from app.schemas.version import VersionDiffOut, VersionOut, VersionSummaryOut
from app.services.endpoint_context import EndpointContext
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


def _endpoint_for(
    db: Session, version: PipelineVersion, *, is_source: bool, user: User
) -> EndpointContext | None:
    """按版本**绑定的探查记录**还原一端上下文，供 Schema 对齐检查使用。

    冻结必须复用生成时那一版 Schema，不能重新取「最新 Profile」。三种情况刻意分开：

    - **从未探查**（生成时就没有绑定）→ 返回 ``tables`` 为空的空上下文，
      由门禁按「缺探查结果」处理（开发环境 ``skipped``、生产环境 ``failed``）；
    - **绑定过但记录已不可用**（数据源或探查记录被删）→ 返回 ``None``，
      门禁按 ``failed`` 处理：这是「无从校验」，不是「没校验」；
    - **跨租户 / 权限异常**→ 直接向上冒泡。那是对调用方的明确拒绝，绝不能悄悄
      降级成「本项没检查」，否则相当于用一次权限错误换一次放行。
    """
    profile_id = version.source_profile_id if is_source else version.target_profile_id
    plan = version.etl_plan or {}
    side = "source" if is_source else "target"

    if profile_id is None:
        return EndpointContext(
            datasource_id=str(plan.get(f"{side}_datasource_id") or ""),
            name="",
            type="",
            config={},
            tables=[],
        )

    try:
        return build_endpoint_from_profile(db, profile_id, user)
    except ResourceNotFound:
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

    source = _endpoint_for(db, version, is_source=True, user=user)
    target = _endpoint_for(db, version, is_source=False, user=user)

    report = run_gate(
        plan=version.etl_plan or {},
        config=version.config or "",
        source=source,
        target=target,
        # 开发环境允许「没探查就冻结」以便联调；其余环境必须有探查依据才放行。
        strict=settings.app_env != "dev",
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
