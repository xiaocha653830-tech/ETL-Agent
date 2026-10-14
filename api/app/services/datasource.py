"""数据源连通性与只读元数据探查（FR-04 ~ FR-07，SPEC §3 M2）。

**模块边界**（SPEC §3 M2 的「不做什么」）：不返回明文数据、不透传凭据。
因此本模块：

  - 凭据只在构造连接时于内存中拼进 URL，**不落库、不写日志**；
  - 采样值一律经 :func:`sensitivity.mask_value` 脱敏后才进入返回值；
  - 探查是**只读**的，且受 ``tables_limit`` / ``sample_size`` 预算约束，
    避免对生产库造成压力（PRD 第 5 章非功能要求）。

探查结果的三层结构：字段级 → 表级 → 数据源级，
越级规则见 :func:`sensitivity.aggregate_level`（PRD 附录 C）。
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.engine import URL
from sqlalchemy.sql import column as sa_column
from sqlalchemy.sql import table as sa_table

from app.core.errors import DatasourceDriverUnavailable, DatasourceUnreachable
from app.models.datasource import DatasourceType
from app.services.sensitivity import (
    SensitivityLevel,
    aggregate_level,
    assess_column,
    mask_value,
)

logger = logging.getLogger(__name__)

# SQLAlchemy 方言名 —— 与 DatasourceType 的映射
_DIALECT_BY_TYPE: dict[DatasourceType, str] = {
    DatasourceType.MYSQL: "mysql+pymysql",
    DatasourceType.POSTGRESQL: "postgresql+psycopg",
    # Doris FE 兼容 MySQL 协议，走同一驱动
    DatasourceType.DORIS: "mysql+pymysql",
    DatasourceType.CLICKHOUSE: "clickhouse+http",
    DatasourceType.ORACLE: "oracle+oracledb",
}

# 文件 / API 类数据源不走数据库协议，其接入在后续阶段实现（FR-02）
_NON_DATABASE_TYPES = frozenset(
    {
        DatasourceType.CSV,
        DatasourceType.EXCEL,
        DatasourceType.JSON,
        DatasourceType.PARQUET,
        DatasourceType.S3,
        DatasourceType.REST_API,
    }
)


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionSpec:
    """构造连接所需的全部信息。

    ``secret`` 是**明文凭据**，由调用方从凭据服务临时解析得到，
    生命周期仅限本次调用，不得被持有、记录或序列化。
    """

    type: DatasourceType
    config: dict[str, Any] = field(default_factory=dict)
    secret: dict[str, str] = field(default_factory=dict)


def build_engine(spec: ConnectionSpec) -> Engine:
    """构造 SQLAlchemy 引擎（不做实际连接）。"""
    if spec.type in _NON_DATABASE_TYPES:
        raise DatasourceDriverUnavailable(
            f"{spec.type.value} 属于文件 / API 数据源，当前版本仅支持数据库类数据源"
        )

    drivername = _DIALECT_BY_TYPE.get(spec.type)
    if drivername is None:
        raise DatasourceDriverUnavailable(f"暂不支持的数据库类型：{spec.type.value}")

    host = str(spec.config.get("host", "")).strip()
    if not host:
        raise DatasourceUnreachable("连接配置缺少 host")

    port = spec.config.get("port")
    # 使用 URL.create 而不是字符串拼接，确保口令中的 @、: 等字符被驱动正确转义。
    url = URL.create(
        drivername=drivername,
        username=spec.secret.get("username") or None,
        password=spec.secret.get("password") or None,
        host=host,
        port=int(port) if port else None,
        database=str(spec.config["database"]) if spec.config.get("database") else None,
    )

    try:
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args=spec.config.get("connect_args") or {},
        )
    except ModuleNotFoundError as exc:
        # 方言的 DBAPI(如 pymysql) 未安装 —— 明确告知，而不是抛 500
        raise DatasourceDriverUnavailable(
            f"当前环境未安装 {spec.type.value} 的数据库驱动（{exc.name}）"
        ) from exc


def test_connection(spec: ConnectionSpec) -> int:
    """只读连通性测试（FR-04）：只执行 ``SELECT 1``，不读任何业务数据。

    返回往返耗时（毫秒）。失败时抛 :class:`DatasourceUnreachable`，
    **对外只暴露异常类名**，避免驱动把主机、账号等信息带进响应体。
    """
    engine = build_engine(spec)
    started = time.perf_counter()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 —— 驱动异常种类繁多，统一转为业务错误
        logger.warning("连通性测试失败 [%s]：%s", spec.type.value, exc)
        raise DatasourceUnreachable(f"连接失败（{exc.__class__.__name__}）") from exc
    finally:
        engine.dispose()
    return int((time.perf_counter() - started) * 1000)


# --------------------------------------------------------------------------
# 元数据读取
# --------------------------------------------------------------------------


class MetadataSource(Protocol):
    """只读元数据读取协议 —— 抽象出来是为了让探查逻辑可脱离真实数据库测试。"""

    def list_tables(self) -> list[str]: ...

    def list_columns(self, table: str) -> list[dict[str, Any]]: ...

    def sample_values(self, table: str, column: str, limit: int) -> list[str]: ...


class SqlAlchemyMetadataSource:
    """基于 SQLAlchemy inspector 的实现。"""

    def __init__(self, engine: Engine, schema: str | None = None) -> None:
        self._engine = engine
        self._schema = schema
        self._inspector = inspect(engine)

    def list_tables(self) -> list[str]:
        names = list(self._inspector.get_table_names(schema=self._schema))
        names += list(self._inspector.get_view_names(schema=self._schema))
        # 固定顺序让相同库结构生成稳定快照，避免无意义的版本差异。
        return sorted(names)

    def list_columns(self, table: str) -> list[dict[str, Any]]:
        return list(self._inspector.get_columns(table, schema=self._schema))

    def sample_values(self, table: str, column: str, limit: int) -> list[str]:
        """取出**原始**样本值；脱敏由 :func:`profile_datasource` 统一负责。

        单个字段采样失败（如二进制列、权限不足）不应中断整次探查。
        """
        # 通过 SQLAlchemy 标识符对象构造查询，避免把表名、字段名直接拼接进 SQL。
        col = sa_column(column)
        stmt = (
            select(col)
            .select_from(sa_table(table, col, schema=self._schema))
            .where(col.is_not(None))
            .limit(limit)
        )
        try:
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.info("字段采样跳过 %s.%s：%s", table, column, exc.__class__.__name__)
            return []
        return ["" if row[0] is None else str(row[0]) for row in rows]


# --------------------------------------------------------------------------
# 探查结果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnProfile:
    name: str
    type: str
    nullable: bool
    level: SensitivityLevel
    category: str
    matched_by: str
    samples: list[str]  # 已脱敏


@dataclass(frozen=True)
class TableProfile:
    name: str
    max_level: SensitivityLevel
    columns: list[ColumnProfile]


@dataclass(frozen=True)
class ProfileResult:
    tables: list[TableProfile]
    max_level: SensitivityLevel

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def column_count(self) -> int:
        return sum(len(table.columns) for table in self.tables)

    def to_schema_json(self) -> dict[str, Any]:
        """落库用的 Schema 快照 —— 样本值此时已脱敏。"""
        return {
            "tables": [
                {
                    "name": table.name,
                    "max_level": table.max_level.value,
                    "columns": [
                        {
                            "name": column.name,
                            "type": column.type,
                            "nullable": column.nullable,
                            "level": column.level.value,
                            "category": column.category,
                            "matched_by": column.matched_by,
                            "samples": column.samples,
                        }
                        for column in table.columns
                    ],
                }
                for table in self.tables
            ],
            "table_count": self.table_count,
            "column_count": self.column_count,
        }

    def to_sensitivity_tags(self) -> dict[str, Any]:
        """按表聚合的分级标签，供安全审批人快速判断影响面。"""
        tags: dict[str, Any] = {}
        for table in self.tables:
            by_level: dict[str, list[str]] = {}
            for column in table.columns:
                if column.level in (SensitivityLevel.L3, SensitivityLevel.L4):
                    by_level.setdefault(column.level.value, []).append(column.name)
            tags[table.name] = {
                "max_level": table.max_level.value,
                "max_level_label": table.max_level.label,
                "by_level": by_level,
            }
        return tags


def _profile_column(
    source: MetadataSource, table: str, column: dict[str, Any], sample_size: int
) -> ColumnProfile:
    name = str(column.get("name", ""))
    raw_samples = source.sample_values(table, name, sample_size)
    assessment = assess_column(name, raw_samples)
    return ColumnProfile(
        name=name,
        type=str(column.get("type", "")),
        nullable=bool(column.get("nullable", True)),
        level=assessment.level,
        category=assessment.category,
        matched_by=assessment.matched_by,
        # 脱敏在这里发生 —— 原始值不离开本函数
        samples=[mask_value(value, assessment.level) for value in raw_samples],
    )


def profile_datasource(
    source: MetadataSource,
    *,
    tables_limit: int = 20,
    sample_size: int = 5,
) -> ProfileResult:
    """执行只读元数据探查并自动分级（FR-05 / FR-06）。

    :param tables_limit: 最多扫描的表数（预算约束，防止拖垮生产库）
    :param sample_size: 每字段采样的行数上限
    """
    tables = source.list_tables()[:tables_limit]

    profiles: list[TableProfile] = []
    for table_name in tables:
        # 原始样本只在 _profile_column 内短暂存在；返回到这里时已经完成分级和脱敏。
        columns = [
            _profile_column(source, table_name, column, sample_size)
            for column in source.list_columns(table_name)
        ]
        profiles.append(
            TableProfile(
                name=table_name,
                max_level=aggregate_level(column.level for column in columns),
                columns=columns,
            )
        )

    return ProfileResult(
        tables=profiles,
        max_level=aggregate_level(table.max_level for table in profiles),
    )
