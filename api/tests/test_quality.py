"""T-16 质量分流配置必须由冻结规则确定，未知规则一律拒绝。"""

import re
import uuid
from types import SimpleNamespace

import pytest

from app.core.errors import DataplaneJobFailed, DataplaneProtocolError
from app.models.datasource import Datasource, DatasourceType
from app.models.version import PipelineVersion
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
