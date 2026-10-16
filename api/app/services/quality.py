"""T-16：Doris 影子表、质量分流与原子发布的确定性实现。"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

import pymysql
from sqlalchemy.orm import Session

from app.core.errors import DataplaneJobFailed, DataplaneProtocolError, DataplaneUnavailable
from app.models.datasource import Datasource, DatasourceType
from app.models.version import PipelineVersion
from app.schemas.execution import ExecutionCommand
from app.services.credentials import CredentialService

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
logger = logging.getLogger(__name__)


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise DataplaneProtocolError("Doris 表名或库名不符合受管标识符规则")
    return f"`{value}`"


def error_table_name(shadow_table: str) -> str:
    """错误表名由已批准的影子表名确定，避免执行期再接受任意目标。"""
    _identifier(shadow_table)
    suffix = "__errors"
    if len(shadow_table) + len(suffix) > 64:
        raise DataplaneProtocolError("影子表名过长，无法派生错误表名")
    return shadow_table + suffix


def _hocon(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def compile_quality_config(
    config: str,
    *,
    source_table: str,
    shadow_table: str,
    rules: list[dict[str, Any]],
) -> str:
    """把已冻结的质量规则编译成 SeaTunnel 的多表分流配置。"""
    if not rules:
        return config
    _identifier(source_table)
    _identifier(shadow_table)
    error_table = error_table_name(shadow_table)
    if (
        config.count("  Jdbc {") != 1
        or config.count("  Doris {") != 1
        or config.count("\nsink {") != 1
        or config.count(f'table = "{shadow_table}"') != 1
        or "\ntransform {" in config
    ):
        raise DataplaneProtocolError("冻结配置不符合单源单目标质量分流结构")

    field_rules: list[str] = []
    for rule in rules:
        if rule.get("action") != "reject":
            raise DataplaneProtocolError("当前数据面不支持 warn 规则，拒绝跳过质量契约")
        field = str(rule.get("field") or "")
        code = str(rule.get("error_code") or "")
        _identifier(field)
        if not code:
            raise DataplaneProtocolError("质量规则缺少错误码")
        kind = str(rule.get("rule") or "")
        params = rule.get("params") or {}
        options = [f"field_name = {_hocon(field)}", f"custom_message = {_hocon(code)}"]
        if kind == "not_null":
            options.append('rule_type = "NOT_NULL"')
        elif kind == "regex" and isinstance(params.get("pattern"), str) and params["pattern"]:
            options.extend(['rule_type = "REGEX"', f'pattern = {_hocon(params["pattern"])}'])
        else:
            # 2.3.13 的 RANGE 对 DECIMAL/BIGINT 存在上游 ClassCastException；
            # ENUM 没有原生规则。不能让未执行的规则被当成已验证并发布。
            raise DataplaneProtocolError(f"SeaTunnel 2.3.13 暂无法安全执行 {kind} 规则")
        field_rules.append("    { " + ", ".join(options) + " }")

    transform = "\n".join(
        [
            "transform {",
            "  TableRename {",
            '    plugin_input = "source_orders"',
            '    plugin_output = "renamed_orders"',
            "    replacements_with_regex = [",
            "      { replace_from = " + _hocon(f"^{re.escape(source_table)}$")
            + ", replace_to = " + _hocon(shadow_table) + " }",
            "    ]",
            "  }",
            "  DataValidator {",
            '    plugin_input = "renamed_orders"',
            '    plugin_output = "validated_orders"',
            '    row_error_handle_way = "ROUTE_TO_TABLE"',
            "    row_error_handle_way.error_table = " + _hocon(error_table),
            "    field_rules = [",
            ",\n".join(field_rules),
            "    ]",
            "  }",
            "}",
        ]
    )
    config = config.replace('  Jdbc {', '  Jdbc {\n    plugin_output = "source_orders"', 1)
    config = config.replace("\nsink {", "\n" + transform + "\nsink {", 1)
    config = config.replace('  Doris {', '  Doris {\n    plugin_input = "validated_orders"', 1)
    return config.replace(f'table = "{shadow_table}"', 'table = "${table_name}"', 1)


@dataclass(frozen=True)
class QualityReport:
    source_rows: int
    accepted_rows: int
    rejected_rows: int
    rejected_ratio: float
    error_table: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_rows": self.source_rows,
            "accepted_rows": self.accepted_rows,
            "rejected_rows": self.rejected_rows,
            "rejected_ratio": self.rejected_ratio,
            "error_table": self.error_table,
        }


class DorisPublisher:
    """仅操作准备单绑定的目标库、正式表、影子表和派生错误表。"""

    def __init__(
        self,
        db: Session,
        command: ExecutionCommand,
        credentials: CredentialService,
    ) -> None:
        version = db.get(PipelineVersion, command.version_id)
        if version is None:
            raise DataplaneProtocolError("冻结版本不存在")
        plan = version.etl_plan or {}
        try:
            target_id = uuid.UUID(str(plan["target_datasource_id"]))
        except (KeyError, ValueError):
            raise DataplaneProtocolError("目标数据源标识无效") from None
        target = db.get(Datasource, target_id)
        if (
            target is None
            or target.project_id != version.project_id
            or target.type is not DatasourceType.DORIS
        ):
            raise DataplaneProtocolError("受管目标不是同项目 Doris 数据源")
        if str(plan.get("target_table")) != command.target_table:
            raise DataplaneProtocolError("执行命令的正式表与冻结方案不一致")

        config = target.config or {}
        secret = credentials.resolve(target.credential_ref) if target.credential_ref else {}
        self.database = str(config.get("database") or "")
        self.formal = command.target_table
        self.shadow = command.shadow_table
        self.errors = error_table_name(self.shadow)
        # 由 prepare 置位：没有质量规则就没有错误表，运行期探测也无从谈起
        self._with_quality_rules = False
        for name in (self.database, self.formal, self.shadow):
            _identifier(name)
        if self.formal == self.shadow:
            raise DataplaneProtocolError("影子表不得与正式表同名")
        host = str(config.get("sql_host") or config.get("host") or "")
        port = int(config.get("sql_port") or config.get("query_port") or config.get("port") or 9030)
        if not host or not 0 < port < 65536:
            raise DataplaneProtocolError("Doris SQL 连接地址无效")
        try:
            self._connection = pymysql.connect(
                host=host,
                port=port,
                user=str(secret.get("username") or secret.get("user") or ""),
                password=str(secret.get("password") or ""),
                database=self.database,
                autocommit=True,
                connect_timeout=5,
                read_timeout=20,
                write_timeout=20,
            )
        except pymysql.MySQLError as exc:
            logger.warning("Doris SQL 连接失败 kind=%s", type(exc).__name__)
            raise DataplaneUnavailable("Doris SQL 连接不可用") from None

    def close(self) -> None:
        self._connection.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        except pymysql.MySQLError as exc:
            # 驱动异常可能含连接参数或表数据，不写进运行记录。
            logger.warning("Doris SQL 调用失败 kind=%s", type(exc).__name__)
            raise DataplaneJobFailed("Doris SQL 操作失败，详见服务端日志") from None

    def _table(self, name: str) -> str:
        return f"{_identifier(self.database)}.{_identifier(name)}"

    def _exists(self, name: str) -> bool:
        rows = self._execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (self.database, name),
        )
        return bool(rows[0][0])

    def _count(self, name: str) -> int:
        return int(self._execute(f"SELECT COUNT(*) FROM {self._table(name)}")[0][0])

    def _columns(self, name: str) -> list[tuple[Any, ...]]:
        return [tuple(row[:3]) for row in self._execute(f"SHOW COLUMNS FROM {self._table(name)}")]

    def prepare(self, *, with_quality_rules: bool) -> None:
        self._with_quality_rules = with_quality_rules
        if not self._exists(self.formal):
            raise DataplaneProtocolError("Doris 正式表不存在，无法创建可发布影子表")
        if not self._exists(self.shadow):
            self._execute(
                f"CREATE TABLE {self._table(self.shadow)} LIKE {self._table(self.formal)}"
            )
        if self._count(self.shadow) != 0 or self._columns(self.shadow) != self._columns(
            self.formal
        ):
            raise DataplaneProtocolError("影子表非空或 Schema 与正式表不一致，拒绝追加写入")
        if with_quality_rules:
            if not self._exists(self.errors):
                self._execute(
                    f"CREATE TABLE {self._table(self.errors)} ("
                    "source_table_id VARCHAR(128) NOT NULL, "
                    "source_table_path VARCHAR(512) NULL, "
                    "original_data STRING NULL, validation_errors STRING NULL, "
                    "create_time DATETIME NULL) ENGINE=OLAP "
                    "DUPLICATE KEY(source_table_id) "
                    "DISTRIBUTED BY HASH(source_table_id) BUCKETS 1 "
                    'PROPERTIES ("replication_num"="1")'
                )
            if self._count(self.errors) != 0:
                raise DataplaneProtocolError("错误表非空，拒绝复用历史错误数据")

    def rejected_rows(self) -> int | None:
        """运行中拒绝行数的尽力探测（FR-35 拒绝率维度）。

        **失败即返回 ``None``，绝不抛异常**：这是早期预警信号，不是判据。
        让一次瞬时的元数据查询抖动去终止一个健康的作业，比晚几秒发现拒绝率
        上升要糟糕得多。权威判定仍在 :meth:`report`——那时错误表已经写完，
        计数是完整的，而不是「截至此刻」。
        """
        if not self._with_quality_rules:
            return None
        try:
            return self._count(self.errors)
        except (DataplaneJobFailed, DataplaneUnavailable):
            logger.warning("运行中拒绝行数探测失败，本轮跳过拒绝率维度")
            return None

    def report(
        self, *, source_rows: int, budget: dict[str, Any], with_quality_rules: bool
    ) -> QualityReport:
        accepted = self._count(self.shadow)
        rejected = self._count(self.errors) if with_quality_rules else 0
        if source_rows != accepted + rejected:
            raise DataplaneJobFailed("源端行数与有效/错误分流行数不一致，拒绝发布")
        ratio = rejected / source_rows if source_rows else 0.0
        limit = float(budget.get("max_reject_ratio", 0.0))
        if ratio > limit:
            raise DataplaneJobFailed("错误行比例超过批准预算，拒绝发布")
        if self._columns(self.shadow) != self._columns(self.formal):
            raise DataplaneJobFailed("发布前影子表 Schema 与正式表不一致")
        return QualityReport(
            source_rows,
            accepted,
            rejected,
            ratio,
            self.errors if with_quality_rules else None,
        )

    def publish(self) -> None:
        # swap=true 会把旧正式表保留在影子表名下，供 T-18 受管回滚使用。
        self._execute(
            f"ALTER TABLE {self._table(self.formal)} REPLACE WITH TABLE "
            f"{_identifier(self.shadow)} PROPERTIES('swap'='true')"
        )
