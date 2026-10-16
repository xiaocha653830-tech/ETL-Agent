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


class QualityReportOut(BaseModel):
    """一次运行的质量结论（FR-33 / FR-40，SPEC §5 ``QUALITY_REPORT``）。

    ``rejected_ratio`` 由服务端派生（拒绝行数 / 源端行数），不在库里另存一列——
    它和那两个数字必须是同一个值。
    """

    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    source_rows: int
    valid_rows: int
    rejected_rows: int
    rejected_ratio: float
    # 错误码到行数的分布，如 ``{"USER_NAME_REGEX": 1}``——这是报告里唯一
    # 指向「该去改哪条规则」的信息。
    error_dist: dict[str, int]
    # 错误明细所在的表；没有质量规则时为 None
    error_table: str | None


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
    # 发布与回滚两个事实（FR-34 / FR-38）：前端凭 ``published_at`` 判断
    # 「这次运行动过正式表没有」，凭 ``rolled_back_at`` 判断残留是否已清理。
    # 不让前端去猜——这两个判断都关系到能不能点回滚。
    published_at: datetime | None
    rolled_back_at: datetime | None
    # 质量结论（FR-33 / FR-40）。**为 None 表示这次运行没有质量契约**，
    # 与「报告显示零拒绝」是两回事——运行列表不该让两者看起来一样。
    quality_report: QualityReportOut | None = None
    created_at: datetime


class RollbackResultOut(BaseModel):
    """API-10 受理结果（FR-38）。

    ``executed`` 区分「本次真的清了」与「此前已经清过」：重复点击回滚是安全的，
    但调用方有权知道这一次到底有没有动过手。
    """

    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    executed: bool
    dropped: list[str]
    reason: str
