"""执行层契约（FR-28 ~ FR-30）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExecutionCommand(BaseModel):
    """交给执行层的一条受管执行命令 —— 即 Outbox 事件的载荷。

    **自描述**：这一份载荷要能让执行方独立回答「执行什么、在哪执行、凭什么执行」，
    不必回查多张业务表。``capability_token`` 只在这里出现——它随事件交给执行层，
    不进入任何面向用户的响应，也不落在运行事实表里。
    """

    model_config = ConfigDict(frozen=True)

    run_id: uuid.UUID
    version_id: uuid.UUID
    preparation_id: uuid.UUID

    capability_token: str
    capability_id: uuid.UUID
    tool: str
    subject: str
    environment: str
    artifact: str

    target_table: str
    shadow_table: str
    budget: dict[str, Any]


class CommitRequest(BaseModel):
    """API-08 请求体。

    ``expected_fingerprint`` 是调用方持有的准备单指纹。服务端**不以它为准**
    （真值以库中版本为准），而是用它来发现「调用方看到的东西变了」——
    这是三阶段协议「复核」这一步的字面含义。
    """

    preparation_id: uuid.UUID
    expected_fingerprint: str = Field(min_length=64, max_length=64)


class CommitAcceptedOut(BaseModel):
    """Commit 受理结果。

    **不返回令牌**：令牌随 Outbox 交给执行层，调用方拿到 ``run_id`` 去查运行状态即可。
    返回 ``capability_expires_at`` 是为了让操作员知道「这次授权还剩多久有效」。
    """

    run_id: uuid.UUID
    status: str
    preparation_id: uuid.UUID
    capability_id: uuid.UUID
    capability_expires_at: datetime
    environment: str
    artifact: str


class ExecutionRunOut(BaseModel):
    """运行状态与度量（API-09 / FR-29 / FR-30）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    version_id: uuid.UUID
    preparation_id: uuid.UUID
    status: str
    environment: str
    artifact: str
    read_rows: int
    written_bytes: int
    rejected_rows: int
    cancel_requested: bool
    worker_lease_owner: uuid.UUID | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
