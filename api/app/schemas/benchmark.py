import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str = Field(default="v1", pattern=r"^v[1-9][0-9]{0,5}$")
    backend: Literal["injected", "deepseek"] = "injected"


class BenchmarkSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    created_by: uuid.UUID | None
    dataset_version: str
    dataset_sha256: str
    backend: str
    cases_total: int
    cases_passed: int
    operator_accuracy: float
    attribute_accuracy: float
    guardrail_rate: float
    passed: bool
    duration_ms: int


class BenchmarkOut(BenchmarkSummaryOut):
    detail: dict[str, Any]
