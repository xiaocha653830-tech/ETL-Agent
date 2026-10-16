"""需求生成状态机测试（FR-08 ~ FR-14）。

全部使用 ``FakeLLMClient`` + ``InMemorySaver``：不连 PostgreSQL、不调真实模型。
"""

import json

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.services.endpoint_context import EndpointContext
from app.services.llm import FakeLLMClient
from app.services.orchestrator import build_generation_graph, build_initial_state

SOURCE = EndpointContext(
    datasource_id="11111111-1111-1111-1111-111111111111",
    name="订单源库",
    type="mysql",
    config={"host": "127.0.0.1", "port": 3306, "database": "demo"},
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
    name="数仓目标库",
    type="doris",
    config={"host": "127.0.0.1", "port": 8030, "database": "ods"},
    tables=[],
)

PARSE_OK = json.dumps(
    {
        "understood": "把 orders 表同步到 Doris",
        "source_table": "orders",
        "target_table": "orders_wide",
        "fields": ["order_id", "user_phone"],
        "filters": None,
        "schedule": None,
        "need_clarification": [],
    },
    ensure_ascii=False,
)

PARSE_NEEDS_CLARIFY = json.dumps(
    {
        "understood": "把订单数据同步到数仓",
        "source_table": "orders",
        "target_table": None,
        "fields": [],
        "filters": None,
        "schedule": None,
        "need_clarification": [
            {
                "key": "target_table",
                "question": "目标端写入哪张表？",
                "hint": "例如 ods_order_wide",
                "options": [],
            }
        ],
    },
    ensure_ascii=False,
)

