"""FastAPI 公共依赖：认证与多租户隔离。

对应 SPEC §3 M1 控制面 API 网关：统一路由、认证鉴权、RBAC 校验。
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.identity import Membership, User

bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DbSession,
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少认证凭据")

    try:
        payload = decode_access_token(credentials.credentials)
        user_id = uuid.UUID(str(payload.get("sub")))
    except Exception as exc:  # noqa: BLE001 — 统一转为 401，不泄露解析细节
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效或已过期") from exc

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_project_member(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> Membership:
    """租户隔离：确认当前用户是目标项目成员，否则拒绝（FR-44）。

    任何涉及具体项目资源的接口都应依赖本函数，确保跨租户访问在写入前被拦截。
    """
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id,
            Membership.project_id == project_id,
        )
    )
    if membership is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "无权访问该项目")
    return membership
