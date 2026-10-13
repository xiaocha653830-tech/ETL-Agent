"""密码哈希与会话令牌。

说明：本模块只处理【会话认证】（登录态）。
执行授权所用的【单次能力令牌 Capability】是独立机制（Ed25519 签名、绑定制品、
5 分钟有效、仅可消费一次），将在 T-10 安全内核中实现（FR-26 / FR-27）。
"""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import settings

ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(subject: str, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.app_secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解析令牌；无效或过期会抛 jwt.PyJWTError，由调用方转为 401。"""
    return jwt.decode(token, settings.app_secret_key, algorithms=[ALGORITHM])
