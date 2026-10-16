"""版本契约（FR-18 ~ FR-20）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.pipeline import QualityRule


class QualityRulesUpdate(BaseModel):
    """草稿版本的人工质量契约；不接受模型输出替代人工决定。"""

    rules: list[QualityRule] = Field(max_length=100)


class VersionSummaryOut(BaseModel):
    """列表用——不含方案与配置正文。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    version_no: int
    status: str
    sha256: str | None
    frozen_at: datetime | None
    created_at: datetime


class VersionOut(VersionSummaryOut):
    """详情用——含冻结时的完整快照与门禁报告。"""

    run_id: uuid.UUID | None
    # 生成与冻结共同依据的探查记录。审批界面据此展示「本次校验基于哪一版 Schema」，
    # 也让「生成后又重新探查过」这件事变得可见（FR-19 依据一致性）。
    source_profile_id: uuid.UUID | None
    target_profile_id: uuid.UUID | None
    etl_plan: dict[str, Any]
    config: str
    quality_rules: list[Any]
    gate_report: dict[str, Any] | None


class VersionDiffOut(BaseModel):
    """版本间差异（FR-20）：方案 / 配置 / 质量规则三个维度。"""

    left: VersionSummaryOut
    right: VersionSummaryOut
    fingerprint_equal: bool
    plan_changed: bool
    config_changed: bool
    quality_rules_changed: bool
    mappings_added: list[dict[str, Any]]
    mappings_removed: list[dict[str, Any]]
    mappings_changed: list[dict[str, Any]]
    steps_added: list[str]
    steps_removed: list[str]
    config_diff: list[str]