MATCH_OK = json.dumps(
    {
        "supported": True,
        "steps": [
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.Jdbc",
                "role": "source",
                "attributes": {
                    "url": "jdbc:mysql://127.0.0.1:3306/demo",
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
    },
    ensure_ascii=False,
)

MATCH_UNSUPPORTED = json.dumps(
    {"supported": False, "reason": "受管清单中没有 Kafka 相关算子，无法实现流式接入"},
    ensure_ascii=False,
)

MATCH_HALLUCINATED = json.dumps(
    {
        "supported": True,
        "steps": [
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.Kafka",
                "role": "source",
                "attributes": {},
            }
        ],
    },
    ensure_ascii=False,
)

PLAN_OK = json.dumps(
    {
        "name": "订单宽表同步",
        "description": "把订单表同步到 Doris 宽表",
        "source_table": "orders",
        "target_table": "orders_wide",
        "field_mappings": [
            {
                "source_field": "order_id",
                "target_field": "order_id",
                "transform": None,
                "sensitivity_level": "L2",
            },
            {
                "source_field": "user_phone",
                "target_field": "user_phone",
                "transform": None,
                "sensitivity_level": "L3",
            },
        ],
        "unmapped_source_fields": [],
        "warnings": [],
    },
    ensure_ascii=False,
)


def _graph(responses: list[str]):
    return build_generation_graph(FakeLLMClient(responses), InMemorySaver())


def _initial(run_id: str = "run-1"):
    return build_initial_state(
        run_id=run_id,
        requirement="把 orders 表的订单数据同步到 Doris",
        source=SOURCE,
        target=TARGET,
    )


def _config(thread: str = "thread-1"):
    return {"configurable": {"thread_id": thread}}


# --------------------------------------------------------------------------
# 正常链路
# --------------------------------------------------------------------------


def test_happy_path_produces_plan_and_config():
    graph = _graph([PARSE_OK, MATCH_OK, PLAN_OK])
    result = graph.invoke(_initial(), _config())

    assert result["supported"] is True
    assert result["plan"]["source_table"] == "orders"
    assert result["plan"]["target_table"] == "orders_wide"

    config_text = result["config"]
    assert "Jdbc {" in config_text
    assert "Doris {" in config_text
    assert 'table = "${TARGET_TABLE}"' in config_text


def test_config_never_contains_credentials():
    """FR-03：配置里绝不能出现连接串、口令等敏感值。"""
    graph = _graph([PARSE_OK, MATCH_OK, PLAN_OK])
    result = graph.invoke(_initial(), _config())

    config_text = result["config"]
    assert "jdbc:mysql://127.0.0.1:3306/demo" not in config_text
    assert "${SOURCE_URL}" in config_text
    assert "${SOURCE_USER}" in config_text
    assert "${SOURCE_PASSWORD}" in config_text
    assert "${TARGET_FENODES}" in config_text
    assert "${TARGET_USERNAME}" in config_text
    assert "${TARGET_PASSWORD}" in config_text
    assert 'doris.config = { format = "json", read_json_by_line = "true" }' in config_text


def test_doris_sink_gets_idempotent_label_prefix():
    """SPEC §11.1 V2：label-prefix 必须按作业唯一，用 run_id 满足 FR-27 幂等。"""
    graph = _graph([PARSE_OK, MATCH_OK, PLAN_OK])
    result = graph.invoke(_initial("run-abc"), _config())

    assert 'sink.label-prefix = "etl-${RUN_LABEL}"' in result["config"]


def test_field_mappings_carry_sensitivity_level():
    graph = _graph([PARSE_OK, MATCH_OK, PLAN_OK])
    result = graph.invoke(_initial(), _config())

    mappings = {item["source_field"]: item for item in result["plan"]["field_mappings"]}
    assert mappings["user_phone"]["sensitivity_level"] == "L3"


def test_quality_rules_are_not_generated_by_model():
    """FR-32：质量契约由数据工程师显式声明，模型不得代劳。"""
    graph = _graph([PARSE_OK, MATCH_OK, PLAN_OK])
    result = graph.invoke(_initial(), _config())

    assert result["plan"]["quality_rules"] == []


# --------------------------------------------------------------------------
# 澄清中断（FR-09 / FR-10）
# --------------------------------------------------------------------------


def test_clarification_interrupts_then_resumes():
    graph = _graph([PARSE_NEEDS_CLARIFY, PARSE_OK, MATCH_OK, PLAN_OK])
    config = _config("thread-clarify")

    first = graph.invoke(_initial("run-clarify"), config)
    interrupts = first.get("__interrupt__")
    assert interrupts, "关键参数缺失时应中断提问"
    questions = interrupts[0].value["questions"]
    assert questions[0]["key"] == "target_table"

    second = graph.invoke(
        Command(resume={"answers": {"target_table": "orders_wide"}}), config
    )
    assert second["plan"]["target_table"] == "orders_wide"


def test_resume_does_not_call_model_twice_for_same_step():
    """断点恢复不应重复调用解析节点——这正是把提问拆成独立节点换来的收益。"""
    llm = FakeLLMClient([PARSE_NEEDS_CLARIFY, PARSE_OK, MATCH_OK, PLAN_OK])
    graph = build_generation_graph(llm, InMemorySaver())
    config = _config("thread-count")

    graph.invoke(_initial("run-count"), config)
    calls_after_first = len(llm.calls)

    graph.invoke(Command(resume={"answers": {"target_table": "orders_wide"}}), config)

    # 第一次：解析 1 次；恢复后：解析 1 次 + 算子 1 次 + 方案 1 次 = 4 次
    assert calls_after_first == 1
    assert len(llm.calls) == 4


# --------------------------------------------------------------------------
# 不支持与幻觉拦截（FR-11 / FR-14）
# --------------------------------------------------------------------------


def test_unsupported_requirement_ends_without_plan():
    graph = _graph([PARSE_OK, MATCH_UNSUPPORTED])
    result = graph.invoke(_initial(), _config("thread-unsupported"))

    assert result["supported"] is False
    assert "Kafka" in result["unsupported_reason"]
    assert result.get("plan") is None


def test_hallucinated_operator_is_rejected():
    """模型返回注册表里不存在的算子时必须拦截 —— 提示词约束不了幻觉。"""
    graph = _graph([PARSE_OK, MATCH_HALLUCINATED])
    result = graph.invoke(_initial(), _config("thread-hallucination"))

    assert result["supported"] is False
    assert "seatunnel.source.Kafka" in result["unsupported_reason"]
    assert result.get("plan") is None


def test_every_selected_operator_exists_in_registry():
    graph = _graph([PARSE_OK, MATCH_OK, PLAN_OK])
    result = graph.invoke(_initial(), _config("thread-registry"))

    from app.registry.catalog import get_operator

    for step in result["steps"]:
        assert get_operator(step["operator_id"]) is not None
