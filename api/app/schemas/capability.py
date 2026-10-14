"""安全内核 M7 的请求 / 响应契约（FR-26 / FR-27）。

**不承载业务语义**：这里的字段只有「主体 / 工具 / 环境 / 制品」四个通用维度，
以及令牌自身的时间与消费状态，不认识数据源、审批、版本等任何业务概念。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.capability import CAPABILITY_TTL_SECONDS


class BindingContext(BaseModel):
    """四维绑定的期望值（校验方声明「我打算做什么」）。

    与令牌声明逐维比对，任一不符即拒绝（FR-26）。
    """

    model_config = ConfigDict(frozen=True)

    tool: str
    subject: str
    environment: str
    artifact: str

    @classmethod
    def from_claims(cls, claims: "CapabilityClaims") -> "BindingContext":
        return cls(
            tool=claims.tool,
            subject=claims.subject,
            environment=claims.environment,
            artifact=claims.artifact,
        )


class CapabilityClaims(BaseModel):
    """令牌载荷 —— 全部字段都进入签名，篡改任一字段都会导致验签失败。"""

    model_config = ConfigDict(frozen=True)

    jti: uuid.UUID  # 令牌唯一标识，与库中 CAPABILITY.id 对应
    tool: str
    subject: str
    environment: str
    artifact: str
    iat: int  # 签发时间（Unix 秒）
    exp: int  # 过期时间（Unix 秒，iat + ≤300）


class CapabilityIssueRequest(BaseModel):
    """签发请求（供 T-12 Commit 收口调用）。"""

    tool: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=255)
    environment: str = Field(min_length=1, max_length=128)
    artifact: str = Field(min_length=1, max_length=255)
    preparation_id: uuid.UUID | None = None
    ttl_seconds: int = Field(default=CAPABILITY_TTL_SECONDS, gt=0, le=CAPABILITY_TTL_SECONDS)


class CapabilityTokenOut(BaseModel):
    """签发结果 —— 令牌串只会在此刻出现一次。"""

    token: str
    capability_id: uuid.UUID
    kid: str
    tool: str
    subject: str
    environment: str
    artifact: str
    expires_at: datetime


class ToolInvocationOut(BaseModel):
    """副作用工具调用记录视图（T-20 证据账本的输入形态）。

    只保留参数指纹而非原始参数，避免把可能的敏感值写进证据。
    """

    capability_id: uuid.UUID
    tool: str
    subject: str
    environment: str
    artifact: str
    arguments_digest: str
    side_effect: bool
    succeeded: bool
    invoked_at: datetime
