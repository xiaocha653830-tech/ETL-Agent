"""数据源与元数据探查契约（FR-01 ~ FR-07）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.datasource import DatasourceStatus, DatasourceType


class DatasourceCreate(BaseModel):
    """登记请求。

    ``credential`` 是**唯一一次**出现明文凭据的地方：服务端收到后立即转入
    凭据服务（FR-03），响应与后续查询都只回 ``has_credential``。
    """

    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=128)
    type: DatasourceType
    config: dict[str, Any] = Field(default_factory=dict)
    credential: dict[str, str] | None = None


class DatasourceOut(BaseModel):
    """数据源对外视图 —— **不含 credential_ref，更不含任何明文凭据**。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    type: DatasourceType
    config: dict[str, Any]
    status: DatasourceStatus
    has_credential: bool
    credential_fingerprint: str | None
    created_at: datetime


class ConnectionTestOut(BaseModel):
    ok: bool
    latency_ms: int
    message: str


class MetadataProfileOut(BaseModel):
    """探查结果。

    ``schema_json`` 里的样本值在落库前已脱敏（FR-06），
    因此本响应可以安全地直接返回给前端。
    """

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: uuid.UUID
    datasource_id: uuid.UUID
    max_level: str
    tables_scanned: int
    sampled_at: datetime
    # 内部字段名不能叫 schema_json —— 会遮蔽 BaseModel.schema_json()（Pydantic v1 兼容方法），
    # 因此内部改名并用双向 alias 保持对外契约不变。
    schema_snapshot: dict[str, Any] = Field(
        validation_alias="schema_json", serialization_alias="schema_json"
    )
    sensitivity_tags: dict[str, Any]
