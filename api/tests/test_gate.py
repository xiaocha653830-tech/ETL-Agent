"""确定性静态门禁测试（FR-18）。

门禁必须是确定性的：同样的输入永远得到同样的结论，不调模型、不做模糊匹配。
"""

from app.services.endpoint_context import EndpointContext
from app.services.gate import (
    CheckStatus,
    check_config_syntax,
    check_operators,
    check_plan_config_consistency,
    check_plan_structure,
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


def test_undeclared_attribute_fails():
    """注册表是权威清单，模型自造的属性名不得进入配置。"""
    plan = _plan(
        steps=[
            {
                "step_id": "snk",
                "operator_id": "seatunnel.sink.Doris",
                "role": "sink",
                "attributes": {
                    "database": "ods",
                    "table": "orders_wide",
                    "write_mode": "append",
                },
            }
        ]
    )
    result = check_operators(plan)
    assert result.status is CheckStatus.FAILED
    assert any("write_mode" in detail for detail in result.details)


def test_deprecated_attribute_gets_specific_hint():
    """已废弃写法要给出可照做的修复指引，而不是一句「不在定义中」。"""
    plan = _plan(
        steps=[
            {
                "step_id": "snk",
                "operator_id": "seatunnel.sink.Doris",
                "role": "sink",
                "attributes": {
                    "database": "ods",
                    "table": "orders_wide",
                    "table.identifier": "ods.orders_wide",
                },
            }
        ]
    )
    result = check_operators(plan)
    assert result.status is CheckStatus.FAILED
    assert any(
        "table.identifier" in detail and "已废弃" in detail for detail in result.details
    )


def test_wrong_value_type_fails():
    plan = _plan(
        steps=[
            {
                "step_id": "snk",
                "operator_id": "seatunnel.sink.Doris",
                "role": "sink",
                "attributes": {"database": 123, "table": "orders_wide"},
            }
        ]
    )
    result = check_operators(plan)
    assert result.status is CheckStatus.FAILED
    assert any("database" in detail and "字符串" in detail for detail in result.details)


def test_boolean_attribute_rejects_integer():
    """bool 是 int 的子类，门禁必须把它区分开。"""
    plan = _plan(
        steps=[
            {
                "step_id": "snk",
                "operator_id": "seatunnel.sink.Doris",
                "role": "sink",
                "attributes": {
                    "database": "ods",
                    "table": "orders_wide",
                    "sink.enable-2pc": 1,
                },
            }
        ]
    )
    assert check_operators(plan).status is CheckStatus.FAILED


def test_enum_value_out_of_range_fails():
    plan = _plan(
        steps=[
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.MySQL-CDC",
                "role": "source",
                "attributes": {
                    "base-url": "jdbc:mysql://x:3306",
                    "table-names": ["demo.orders"],
                    "startup.mode": "whenever",
                },
            }
        ]
    )
    result = check_operators(plan)
    assert result.status is CheckStatus.FAILED
    assert any("startup.mode" in detail for detail in result.details)


def test_valid_enum_value_passes():
    plan = _plan(
        steps=[
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.MySQL-CDC",
                "role": "source",
                "attributes": {
                    "base-url": "jdbc:mysql://x:3306",
                    "table-names": ["demo.orders"],
                    "startup.mode": "latest",
                },
            }
        ]
    )
    assert check_operators(plan).status is CheckStatus.PASSED


def test_injected_attribute_skips_type_check():
    """注入类属性的值整体被占位符替换，形态无所谓——不该因此拦下方案。"""
    plan = _plan(
        steps=[
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.Jdbc",
                "role": "source",
                # url 是注入类，故意给个非字符串值
                "attributes": {
                    "url": 12345,
                    "driver": "com.mysql.cj.jdbc.Driver",
                    "query": "SELECT 1",
                },
            }
        ]
    )
    assert check_operators(plan).status is CheckStatus.PASSED


# --------------------------------------------------------------------------
# 方案结构
# --------------------------------------------------------------------------


def test_structure_passes_for_wellformed_plan():
    result = check_plan_structure(_plan())
    assert result.status is CheckStatus.PASSED


def test_empty_steps_fails_structure():
    assert check_plan_structure(_plan(steps=[])).status is CheckStatus.FAILED


def test_duplicate_step_id_fails():
    plan = _plan()
    plan["steps"][1]["step_id"] = "src"
    result = check_plan_structure(plan)
    assert result.status is CheckStatus.FAILED
    assert any("step_id 重复" in detail for detail in result.details)


def test_role_mismatch_against_registry_fails():
    """方案把 sink 算子标成 source —— DAG 与配置会指向完全不同的东西。"""
    plan = _plan()
    plan["steps"][1]["role"] = "source"
    result = check_plan_structure(plan)
    assert result.status is CheckStatus.FAILED
    assert any("sink" in detail for detail in result.details)


def test_missing_sink_fails_structure():
    plan = _plan()
    plan["steps"] = [plan["steps"][0]]
    result = check_plan_structure(plan)
    assert result.status is CheckStatus.FAILED
    assert any("缺少 sink" in detail for detail in result.details)


def test_missing_source_fails_structure():
    plan = _plan()
    plan["steps"] = [plan["steps"][1]]
    result = check_plan_structure(plan)
    assert result.status is CheckStatus.FAILED
    assert any("缺少 source" in detail for detail in result.details)


# --------------------------------------------------------------------------
# 方案与配置一致
# --------------------------------------------------------------------------


def test_matching_config_passes_consistency():
    assert (
        check_plan_config_consistency(_plan(), VALID_CONFIG).status is CheckStatus.PASSED
    )


def test_config_with_different_sink_fails_consistency():
    """审的是 Doris 方案，跑的是 Clickhouse 配置——这正是 FR-19 要挡住的错配。"""
    mismatched = """
env {
  parallelism = 2
}
source {
  Jdbc {
    driver = "x"
    query = "y"
  }
}
sink {
  Clickhouse {
    database = "ods"
  }
}
"""
    result = check_plan_config_consistency(_plan(), mismatched)
    assert result.status is CheckStatus.FAILED
    assert any("Doris" in detail and "未出现" in detail for detail in result.details)
    assert any("Clickhouse" in detail for detail in result.details)


def test_config_missing_source_block_fails_consistency():
    sink_only = """
env {
  parallelism = 2
}
sink {
  Doris {
    database = "ods"
  }
}
"""
    result = check_plan_config_consistency(_plan(), sink_only)
    assert result.status is CheckStatus.FAILED
    assert any("Jdbc" in detail for detail in result.details)


def test_unparseable_config_is_skipped_in_consistency():
    """解析不了就无从比对——语法检查已经会把它判失败，这里不重复计一次。"""
    assert (
        check_plan_config_consistency(_plan(), BROKEN_CONFIG).status
        is CheckStatus.SKIPPED
    )


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
    assert report.to_dict()["summary"] == {"passed": 5, "failed": 0, "skipped": 0}


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
