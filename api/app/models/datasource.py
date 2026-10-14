"""数据源与元数据探查模型（FR-01 ~ FR-07）。

对应 SPEC §5 的 ``DATASOURCE`` 与 ``METADATA_PROFILE`` 实体，并做了两处细化：
  - ``Datasource.config`` 承载非敏感连接参数（host/port/database/schema）；
  - ``Datasource.credential_fingerprint`` 记录凭据摘要，用于审计与轮转比对（不可逆）。
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.types import JsonColumn


class DatasourceType(enum.StrEnum):
    """FR-01 异构数据库 + FR-02 文件 / API 数据源。"""

    MYSQL = "mysql"
    POSTGRESQL = "postgresql"
    ORACLE = "oracle"
    DORIS = "doris"
    CLICKHOUSE = "clickhouse"
    CSV = "csv"
    EXCEL = "excel"
    JSON = "json"
    PARQUET = "parquet"
    S3 = "s3"
    REST_API = "rest_api"


class DatasourceStatus(enum.StrEnum):
    REGISTERED = "registered"  # 已登记，未做连通性测试
    CONNECTED = "connected"  # 连通性测试通过
    UNREACHABLE = "unreachable"  # 连通性测试失败
    PROFILED = "profiled"  # 元数据探查已完成


class Datasource(Base, TimestampMixin):
    __tablename__ = "datasources"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[DatasourceType] = mapped_column(
        Enum(DatasourceType, name="datasource_type"), nullable=False
    )
    # 非敏感连接参数；**任何口令类字段都不得进入本列**（FR-03）
    config: Mapped[dict] = mapped_column(JsonColumn, default=dict, nullable=False)
    # 凭据引用：明文只存在于凭据服务，此处仅存句柄
    credential_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credential_fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[DatasourceStatus] = mapped_column(
        Enum(DatasourceStatus, name="datasource_status"),
        default=DatasourceStatus.REGISTERED,
        nullable=False,
    )

    profiles: Mapped[list["MetadataProfile"]] = relationship(
        back_populates="datasource", cascade="all, delete-orphan"
    )

    @property
    def has_credential(self) -> bool:
        """对外只暴露「是否已配置凭据」，不暴露引用本身。"""
        return self.credential_ref is not None


class MetadataProfile(Base, TimestampMixin):
    """一次元数据探查的留档（FR-05 / FR-07）。

    探查是**只读**的：只读 Schema 与少量样本，样本值经脱敏后才落库（FR-06）。
    同一数据源可有多个 Profile，按 ``sampled_at`` 形成版本序列，供审批环节对照。
    """

    __tablename__ = "metadata_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    datasource_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("datasources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # {"tables": [{"name", "columns": [...], "max_level"}], "table_count", "column_count"}
    schema_json: Mapped[dict] = mapped_column(JsonColumn, nullable=False)
    # {"<表名>": {"max_level": "L4", "by_level": {"L3": [...], "L4": [...]}}}
    sensitivity_tags: Mapped[dict] = mapped_column(JsonColumn, nullable=False)
    # 越级规则结论：整个数据源按最高级别对待（PRD 附录 C）
    max_level: Mapped[str] = mapped_column(String(2), nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 本次探查实际扫描的表数，用于说明覆盖范围（未扫到 ≠ 不存在）
    tables_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    datasource: Mapped["Datasource"] = relationship(back_populates="profiles")
