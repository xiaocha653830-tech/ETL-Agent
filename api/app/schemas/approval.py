"""Prepare 准备单与双维审批契约（FR-21 ~ FR-24）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.approval import ApprovalDecision


class PreparationCreateRequest(BaseModel):
    """API-06 请求体：为某个已冻结版本生成准备单。"""

    version_id: uuid.UUID


class ApprovalOut(BaseModel):
    """一个审批槽位。``status`` 直接给出 pending / approved / rejected。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dimension: str
    status: str
    approver_id: uuid.UUID | None
    decision: str | None
    comment: str | None
    decided_at: datetime | None
    expires_at: datetime


class PreparationOut(BaseModel):
    """准备单详情（审批台与审计台的共同数据源）。

    ``missing_dimensions`` 是服务端算出的「还缺哪一维」，前端不必自己推导——
    PRD 第 7 章要求服务端独立校验，前端不做业务判定。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    version_id: uuid.UUID
    created_by: uuid.UUID
    fingerprint: str
    risk_level: str
    risk_reasons: list[str]
    impact_scope: dict[str, Any]
    data_grading: dict[str, Any]
    budget: dict[str, Any]
    rollback_plan: dict[str, Any]
    status: str
    expires_at: datetime
    committed_at: datetime | None
    created_at: datetime
    approvals: list[ApprovalOut]
    missing_dimensions: list[str]


class ApprovalDecisionRequest(BaseModel):
    """API-07 请求体：提交一次审批决定。"""

    decision: ApprovalDecision
    comment: str | None = Field(default=None, max_length=512)


class PreparationSummaryOut(BaseModel):
    """列表用——不含准备单四项正文。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version_id: uuid.UUID
    status: str
    risk_level: str
    fingerprint: str
    expires_at: datetime
    created_at: datetime
