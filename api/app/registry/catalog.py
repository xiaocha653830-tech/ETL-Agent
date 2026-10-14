"""算子能力注册表 —— 静态算子清单数据源（SPEC §3 M4 / PRD 附录 B）。

本模块是注册表的**唯一权威数据源**：受管算子的标识、类型、实现制品、属性清单与
方言约束在此集中定义，供 M3 需求生成时闭合生成空间（ADR-05，FR-11 / FR-14）。

数据来源与忠实性约定：
  - 算子标识（``seatunnel.<type>.<PluginName>``）与实现制品（connector jar 前缀）直接
    取自本地发行包 ``connectors/plugin-mapping.properties``（SeaTunnel 2.3.13）；
  - 属性名取自 SeaTunnel 2.3.13 连接器定义；Doris 相关结论对齐 SPEC §11.1 V2；
  - **不确定的参数一律不收录**（宁缺毋滥），默认值不确定时留空，避免注册表自身成为
    幻觉来源——注册表一旦写错，生成端会跟着错。

当前为 **P0 子集**（约 10~15 个算子）：MySQL 系列、PostgreSQL 系列、Doris、ClickHouse、
文件类（CSV / JSON / Parquet / S3File）、HTTP。P1 连接器按需分批纳入。
"""

from app.registry.schemas import (
    AttributeType,
    OperatorAttribute,
    OperatorDetail,
    OperatorType,
)


def _attr(
    name: str,
    type_: AttributeType,
    required: bool,
    description: str,
    default: str | None = None,
    enum_values: list[str] | None = None,
) -> OperatorAttribute:
    """构造一条属性规格，减少清单书写样板。"""
    return OperatorAttribute(
        name=name,
        type=type_,
        required=required,
        description=description,
        default=default,
        enum_values=enum_values,
    )


def _op(
    operator_id: str,
    type_: OperatorType,
    connector_artifact: str,
    description: str,
    attributes: list[OperatorAttribute],
    dialect_constraints: list[str] | None = None,
) -> OperatorDetail:
    """构造一个算子条目。"""
    return OperatorDetail(
        operator_id=operator_id,
        type=type_,
        connector_artifact=connector_artifact,
        description=description,
        attributes=attributes,
        dialect_constraints=dialect_constraints or [],
    )


_STARTUP_MODES = ["initial", "earliest", "latest", "specific"]
_FILE_FORMATS = ["csv", "json", "parquet", "text", "orc", "excel"]


