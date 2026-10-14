"""项目与成员管理契约（FR-45）。"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.identity import UserRole


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(default="default", min_length=1, max_length=64)


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    tenant_id: str
    created_at: datetime


class MemberAdd(BaseModel):
    """按邮箱邀请成员 —— 目标用户须已注册。"""

    email: EmailStr
    role: UserRole


class MemberRoleUpdate(BaseModel):
    role: UserRole


class MemberOut(BaseModel):
    """成员视图：联表 User 后的扁平结构，供前端成员列表直接渲染。"""

    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: UserRole
    created_at: datetime
