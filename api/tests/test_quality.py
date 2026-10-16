"""T-16 质量分流配置必须由冻结规则确定，未知规则一律拒绝。"""

import re
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.errors import DataplaneJobFailed, DataplaneProtocolError
from app.models.datasource import Datasource, DatasourceType
from app.models.version import PipelineVersion
from app.schemas.pipeline import QualityRule
from app.services.quality import DorisPublisher, compile_quality_config, error_table_name

BASE_CONFIG = '''
env { job.mode = "BATCH" }
source {
  Jdbc {
    url = "jdbc:mysql://mysql:3306/demo"
    query = "SELECT * FROM orders"
  }
}
sink {
  Doris {
    database = "ods"
    table = "orders__shadow_v2"
  }
}
'''


def test_regex_rule_generates_multitable_validator_config():
    result = compile_quality_config(
        BASE_CONFIG,
        source_table="orders",
        shadow_table="orders__shadow_v2",
        rules=[{
            "field": "user_name",
            "rule": "regex",
            "params": {"pattern": "^[^吴].*$"},
            "error_code": "USER_NAME_REGEX",
            "action": "reject",
        }],
    )
    # pyhocon 无法解析 SeaTunnel 支持的“标量键 + 同前缀 dotted 键”写法；
    # 运行时由已验证的 Zeta 2.3.13 解析，静态门禁仍校验原始候选配置。
    assert 'row_error_handle_way = "ROUTE_TO_TABLE"' in result
    assert 'row_error_handle_way.error_table = "orders__shadow_v2__errors"' in result
    assert 'custom_message = "USER_NAME_REGEX"' in result
    assert 'table = "${table_name}"' in result


def test_empty_contract_keeps_frozen_candidate_unchanged():
    assert compile_quality_config(
        BASE_CONFIG, source_table="orders", shadow_table="orders__shadow_v2", rules=[]
    ) == BASE_CONFIG


@pytest.mark.parametrize("kind", ["range", "enum"])
def test_unimplemented_rule_is_blocked_before_submission(kind):
    with pytest.raises(DataplaneProtocolError, match="暂无法安全执行"):
        compile_quality_config(
            BASE_CONFIG,
            source_table="orders",
            shadow_table="orders__shadow_v2",
            rules=[{"field": "order_amount", "rule": kind, "params": {},
                    "error_code": "NOT_EXECUTED", "action": "reject"}],
        )


def test_warn_rule_is_not_silently_treated_as_reject():
    with pytest.raises(DataplaneProtocolError, match="warn"):
        compile_quality_config(
            BASE_CONFIG,
            source_table="orders",
            shadow_table="orders__shadow_v2",
            rules=[{"field": "user_name", "rule": "not_null", "params": {},
                    "error_code": "WARN_ONLY", "action": "warn"}],
        )


def test_error_table_name_is_derived_and_bounded():
    assert error_table_name("orders__shadow_v2") == "orders__shadow_v2__errors"
    with pytest.raises(DataplaneProtocolError):
        error_table_name("x" * 60)


class _DorisConnection:
    def __init__(self):
        self.rows = {"orders_wide": 8}
        self.columns = {"orders_wide": [("order_id", "bigint", "NO")]}
        # 错误表的 validation_errors 原文（T-16 报告用）：表名 -> [(原始值, 行数)]。
        # **不是** {错误码: 行数}——真实引擎在这一列写下的是 JSON 数组，
        # 报告要从里面把 error_message 解析出来（见 quality._error_code）。
        self.error_dist: dict[str, list[tuple[str, int]]] = {}
        self.statements: list[str] = []

    def cursor(self):
        return _DorisCursor(self)

    def close(self):
        pass


class _DorisCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        pass

    def execute(self, sql, params=()):
        self.connection.statements.append(sql)
        tables = re.findall(r"`ods`\.`([^`]+)`", sql)
        if sql.startswith("ALTER TABLE"):
            tables.append(re.search(r"REPLACE WITH TABLE `([^`]+)`", sql).group(1))
        if "information_schema.tables" in sql:
            self.result = [(int(params[1] in self.connection.rows),)]
        elif sql.startswith("SELECT COUNT(*) FROM"):
            self.result = [(self.connection.rows[tables[0]],)]
        elif sql.startswith("SELECT validation_errors"):
            self.result = list(self.connection.error_dist.get(tables[0], []))
        elif sql.startswith("SHOW COLUMNS FROM"):
            self.result = self.connection.columns[tables[0]]
        elif sql.startswith("CREATE TABLE") and " LIKE " in sql:
            self.connection.rows[tables[0]] = 0
            self.connection.columns[tables[0]] = list(self.connection.columns[tables[1]])
            self.result = []
        elif sql.startswith("CREATE TABLE"):
            self.connection.rows[tables[0]] = 0
            self.connection.columns[tables[0]] = []
            self.result = []
        elif sql.startswith("ALTER TABLE"):
            first, second = tables
            self.connection.rows[first], self.connection.rows[second] = (
                self.connection.rows[second], self.connection.rows[first]
            )
            self.result = []
        else:
            raise AssertionError(f"未预期 SQL: {sql}")

    def fetchall(self):
        return self.result