_OPERATORS: tuple[OperatorDetail, ...] = (
    # ---------- MySQL 系列 ----------
    _op(
        "seatunnel.source.MySQL-CDC",
        OperatorType.SOURCE,
        "connector-cdc-mysql",
        "通过 MySQL binlog CDC 捕获源库变更，支持全量初始化 + 增量同步",
        [
            _attr("base-url", AttributeType.STRING, True, "MySQL JDBC 连接串，如 jdbc:mysql://host:3306"),
            _attr("username", AttributeType.STRING, True, "数据库用户名"),
            _attr(
                "password",
                AttributeType.STRING,
                True,
                "数据库密码；由凭据服务注入，配置文件不落明文",
            ),
            _attr(
                "table-names",
                AttributeType.LIST,
                True,
                "需同步的表列表，元素形如 database.table",
            ),
            _attr(
                "startup.mode",
                AttributeType.ENUM,
                False,
                "首次启动的消费位点",
                default="initial",
                enum_values=_STARTUP_MODES,
            ),
            _attr(
                "server-id",
                AttributeType.STRING,
                False,
                "CDC 客户端 server id，集群内须唯一；留空由引擎随机分配",
            ),
        ],
    ),
    _op(
        "seatunnel.source.Jdbc",
        OperatorType.SOURCE,
        "connector-jdbc",
        "通过 JDBC 批量读取关系库（MySQL / PostgreSQL 等），适合全量拉取与字段映射",
        [
            _attr("url", AttributeType.STRING, True, "JDBC 连接串"),
            _attr("driver", AttributeType.STRING, True, "JDBC 驱动类名"),
            _attr("query", AttributeType.STRING, True, "读取 SQL（SELECT 语句）"),
            _attr("user", AttributeType.STRING, False, "数据库用户名"),
            _attr("password", AttributeType.STRING, False, "数据库密码；由凭据服务注入"),
        ],
    ),
    _op(
        "seatunnel.sink.Jdbc",
        OperatorType.SINK,
        "connector-jdbc",
        "通过 JDBC 写入关系库（MySQL / PostgreSQL 等）",
        [
            _attr("url", AttributeType.STRING, True, "JDBC 连接串"),
            _attr("driver", AttributeType.STRING, True, "JDBC 驱动类名"),
            _attr("user", AttributeType.STRING, False, "数据库用户名"),
            _attr("password", AttributeType.STRING, False, "数据库密码；由凭据服务注入"),
            _attr(
                "database",
                AttributeType.STRING,
                False,
                "目标库名（配合 generate_sink_sql 使用）",
            ),
            _attr("table", AttributeType.STRING, False, "目标表名（配合 generate_sink_sql 使用）"),
            _attr("query", AttributeType.STRING, False, "自定义写入 SQL（不自动生成时使用）"),
            _attr(
                "generate_sink_sql",
                AttributeType.BOOLEAN,
                False,
                "是否依 database + table + schema 自动生成写入 SQL",
                default="false",
            ),
            _attr("primary_keys", AttributeType.LIST, False, "主键字段，用于生成 upsert 语句"),
            _attr(
                "batch_size",
                AttributeType.INT,
                False,
                "批量写入条数",
                default="1000",
            ),
            _attr(
                "is_exactly_once",
                AttributeType.BOOLEAN,
                False,
                "是否开启精确一次写入（需要目标库支持 XA）",
                default="false",
            ),
        ],
    ),
    # ---------- PostgreSQL 系列 ----------
    _op(
        "seatunnel.source.Postgres-CDC",
        OperatorType.SOURCE,
        "connector-cdc-postgres",
        "通过 PostgreSQL 逻辑复制（WAL）捕获源库变更",
        [
            _attr(
                "base-url",
                AttributeType.STRING,
                True,
                "PostgreSQL JDBC 连接串，如 jdbc:postgresql://host:5432/db",
            ),
            _attr("username", AttributeType.STRING, True, "数据库用户名"),
            _attr("password", AttributeType.STRING, True, "数据库密码；由凭据服务注入"),
            _attr("database-names", AttributeType.LIST, True, "需同步的数据库名列表"),
            _attr("table-names", AttributeType.LIST, True, "需同步的表列表，元素形如 schema.table"),
            _attr(
                "startup.mode",
                AttributeType.ENUM,
                False,
                "首次启动的消费位点",
                default="initial",
                enum_values=_STARTUP_MODES,
            ),
        ],
    ),
    # ---------- Doris ----------
    _op(
        "seatunnel.source.Doris",
        OperatorType.SOURCE,
        "connector-doris",
        "从 Apache Doris 读取数据（Doris Source）",
        [
            _attr(
                "fenodes",
                AttributeType.STRING,
                True,
                "Doris FE 地址，格式 host:8030，多 FE 逗号分隔",
            ),
            _attr("username", AttributeType.STRING, True, "Doris 用户名"),
            _attr("password", AttributeType.STRING, True, "Doris 密码；由凭据服务注入"),
            _attr("table_list", AttributeType.LIST, True, "待读取表列表，元素形如 database.table"),
            _attr("doris.read.field", AttributeType.LIST, False, "指定读取的列，逗号分隔"),
            _attr("doris.filter.query", AttributeType.STRING, False, "下推到 Doris 的过滤条件"),
        ],
    ),
    _op(
        "seatunnel.sink.Doris",
        OperatorType.SINK,
        "connector-doris",
        "通过 Stream Load 写入 Apache Doris，支持 2PC 精确一次与整表原子 Swap",
        [
            _attr(
                "fenodes",
                AttributeType.STRING,
                True,
                "Doris FE 地址，格式 host:8030，多 FE 逗号分隔",
            ),
            _attr("username", AttributeType.STRING, True, "Doris 用户名"),
            _attr("password", AttributeType.STRING, True, "Doris 密码；由凭据服务注入"),
            _attr("database", AttributeType.STRING, True, "目标库名"),
            _attr("table", AttributeType.STRING, True, "目标表名"),
            _attr(
                "sink.label-prefix",
                AttributeType.STRING,
                True,
                "Stream Load 标签前缀，必须按作业全局唯一（建议用 run_id）",
            ),
            _attr(
                "sink.enable-2pc",
                AttributeType.BOOLEAN,
                False,
                "是否开启两阶段提交以支持 Exactly-Once",
            ),
            _attr(
                "sink.enable-delete",
                AttributeType.BOOLEAN,
                False,
                "是否启用删除事件同步（CDC 场景）",
            ),
        ],
        dialect_constraints=[
            "sink.label-prefix 必须按作业全局唯一，2PC 重启复用同一前缀会报 Label already exists；"
            "实现时直接以 run_id 作前缀即可满足。",
            "table.identifier 自 SeaTunnel 2.3.5 起已废弃，必须改用 database + table 新写法。",
            "影子表原子 Swap（ALTER TABLE ... REPLACE WITH TABLE ... ('swap'='true')）要求目标为 "
            "Doris >= 2.0 的 OLAP 内部表，且两表 schema 一致性须由本平台自行保证。",
        ],
    ),
    # ---------- ClickHouse ----------
    _op(
        "seatunnel.source.Clickhouse",
        OperatorType.SOURCE,
        "connector-clickhouse",
        "从 ClickHouse 读取数据（ClickHouse Source）",
        [
            _attr("host", AttributeType.STRING, True, "ClickHouse 地址，格式 host:8123"),
            _attr("database", AttributeType.STRING, True, "库名"),
            _attr("table", AttributeType.STRING, True, "表名"),
            _attr("username", AttributeType.STRING, False, "ClickHouse 用户名"),
            _attr("password", AttributeType.STRING, False, "ClickHouse 密码；由凭据服务注入"),
            _attr("sql", AttributeType.STRING, False, "自定义读取 SQL（指定后覆盖 table 读取）"),
        ],
    ),
    _op(
        "seatunnel.sink.Clickhouse",
        OperatorType.SINK,
        "connector-clickhouse",
        "写入 ClickHouse（ClickHouse Sink）",
        [
            _attr("host", AttributeType.STRING, True, "ClickHouse 地址，格式 host:8123"),
            _attr("database", AttributeType.STRING, True, "库名"),
            _attr("table", AttributeType.STRING, True, "表名"),
            _attr("username", AttributeType.STRING, False, "ClickHouse 用户名"),
            _attr("password", AttributeType.STRING, False, "ClickHouse 密码；由凭据服务注入"),
        ],
    ),
    # ---------- 文件类（CSV / JSON / Parquet） ----------
    _op(
        "seatunnel.source.LocalFile",
        OperatorType.SOURCE,
        "connector-file-local",
        "读取本地文件（CSV / JSON / Parquet 等），常用于演示与回归造数",
        [
            _attr("path", AttributeType.STRING, True, "文件或目录路径，支持通配符"),
            _attr(
                "file_format_type",
                AttributeType.ENUM,
                True,
                "文件格式；CSV / JSON / Parquet 均由此参数区分",
                enum_values=_FILE_FORMATS,
            ),
            _attr(
                "schema",
                AttributeType.MAP,
                False,
                "字段结构；Parquet 可从文件头推断，CSV / JSON 通常须显式声明",
            ),
            _attr("delimiter", AttributeType.STRING, False, "CSV 分隔符", default=","),
            _attr(
                "skip_header_row_number",
                AttributeType.INT,
                False,
                "CSV 跳过表头的行数",
                default="0",
            ),
            _attr("encoding", AttributeType.STRING, False, "文本编码", default="UTF-8"),
        ],
    ),
    _op(
        "seatunnel.sink.LocalFile",
        OperatorType.SINK,
        "connector-file-local",
        "写入本地文件（CSV / JSON / Parquet 等）",
        [
            _attr("path", AttributeType.STRING, True, "输出目录路径"),
            _attr(
                "file_format_type",
                AttributeType.ENUM,
                True,
                "文件格式；CSV / JSON / Parquet 均由此参数区分",
                enum_values=_FILE_FORMATS,
            ),
            _attr("delimiter", AttributeType.STRING, False, "CSV 分隔符", default=","),
            _attr(
                "is_enable_transaction",
                AttributeType.BOOLEAN,
                False,
                "是否启用文件级事务，避免产生半成品文件",
            ),
        ],
    ),
    _op(
        "seatunnel.source.S3File",
        OperatorType.SOURCE,
        "connector-file-s3",
        "从 S3 / 兼容对象存储读取文件（CSV / JSON / Parquet 等）",
        [
            _attr("path", AttributeType.STRING, True, "对象路径，支持通配符"),
            _attr("bucket", AttributeType.STRING, True, "S3 桶名"),
            _attr("fs.s3a.access.key", AttributeType.STRING, True, "访问密钥 AK；由凭据服务注入"),
            _attr("fs.s3a.secret.key", AttributeType.STRING, True, "访问密钥 SK；由凭据服务注入"),
            _attr(
                "file_format_type",
                AttributeType.ENUM,
                True,
                "文件格式；CSV / JSON / Parquet 均由此参数区分",
                enum_values=_FILE_FORMATS,
            ),
            _attr("fs.s3a.endpoint", AttributeType.STRING, False, "S3 兼容端点地址"),
            _attr("delimiter", AttributeType.STRING, False, "CSV 分隔符", default=","),
            _attr("schema", AttributeType.MAP, False, "字段结构；CSV / JSON 通常须显式声明"),
        ],
    ),
    _op(
        "seatunnel.sink.S3File",
        OperatorType.SINK,
        "connector-file-s3",
        "写入 S3 / 兼容对象存储",
        [
            _attr("path", AttributeType.STRING, True, "对象输出路径"),
            _attr("bucket", AttributeType.STRING, True, "S3 桶名"),
            _attr("fs.s3a.access.key", AttributeType.STRING, True, "访问密钥 AK；由凭据服务注入"),
            _attr("fs.s3a.secret.key", AttributeType.STRING, True, "访问密钥 SK；由凭据服务注入"),
            _attr(
                "file_format_type",
                AttributeType.ENUM,
                True,
                "文件格式；CSV / JSON / Parquet 均由此参数区分",
                enum_values=_FILE_FORMATS,
            ),
            _attr("fs.s3a.endpoint", AttributeType.STRING, False, "S3 兼容端点地址"),
            _attr("delimiter", AttributeType.STRING, False, "CSV 分隔符", default=","),
        ],
    ),
    # ---------- HTTP ----------
    _op(
        "seatunnel.source.Http",
        OperatorType.SOURCE,
        "connector-http-base",
        "通过 HTTP 拉取接口数据（JSON / 文本），支持轮询",
        [
            _attr("url", AttributeType.STRING, True, "接口地址"),
            _attr(
                "method",
                AttributeType.ENUM,
                False,
                "HTTP 方法",
                default="GET",
                enum_values=["GET", "POST"],
            ),
            _attr(
                "format",
                AttributeType.ENUM,
                False,
                "响应体解析格式",
                default="json",
                enum_values=["json", "text"],
            ),
            _attr("headers", AttributeType.MAP, False, "请求头"),
            _attr("params", AttributeType.MAP, False, "请求参数"),
            _attr(
                "content_field",
                AttributeType.STRING,
                False,
                "从响应 JSON 中提取数据数组的字段名",
            ),
            _attr("schema", AttributeType.MAP, False, "字段结构"),
        ],
    ),
)

#: 受管算子总数（P0 子集）。
OPERATOR_COUNT = len(_OPERATORS)


def list_operators(op_type: OperatorType | None = None) -> list[OperatorDetail]:
    """返回受管算子清单；传入 ``op_type`` 时按类型过滤。"""
    if op_type is None:
        return list(_OPERATORS)
    return [op for op in _OPERATORS if op.type == op_type]


def get_operator(operator_id: str) -> OperatorDetail | None:
    """按算子标识精确查找；未注册返回 ``None``（由调用方决定「不支持」的响应形态）。"""
    for op in _OPERATORS:
        if op.operator_id == operator_id:
            return op
    return None
