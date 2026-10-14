"""确定性静态门禁测试（FR-18）。

门禁必须是确定性的：同样的输入永远得到同样的结论，不调模型、不做模糊匹配。
"""

from app.services.endpoint_context import EndpointContext
from app.services.gate import (
    CheckStatus,
    check_config_syntax,
    check_operators,
    check_schema_alignment,
    run_gate,
)

VALID_CONFIG = """
env {
  parallelism = 2
  job.mode = "BATCH"
}
source {
  Jdbc {
    url = "${SOURCE_URL}"
    driver = "com.mysql.cj.jdbc.Driver"
    query = "SELECT order_id FROM orders"
  }
}
sink {
  Doris {
    fenodes = "${TARGET_FENODES}"
    database = "ods"
    table = "orders_wide"
    sink.label-prefix = "etl-run-1"
  }
}
"""

BROKEN_CONFIG = """
source {
  Jdbc {
    url = "${SOURCE_URL}"
"""

SOURCE = EndpointContext(
    datasource_id="11111111-1111-1111-1111-111111111111",
    name="订单源库",
    type="mysql",
    config={"host": "127.0.0.1", "database": "demo"},
    tables=[
        {
            "name": "orders",
            "max_level": "L3",
            "columns": [
                {"name": "order_id", "type": "BIGINT", "level": "L2", "category": "业务数据"},
                {"name": "user_phone", "type": "VARCHAR(20)", "level": "L3", "category": "手机号"},
            ],
        }
    ],
)

TARGET = EndpointContext(
    datasource_id="22222222-2222-2222-2222-222222222222",
    name="数仓",
    type="doris",
    config={"host": "127.0.0.1", "database": "ods"},
    tables=[
        {
            "name": "orders_wide",
            "max_level": "L3",
            "columns": [
                {"name": "order_id", "type": "BIGINT", "level": "L2", "category": "业务数据"},
                {"name": "user_phone", "type": "VARCHAR(20)", "level": "L3", "category": "手机号"},
            ],
        }
    ],
)

NO_PROFILE = EndpointContext(
    datasource_id="33333333-3333-3333-3333-333333333333",
    name="未探查",
    type="mysql",
    config={},
    tables=[],
)


def _plan(**overrides) -> dict:
    plan = {
        "name": "订单同步",
        "description": "",
        "source_datasource_id": SOURCE.datasource_id,
        "target_datasource_id": TARGET.datasource_id,
        "source_table": "orders",
        "target_table": "orders_wide",
        "steps": [
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.Jdbc",
                "role": "source",
                "attributes": {
                    "url": "jdbc:mysql://x/demo",
                    "driver": "com.mysql.cj.jdbc.Driver",
                    "query": "SELECT order_id, user_phone FROM orders",
                },
            },
            {
                "step_id": "snk",
                "operator_id": "seatunnel.sink.Doris",
                "role": "sink",
                "attributes": {
                    "fenodes": "127.0.0.1:8030",
                    "database": "ods",
                    "table": "orders_wide",
                },
            },
        ],
        "field_mappings": [
            {"source_field": "order_id", "target_field": "order_id"},
            {"source_field": "user_phone", "target_field": "user_phone"},
        ],
        "quality_rules": [],
    }
    plan.update(overrides)
    return plan


# --------------------------------------------------------------------------
# 配置语法
# --------------------------------------------------------------------------


def test_valid_config_passes():
    assert check_config_syntax(VALID_CONFIG).status is CheckStatus.PASSED


def test_broken_config_fails():
    result = check_config_syntax(BROKEN_CONFIG)
    assert result.status is CheckStatus.FAILED
    assert "解析失败" in result.message


def test_empty_config_fails():
    assert check_config_syntax("").status is CheckStatus.FAILED
    assert check_config_syntax("   ").status is CheckStatus.FAILED


# --------------------------------------------------------------------------
# 算子在册与属性齐备
# --------------------------------------------------------------------------


def test_all_operators_registered_passes():
    result = check_operators(_plan())
    assert result.status is CheckStatus.PASSED


def test_unregistered_operator_fails():
    plan = _plan(
        steps=[
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.Kafka",
                "role": "source",
                "attributes": {},
            }
        ]
    )
    result = check_operators(plan)
    assert result.status is CheckStatus.FAILED
    assert any("不在受管算子注册表" in detail for detail in result.details)


def test_missing_required_attribute_fails():
    plan = _plan(
        steps=[
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.Jdbc",
                "role": "source",
                # driver 与 query 是必填，且都不是注入类
                "attributes": {},
            }
        ]
    )
    result = check_operators(plan)
    assert result.status is CheckStatus.FAILED
    assert any("driver" in detail and "query" in detail for detail in result.details)


def test_injected_attributes_are_not_required_from_plan():
    """凭据、连接端点、label-prefix 由控制面注入，缺了不算门禁失败。"""
    result = check_operators(_plan())
    assert result.status is CheckStatus.PASSED


def test_empty_steps_fails():
    assert check_operators(_plan(steps=[])).status is CheckStatus.FAILED


# --------------------------------------------------------------------------
# Schema 对齐
# --------------------------------------------------------------------------


def test_aligned_schema_passes():
    result = check_schema_alignment(_plan(), SOURCE, TARGET)
    assert result.status is CheckStatus.PASSED


def test_unknown_source_field_fails():
    plan = _plan(
        field_mappings=[{"source_field": "made_up_field", "target_field": "order_id"}]
    )
    result = check_schema_alignment(plan, SOURCE, TARGET)
    assert result.status is CheckStatus.FAILED
    assert any("made_up_field" in detail for detail in result.details)


def test_unknown_target_field_fails():
    plan = _plan(
        field_mappings=[{"source_field": "order_id", "target_field": "ghost_column"}]
    )
    result = check_schema_alignment(plan, SOURCE, TARGET)
    assert result.status is CheckStatus.FAILED


def test_unknown_table_fails():
    result = check_schema_alignment(_plan(source_table="nope"), SOURCE, TARGET)
    assert result.status is CheckStatus.FAILED
    assert any("不在探查结果中" in detail for detail in result.details)


def test_missing_profile_is_skipped_not_failed():
    """「没检查」与「检查不通过」必须区分开。"""
    result = check_schema_alignment(_plan(), NO_PROFILE, TARGET)
    assert result.status is CheckStatus.SKIPPED


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------


def test_gate_passes_when_all_checks_pass():
    report = run_gate(plan=_plan(), config=VALID_CONFIG, source=SOURCE, target=TARGET)
    assert report.passed is True
    assert report.to_dict()["summary"] == {"passed": 3, "failed": 0, "skipped": 0}


def test_gate_fails_on_broken_config():
    report = run_gate(plan=_plan(), config=BROKEN_CONFIG, source=SOURCE, target=TARGET)
    assert report.passed is False
    assert [check.name for check in report.failed_checks] == ["配置语法"]


def test_gate_passes_with_skipped_checks():
    """skipped 不阻塞通过，但必须如实出现在报告里。"""
    report = run_gate(plan=_plan(), config=VALID_CONFIG, source=NO_PROFILE, target=TARGET)
    assert report.passed is True
    assert report.to_dict()["summary"]["skipped"] == 1


def test_gate_is_deterministic():
    """同样输入必须得到同样结论。"""
    first = run_gate(plan=_plan(), config=VALID_CONFIG, source=SOURCE, target=TARGET)
    second = run_gate(plan=_plan(), config=VALID_CONFIG, source=SOURCE, target=TARGET)
    assert first.to_dict() == second.to_dict()