def _publisher(monkeypatch):
    connection = _DorisConnection()
    monkeypatch.setattr("app.services.quality.pymysql.connect", lambda **_kw: connection)
    project_id, target_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    version = SimpleNamespace(
        project_id=project_id,
        etl_plan={"target_datasource_id": str(target_id), "target_table": "orders_wide"},
    )
    target = SimpleNamespace(
        project_id=project_id,
        type=DatasourceType.DORIS,
        config={"host": "127.0.0.1", "port": 8050, "database": "ods"},
        credential_ref="target",
    )

    class _Db:
        def get(self, model, key):
            return version if model is PipelineVersion else target if model is Datasource else None

    class _Credentials:
        def resolve(self, ref):
            return {"username": "root", "password": ""}

    command = SimpleNamespace(
        version_id=version_id,
        target_table="orders_wide",
        shadow_table="orders_wide__shadow_v2",
    )
    return DorisPublisher(_Db(), command, _Credentials()), connection


def test_doris_publisher_checks_split_then_swaps_without_dropping_old_table(monkeypatch):
    publisher, connection = _publisher(monkeypatch)
    publisher.prepare(with_quality_rules=True)
    connection.rows[publisher.shadow] = 7
    connection.rows[publisher.errors] = 1
    report = publisher.report(
        source_rows=8, budget={"max_reject_ratio": 0.2}, with_quality_rules=True
    )
    assert report.accepted_rows == 7 and report.rejected_rows == 1
    publisher.publish()
    assert connection.rows[publisher.formal] == 7
    assert connection.rows[publisher.shadow] == 8
    assert any("PROPERTIES('swap'='true')" in sql for sql in connection.statements)
    assert not any("DROP TABLE" in sql for sql in connection.statements)


def test_doris_publisher_blocks_over_budget_before_swap(monkeypatch):
    publisher, connection = _publisher(monkeypatch)
    publisher.prepare(with_quality_rules=True)
    connection.rows[publisher.shadow] = 7
    connection.rows[publisher.errors] = 1
    with pytest.raises(DataplaneJobFailed, match="超过批准预算"):
        publisher.report(
            source_rows=8, budget={"max_reject_ratio": 0.1}, with_quality_rules=True
        )
    assert not any(sql.startswith("ALTER TABLE") for sql in connection.statements)


def test_quality_rule_rejects_warn_at_declaration_time():
    """``warn`` 在**声明那一刻**就被拒，而不是等运行时才发现它执行不了。

    「告警但放行」与 FR-33 的分流契约本身冲突：违规行会留在结果里被发布出去，
    错误表失去意义。审批人签下的契约必须是能被执行的那一份——版本一旦冻结就
    改不动了，那时候才发现规则跑不了，代价是一次白跑的审批。
    """
    rule = QualityRule(field="user_name", rule="regex", error_code="E001")
    assert rule.action == "reject"

    with pytest.raises(ValidationError):
        QualityRule(field="user_name", rule="regex", error_code="E001", action="warn")


def test_error_distribution_reads_the_code_out_of_the_engine_payload(monkeypatch):
    """错误表里存的是引擎写的 JSON 数组，不是错误码本身。

    2026-10-16 在真实 Doris 上实测到的形态是
    ``[{"error_message":"USER_NAME_REGEX","field_name":"user_name"}]``。
    把整串 JSON 当作键，报告里的分布就成了一句没人念得出来的话——
    而「该去改哪条规则」是这个报告里唯一有行动指向的信息。
    """
    publisher, connection = _publisher(monkeypatch)
    publisher.prepare(with_quality_rules=True)
    connection.rows[publisher.shadow] = 7
    connection.rows[publisher.errors] = 1
    connection.error_dist[publisher.errors] = [
        ('[{"error_message":"USER_NAME_REGEX","field_name":"user_name"}]', 1)
    ]

    report = publisher.report(
        source_rows=8, budget={"max_reject_ratio": 0.2}, with_quality_rules=True
    )

    assert report.error_dist == {"USER_NAME_REGEX": 1}


def test_error_distribution_keeps_its_sum_equal_to_rejected_rows(monkeypatch):
    """分布的各项之和必须等于拒绝行数。

    同一错误码出现在不同字段上要合并；解析不出的原文和空值也不能被悄悄丢掉——
    后者恰恰说明有一条拒绝没写错误码，那是需要被看见的异常，不是可以四舍五入的噪声。
    """
    publisher, connection = _publisher(monkeypatch)
    publisher.prepare(with_quality_rules=True)
    connection.rows[publisher.shadow] = 7
    connection.rows[publisher.errors] = 5
    connection.error_dist[publisher.errors] = [
        ('[{"error_message":"E_REGEX","field_name":"a"}]', 2),
        ('[{"error_message":"E_REGEX","field_name":"b"}]', 1),
        ("这不是 JSON", 1),
        ("", 1),
    ]

    report = publisher.report(
        source_rows=12, budget={"max_reject_ratio": 0.9}, with_quality_rules=True
    )

    assert report.error_dist == {"E_REGEX": 3, "这不是 JSON": 1, "(未标注)": 1}
    assert sum(report.error_dist.values()) == report.rejected_rows
