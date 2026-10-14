"""FastAPI 公共依赖：认证与多租户隔离。

对应 SPEC §3 M1 控制面 API 网关：统一路由、认证鉴权、RBAC 校验。
"""

import uuid
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AuthTokenInvalid, CrossTenantDenied, PermissionDenied
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.identity import Membership, User, UserRole

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DbSession,
) -> User:
    if credentials is None:
        raise AuthTokenInvalid("缺少认证凭据")

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = uuid.UUID(str(payload.get("sub")))
    except Exception as exc:  # noqa: BLE001 — 统一转为 401，不泄露解析细节
        raise AuthTokenInvalid() from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthTokenInvalid("用户不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_project_member(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> Membership:
    """租户隔离：确认当前用户是目标项目成员，否则拒绝（FR-44）。

    任何涉及具体项目资源的接口都应依赖本函数，确保跨租户访问在**写入前**被拦截。
    路径参数名必须为 ``project_id``，与各路由的 ``/{project_id}`` 保持一致。
    """
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.project_id == project_id,
        )
    )
    if membership is None:
        raise CrossTenantDenied()
    return membership


ProjectMember = Annotated[Membership, Depends(require_project_member)]


def ensure_role(membership: Membership, *allowed: UserRole) -> None:
    """项目内角色校验（RBAC）。

    五角色矩阵见 PRD 第 2 章。注意：**职责分离（FR-24）不在此处判定**——
    它约束的是「同一人不得兼任 maker 与 checker」，属于审批环节的校验（T-11）。
    """
    if membership.role not in allowed:
        raise PermissionDenied(
            "该操作需要角色：" + " / ".join(role.value for role in allowed)
        )
