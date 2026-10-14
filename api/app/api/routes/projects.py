"""项目管理与成员角色管理（FR-45，对应 US-09）。

权限约定（PRD 第 2 章五角色矩阵的落地）：
  - 项目**创建者**自动成为该项目的 MAKER 成员；
  - 成员与角色管理**仅限 MAKER**；
  - 任何成员都可查看项目详情与成员列表；
  - 跨租户访问由 ``require_project_member`` 在写入前拦截（FR-44）。
"""

import uuid

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, ProjectMember, ensure_role
from app.core.errors import ResourceConflict, ResourceNotFound
from app.models.identity import Membership, Project, User, UserRole
from app.schemas.project import MemberAdd, MemberOut, MemberRoleUpdate, ProjectCreate, ProjectOut

router = APIRouter(prefix="/projects", tags=["projects"])


# --------------------------------------------------------------------------
# 内部辅助
# --------------------------------------------------------------------------


def _member_rows(db: Session, project_id: uuid.UUID) -> list[tuple[Membership, User]]:
    """联表取成员与用户，一次查询避免 N+1。"""
    stmt = (
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.project_id == project_id)
        .order_by(Membership.created_at)
    )
    return [tuple(row) for row in db.execute(stmt).all()]


def _to_member_out(membership: Membership, user: User) -> MemberOut:
    return MemberOut(
        id=membership.id,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=membership.role,
        created_at=membership.created_at,
    )


def _get_membership_in_project(
    db: Session, project_id: uuid.UUID, membership_id: uuid.UUID
) -> Membership:
    """按项目边界取成员，防止拿其他项目的 membership_id 越权操作。"""
    membership = db.get(Membership, membership_id)
    if membership is None or membership.project_id != project_id:
        raise ResourceNotFound("成员不存在")
    return membership


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, user: CurrentUser, db: DbSession) -> Project:
    """创建项目 —— 创建者自动成为 MAKER 成员。"""
    project = Project(name=payload.name, tenant_id=payload.tenant_id)
    db.add(project)
    db.flush()  # 先拿到 project.id 再写成员关系，保证两者处于同一事务
    db.add(Membership(user_id=user.id, project_id=project.id, role=UserRole.MAKER))
    db.commit()
    db.refresh(project)
    return project


@router.get("", response_model=list[ProjectOut])
def list_my_projects(user: CurrentUser, db: DbSession) -> list[Project]:
    """列出当前用户参与的项目。

    以成员关系为唯一入口，天然限定在当前用户可见的范围内（FR-44）。
    """
    stmt = (
        select(Project)
        .join(Membership, Membership.project_id == Project.id)
        .where(Membership.user_id == user.id)
        .order_by(Project.created_at)
    )
    return list(db.scalars(stmt))


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(membership: ProjectMember, db: DbSession) -> Project:
    project = db.get(Project, membership.project_id)
    if project is None:
        raise ResourceNotFound("项目不存在")
    return project


# --------------------------------------------------------------------------
# 成员与角色
# --------------------------------------------------------------------------


@router.get("/{project_id}/members", response_model=list[MemberOut])
def list_members(membership: ProjectMember, db: DbSession) -> list[MemberOut]:
    return [_to_member_out(m, u) for m, u in _member_rows(db, membership.project_id)]


@router.post(
    "/{project_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
)
def add_member(payload: MemberAdd, membership: ProjectMember, db: DbSession) -> MemberOut:
    """按邮箱邀请成员 —— 目标用户须已注册。"""
    ensure_role(membership, UserRole.MAKER)

    user = db.scalar(select(User).where(User.email == payload.email))
    if user is None:
        raise ResourceNotFound("该邮箱尚未注册用户")

    existed = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.project_id == membership.project_id,
        )
    )
    if existed is not None:
        raise ResourceConflict("该用户已是项目成员")

    created = Membership(
        user_id=user.id, project_id=membership.project_id, role=payload.role
    )
    db.add(created)
    db.commit()
    db.refresh(created)
    return _to_member_out(created, user)


@router.patch("/{project_id}/members/{membership_id}", response_model=MemberOut)
def update_member_role(
    membership_id: uuid.UUID,
    payload: MemberRoleUpdate,
    membership: ProjectMember,
    db: DbSession,
) -> MemberOut:
    ensure_role(membership, UserRole.MAKER)

    target = _get_membership_in_project(db, membership.project_id, membership_id)
    target.role = payload.role
    db.commit()
    db.refresh(target)

    user = db.get(User, target.user_id)
    if user is None:
        raise ResourceNotFound("用户不存在")
    return _to_member_out(target, user)


@router.delete(
    "/{project_id}/members/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def remove_member(
    membership_id: uuid.UUID,
    membership: ProjectMember,
    db: DbSession,
) -> Response:
    ensure_role(membership, UserRole.MAKER)

    target = _get_membership_in_project(db, membership.project_id, membership_id)
    if target.user_id == membership.user_id:
        # 防止项目失去管理者，也避免误操作把自己踢出
        raise ResourceConflict("不能移除自己")

    db.delete(target)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
