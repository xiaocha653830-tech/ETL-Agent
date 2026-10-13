"""认证路由：注册 / 登录 / 当前用户。"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.models.identity import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: DbSession) -> User:
    if db.scalar(select(User).where(User.email == payload.email)) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "邮箱已注册")

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        display_name=payload.display_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == payload.email))
    # 用户不存在与密码错误返回同一提示，避免账号枚举
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已停用")

    token = create_access_token(subject=str(user.id))
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_expire_minutes * 60,
    )


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> User:
    return user
