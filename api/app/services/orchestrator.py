"""需求生成状态机（M3，FR-08 ~ FR-14；技术选型见 SPEC §2「LangGraph + PostgresSaver」）。

图结构（线性主链 + 两个提前出口）：

    parse_requirement ──► match_operators ──► generate_plan ──► generate_config ──► END
           │                     │
      interrupt               不支持
       （FR-09）              （FR-14）
           ▼                     ▼
         暂停                   END

三条设计原则：

  1. **图是纯函数式的** —— 节点不访问数据库，两端上下文在入图前备好。
     因此状态机可以完全脱离 PostgreSQL 做单元测试（checkpointer 换成 ``InMemorySaver``）。
  2. **澄清中断用 LangGraph 原生 ``interrupt``** —— 暂停时由 checkpointer 自动保存断点，
     ``Command(resume=...)`` 从断点续跑，不重跑已完成阶段（FR-10）。
  3. **服务端独立校验模型输出** —— 模型给出的算子标识必须命中 M4 注册表，
     否则一律判为「不支持」。提示词约束不了幻觉，只有校验能（FR-14）。
"""

import logging
import threading
from contextlib import contextmanager
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.core.config import settings
from app.registry.catalog import get_operator
from app.services.config_renderer import render_seatunnel_config
from app.services.endpoint_context import EndpointContext
from app.services.llm import LLMClient
from app.services.prompts import (
    build_operator_messages,
    build_parse_messages,
    build_plan_messages,
)

logger = logging.getLogger(__name__)

# 澄清轮次上限：超过后不再追问，直接把已得到的方案交出去并附警告。
# 用「已收集的补充参数个数」而非循环计数——节点在 resume 时会重新执行，
# 局部计数器会被重置，只有 state 里的数据是跨断点可靠的。
MAX_CLARIFICATION_PARAMS = 10


class GenerationState(TypedDict, total=False):
    """图的共享状态。

    ⚠ 所有字段都必须是 **JSON 可序列化**的——PostgresSaver 会把每个断点写成 JSON，
    塞 dataclass 会在保存 checkpoint 时失败。
    """

    run_id: str
    requirement: str
    source_datasource_id: str
    target_datasource_id: str
    source_context: str
    target_context: str
    source_meta: dict[str, Any]
    target_meta: dict[str, Any]

    clarifications: dict[str, Any]
    parsed: dict[str, Any]
    pending_questions: list[dict[str, Any]]

    supported: bool
    steps: list[dict[str, Any]]
    unsupported_reason: str

    plan: dict[str, Any]
    config: str
    warnings: list[str]


# --------------------------------------------------------------------------
# 模型输出的清洗与校验
# --------------------------------------------------------------------------


def _normalize_questions(raw: Any) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        question = str(item.get("question") or "").strip()
        if not key or not question:
            continue
        questions.append(
            {
                "key": key[:64],
                "question": question[:500],
                "hint": (str(item["hint"])[:500] if item.get("hint") else None),
                "options": [str(option) for option in (item.get("options") or [])][:10],
            }
        )
    return questions


def _normalize_answers(raw: Any) -> dict[str, Any]:
    """把 resume 传入的内容规范成 ``{key: value}``。"""
    if isinstance(raw, dict):
        answers = raw.get("answers") if "answers" in raw else raw
        if isinstance(answers, dict):
            return {str(key): value for key, value in answers.items()}
    return {}


def _normalize_steps(raw: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for index, item in enumerate(raw or [], start=1):
        if not isinstance(item, dict):
            continue
        operator_id = str(item.get("operator_id") or "").strip()
        role = str(item.get("role") or "").strip().lower()
        if not operator_id or role not in ("source", "transform", "sink"):
            continue
        attributes = item.get("attributes")
        steps.append(
            {
                "step_id": str(item.get("step_id") or f"step{index}")[:64],
                "operator_id": operator_id[:128],
                "role": role,
                "attributes": attributes if isinstance(attributes, dict) else {},
                "note": (str(item["note"])[:200] if item.get("note") else None),
            }
        )
    return steps


def _normalize_mappings(raw: Any) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        source_field = str(item.get("source_field") or "").strip()
        target_field = str(item.get("target_field") or "").strip()
        if not source_field or not target_field:
            continue
        level = item.get("sensitivity_level")
        mappings.append(
            {
                "source_field": source_field,
                "target_field": target_field,
                "transform": (str(item["transform"]) if item.get("transform") else None),
                "sensitivity_level": (
                    str(level) if isinstance(level, str) and level in _LEVELS else None
                ),
            }
        )
    return mappings


_LEVELS = frozenset({"L1", "L2", "L3", "L4"})


# --------------------------------------------------------------------------
# 图构建
# --------------------------------------------------------------------------


def build_generation_graph(llm: LLMClient, checkpointer: Any):
    """编译状态机。

    :param llm: 模型客户端；测试传 :class:`FakeLLMClient`
    :param checkpointer: 断点存储；生产用 ``PostgresSaver``，测试用 ``InMemorySaver``
    """

    def parse_requirement(state: GenerationState) -> dict[str, Any]:
        """解析需求。

        **不在这里 interrupt** —— 提问交给独立的 ``ask_clarification`` 节点。
        拆开的原因：节点在 resume 时会整体重新执行，若把 LLM 调用和 interrupt
        放在同一节点，每次恢复都要重复调用一次模型。拆开后解析结果会随
        ``pending_questions`` 存进 state，恢复时无需重算。
        """
        clarifications = dict(state.get("clarifications") or {})
        warnings = list(state.get("warnings") or [])

        parsed = llm.complete_json(
            build_parse_messages(
                requirement=state["requirement"],
                source_context=state["source_context"],
                target_context=state["target_context"],
                clarifications=clarifications,
            )
        )
        questions = _normalize_questions(parsed.get("need_clarification"))

        if questions and len(clarifications) >= MAX_CLARIFICATION_PARAMS:
            # 已达澄清上限，不再追问，带着问题继续（方案可能不完整）
            warnings.append(
                f"澄清轮次已达上限（{MAX_CLARIFICATION_PARAMS} 项），方案可能不完整，请人工复核。"
            )
            questions = []

        return {
            "parsed": parsed,
            "clarifications": clarifications,
            "pending_questions": questions,
            "warnings": warnings,
        }

    def ask_clarification(state: GenerationState) -> dict[str, Any]:
        """向用户提问并等待补充（FR-09）。

        整个节点只做一件事：把 ``pending_questions`` 抛给用户，拿到答案并入
        ``clarifications``，然后交回 ``parse_requirement`` 重新解析。
        resume 时本节点会重新执行，``interrupt()`` 直接返回用户提交的答案。
        """
        answers = interrupt({"questions": state.get("pending_questions") or []})
        merged = dict(state.get("clarifications") or {})
        merged.update(_normalize_answers(answers))
        return {"clarifications": merged, "pending_questions": []}

    def match_operators(state: GenerationState) -> dict[str, Any]:
        """在受管清单内选择算子；选不中即判「不支持」（FR-11 / FR-14）。"""
        response = llm.complete_json(
            build_operator_messages(
                requirement=state["requirement"],
                parsed=state.get("parsed") or {},
                source_context=state["source_context"],
                target_context=state["target_context"],
                clarifications=state.get("clarifications") or {},
            )
        )

        if not response.get("supported"):
            reason = str(response.get("reason") or "").strip() or "当前算子不支持该需求"
            logger.info("算子匹配判定为不支持：%s", reason)
            return {"supported": False, "unsupported_reason": reason, "steps": []}

        steps = _normalize_steps(response.get("steps"))
        if not steps:
            return {
                "supported": False,
                "unsupported_reason": "模型未给出任何可用算子",
                "steps": [],
            }

        # 幻觉拦截：模型可能返回一个「看起来合理」但并不存在的算子标识
        unknown = [
            step["operator_id"]
            for step in steps
            if get_operator(step["operator_id"]) is None
        ]
        if unknown:
            reason = f"模型返回了未注册的算子：{'、'.join(unknown)}"
            logger.warning("算子幻觉已拦截：%s", unknown)
            return {"supported": False, "unsupported_reason": reason, "steps": []}

        return {"supported": True, "steps": steps, "unsupported_reason": ""}

    def generate_plan(state: GenerationState) -> dict[str, Any]:
        """生成结构化方案（FR-12）。"""
        response = llm.complete_json(
            build_plan_messages(
                parsed=state.get("parsed") or {},
                steps=state.get("steps") or [],
                source_context=state["source_context"],
                target_context=state["target_context"],
            )
        )

        warnings = list(state.get("warnings") or [])
        unmapped = response.get("unmapped_source_fields") or []
        if unmapped:
            joined = "、".join(str(item) for item in unmapped)
            warnings.append(f"以下源字段未映射到目标端：{joined}")
        warnings.extend(str(item) for item in (response.get("warnings") or []))

        parsed = state.get("parsed") or {}
        plan: dict[str, Any] = {
            "name": str(
                response.get("name") or parsed.get("understood") or "未命名方案"
            )[:128],
            "description": str(response.get("description") or "")[:1000],
            "source_datasource_id": state["source_datasource_id"],
            "target_datasource_id": state["target_datasource_id"],
            "source_table": str(
                response.get("source_table") or parsed.get("source_table") or ""
            ),
            "target_table": str(
                response.get("target_table") or parsed.get("target_table") or ""
            ),
            "steps": state.get("steps") or [],
            "field_mappings": _normalize_mappings(response.get("field_mappings")),
            # FR-32：质量契约由数据工程师显式声明，模型不生成
            "quality_rules": [],
        }
        return {"plan": plan, "warnings": warnings}

    def generate_config(state: GenerationState) -> dict[str, Any]:
        """渲染 SeaTunnel 配置候选（FR-13）。"""
        config = render_seatunnel_config(
            plan=state["plan"],
            source=EndpointContext(**state["source_meta"]),
            target=EndpointContext(**state["target_meta"]),
            run_id=state["run_id"],
        )
        return {"config": config}

    def route_after_parse(state: GenerationState) -> str:
        return "clarify" if state.get("pending_questions") else "match"

    def route_after_match(state: GenerationState) -> str:
        return "continue" if state.get("supported") else "stop"

    graph = StateGraph(GenerationState)
    graph.add_node("parse_requirement", parse_requirement)
    graph.add_node("ask_clarification", ask_clarification)
    graph.add_node("match_operators", match_operators)
    graph.add_node("generate_plan", generate_plan)
    graph.add_node("generate_config", generate_config)

    graph.add_edge(START, "parse_requirement")
    graph.add_conditional_edges(
        "parse_requirement",
        route_after_parse,
        {"clarify": "ask_clarification", "match": "match_operators"},
    )
    # 补充信息后回到解析节点重新理解需求（可能引发新一轮提问）
    graph.add_edge("ask_clarification", "parse_requirement")
    graph.add_conditional_edges(
        "match_operators",
        route_after_match,
        {"continue": "generate_plan", "stop": END},
    )
    graph.add_edge("generate_plan", "generate_config")
    graph.add_edge("generate_config", END)

    return graph.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------
# 断点存储
# --------------------------------------------------------------------------


@contextmanager
def postgres_checkpointer():
    """PostgresSaver 上下文（生产形态）。

    LangGraph 自管自己的 checkpoint 表，不走 Alembic；
    ``setup()`` 是幂等的，重复调用安全。

    ⚠ 每次调用都会新建连接池。接口层应使用 :func:`get_checkpointer` 复用单例。
    """
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(settings.langgraph_dsn) as saver:
        saver.setup()
        yield saver


_checkpointer: Any = None
_checkpointer_lock = threading.Lock()


def get_checkpointer() -> Any:
    """进程级单例的 PostgresSaver（内部维持连接池）。

    LangGraph 把断点写进自己管理的表；``setup()`` 幂等，只做一次。
    """
    global _checkpointer
    if _checkpointer is None:
        with _checkpointer_lock:
            if _checkpointer is None:
                from langgraph.checkpoint.postgres import PostgresSaver
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool

                # 两个 kwargs 都是必需的：
                #   autocommit —— setup() 会执行 CREATE INDEX CONCURRENTLY，
                #     该语句不允许出现在事务块内，默认的 autocommit=False 必然报错；
                #   dict_row —— langgraph 读取 checkpoint 时按列名取值。
                pool = ConnectionPool(
                    conninfo=settings.langgraph_dsn,
                    max_size=5,
                    open=True,
                    kwargs={"autocommit": True, "row_factory": dict_row},
                )
                saver = PostgresSaver(pool)
                saver.setup()
                _checkpointer = saver
    return _checkpointer


def build_initial_state(
    *,
    run_id: str,
    requirement: str,
    source: EndpointContext,
    target: EndpointContext,
) -> GenerationState:
    """组装入图状态 —— 把 dataclass 摊平成 JSON 可序列化的 dict。"""
    return {
        "run_id": run_id,
        "requirement": requirement,
        "source_datasource_id": source.datasource_id,
        "target_datasource_id": target.datasource_id,
        "source_context": source.to_prompt_text(),
        "target_context": target.to_prompt_text(),
        "source_meta": {
            "datasource_id": source.datasource_id,
            "name": source.name,
            "type": source.type,
            "config": source.config,
            "tables": source.tables,
        },
        "target_meta": {
            "datasource_id": target.datasource_id,
            "name": target.name,
            "type": target.type,
            "config": target.config,
            "tables": target.tables,
        },
        "clarifications": {},
        "warnings": [],
        "supported": True,
    }
