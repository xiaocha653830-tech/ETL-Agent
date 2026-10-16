"""T-21：隔离的生成评测；复用状态机、门禁和 PDP，不提交数据作业。"""

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.config import settings
from app.core.errors import ModelUnavailable
from app.registry.catalog import get_operator
from app.services.commit import EXECUTION_TOOL
from app.services.endpoint_context import EndpointContext
from app.services.gate import CheckStatus, check_operators
from app.services.llm import DeepSeekClient, FakeLLMClient, LLMClient
from app.services.orchestrator import build_generation_graph, build_initial_state
from app.services.pdp import (
    AuthorizedSubjectPolicy,
    HighRiskToolPolicy,
    PolicyDecisionPoint,
)

logger = logging.getLogger(__name__)
Backend = Literal["injected", "deepseek"]
THRESHOLDS = {"operator_accuracy": 0.90, "attribute_accuracy": 0.85, "guardrail_rate": 1.0}
EVALUATOR_VERSION = "1"
_VERSION = re.compile(r"v[1-9][0-9]{0,5}\Z")
_SOURCE_ID = "11111111-1111-1111-1111-111111111111"
_TARGET_ID = "22222222-2222-2222-2222-222222222222"


class DatasetError(ValueError):
    """缺失、空集、非法预期均为数据集错误，不能伪装成零失败。"""


class GenerationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    requirement: str = Field(min_length=1, max_length=4000)
    source_type: Literal["mysql", "postgresql", "rest_api"] = "mysql"
    source_table: str = Field(min_length=1)
    target_table: str = Field(min_length=1)
    fields: list[str] = Field(min_length=1)
    mappings: dict[str, str] = Field(default_factory=dict)
    sensitivity: dict[str, str] = Field(default_factory=dict)
    expect_operators: list[str]
    expect_unsupported: bool = False
    expect_clarification: bool = False
    answers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_expectations(self) -> "GenerationCase":
        if len(set(self.fields)) != len(self.fields):
            raise ValueError("字段不能重复")
        if self.expect_unsupported:
            if self.expect_operators or self.expect_clarification:
                raise ValueError("不支持用例不得声明成功算子或澄清预期")
        elif not self.expect_operators or len(set(self.expect_operators)) != len(
            self.expect_operators
        ):
            raise ValueError("成功用例必须声明不重复的期望算子")
        if any(get_operator(operator) is None for operator in self.expect_operators):
            raise ValueError("期望算子不在注册表中")
        if self.expect_clarification and not self.answers:
            raise ValueError("澄清用例必须提供恢复答案")
        if set(self.mappings) - set(self.fields):
            raise ValueError("重命名字段必须在输入字段中")
        if any(level not in {"L1", "L2", "L3", "L4"} for level in self.sensitivity.values()):
            raise ValueError("敏感级别无效")
        return self


class GuardrailCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    scenario: str = Field(min_length=1, max_length=200)
    subject: str
    tool: str
    environment: str
    artifact: str
    expect_allow: bool = False
    expect_code: str | None = None

    @model_validator(mode="after")
    def validate_code(self) -> "GuardrailCase":
        if self.expect_allow and self.expect_code is not None:
            raise ValueError("放行对照用例不能声明拒绝码")
        if not self.expect_allow and self.expect_code not in {
            "POL_RISK_BLOCKED",
            "POL_SUBJECT_NOT_AUTHORIZED",
        }:
            raise ValueError("拒绝用例必须明确预期的 PDP 拒绝码")
        if self.tool != EXECUTION_TOOL:
            raise ValueError("v1 只评测已注册的受管提交工具")
        return self


@dataclass(frozen=True)
class Dataset:
    version: str
    sha256: str
    cases: tuple[GenerationCase, ...]
    guardrails: tuple[GuardrailCase, ...]


@dataclass(frozen=True)
class CaseOutcome:
    case_id: str
    passed: bool
    operator_hit: bool | None
    attribute_hit: bool | None
    selected: tuple[str, ...]
    unexpected: tuple[str, ...]
    missing: tuple[str, ...]
    status: str
    clarification_observed: bool
    detail: str


@dataclass(frozen=True)
class GuardrailOutcome:
    case_id: str
    scenario: str
    expected_allow: bool
    actual_allow: bool
    expected_code: str | None
    actual_code: str | None
    passed: bool


@dataclass(frozen=True)
class BenchmarkReport:
    dataset_version: str
    dataset_sha256: str
    backend: Backend
    model: str
    cases_total: int
    cases_passed: int
    generation_cases: int
    operator_hits: int
    attribute_hits: int
    operator_accuracy: float
    attribute_accuracy: float
    guardrails_total: int
    guardrails_blocked: int
    guardrail_rate: float
    controls_passed: bool
    behavior_cases_passed: bool
    outcomes: tuple[CaseOutcome, ...]
    guardrail_outcomes: tuple[GuardrailOutcome, ...]
    duration_ms: int

    @property
    def passed(self) -> bool:
        return (
            self.generation_cases > 0
            and self.guardrails_total > 0
            and self.operator_accuracy >= THRESHOLDS["operator_accuracy"]
            and self.attribute_accuracy >= THRESHOLDS["attribute_accuracy"]
            and self.guardrail_rate == THRESHOLDS["guardrail_rate"]
            and self.controls_passed
            and self.behavior_cases_passed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "passed": self.passed,
            "thresholds": dict(THRESHOLDS),
            "evaluator_version": EVALUATOR_VERSION,
            "layer": "L0" if self.backend == "injected" else "L2",
            "measures_real_model": self.backend == "deepseek",
            "attribute_scope": "用例级：门禁属性合法性 + 期望表/字段映射/分级一致性",
            "guardrail_scope": "隔离的既有 PDP 策略夹具；不证明生产授权全链路",
            "guardrail_failures": [
                asdict(item) for item in self.guardrail_outcomes if not item.passed
            ],
        }


def _dataset_root() -> Path:
    root = Path(__file__).resolve().parents[2] / "benchmarks"
    # wheel 将版本化数据一起打包；editable/源码布局仍使用 api/benchmarks。
    return root if root.is_dir() else Path(__file__).resolve().parents[1] / "benchmark_data"


def load_dataset(version: str, *, root: Path | None = None) -> Dataset:
    if not _VERSION.fullmatch(version):
        raise DatasetError("版本名必须形如 v1、v2，不接受路径")
    base = (root or _dataset_root()).resolve()
    folder = (base / version).resolve()
    if folder.parent != base:
        raise DatasetError("评测集路径越界")
    digest = hashlib.sha256()
    groups: list[list[Any]] = []
    for filename, schema in (("cases.jsonl", GenerationCase), ("guardrails.jsonl", GuardrailCase)):
        path = folder / filename
        if path.resolve().parent != folder or not path.is_file():
            raise DatasetError(f"评测集缺少 {filename}")
        if path.stat().st_size > 2_000_000:
            raise DatasetError(f"{filename} 超过大小上限")
        raw = path.read_bytes()
        digest.update(filename.encode() + b"\0" + raw)
        rows: list[Any] = []
        try:
            lines = raw.decode("utf-8-sig").splitlines()
        except UnicodeError as exc:
            raise DatasetError(f"{filename} 必须使用 UTF-8 编码") from exc
        for line_no, line in enumerate(lines, 1):
            if line.strip():
                try:
                    rows.append(schema.model_validate(json.loads(line)))
                except (json.JSONDecodeError, ValidationError) as exc:
                    raise DatasetError(f"{filename} 第 {line_no} 行格式或预期无效") from exc
        if not rows:
            raise DatasetError(f"{filename} 不能为空")
        groups.append(rows)
    cases, guardrails = groups
    ids = [item.id for group in groups for item in group]
    if len(set(ids)) != len(ids):
        raise DatasetError("用例 id 必须全局唯一")
    if not any(not case.expect_unsupported for case in cases):
        raise DatasetError("必须至少包含一个成功生成用例")
    if not any(not case.expect_allow for case in guardrails):
        raise DatasetError("必须至少包含一个高危拒绝用例")
    if not any(case.expect_allow for case in guardrails):
        raise DatasetError("必须包含合法放行对照，防止全拒绝策略假绿")
    return Dataset(version, digest.hexdigest(), tuple(cases), tuple(guardrails))


def _endpoints(case: GenerationCase) -> tuple[EndpointContext, EndpointContext]:
    types = {
        "order_id": "BIGINT",
        "order_amount": "DECIMAL(18,2)",
        "order_date": "DATE",
        "user_name": "VARCHAR(64)",
        "user_phone": "VARCHAR(20)",
    }
    columns = [
        {
            "name": field,
            "type": types.get(field, "VARCHAR(128)"),
            "level": case.sensitivity.get(field, "L2"),
            "category": "合成评测字段",
        }
        for field in case.fields
    ]
    source = EndpointContext(
        datasource_id=_SOURCE_ID,
        name="合成评测源",
        type=case.source_type,
        config={
            "host": "benchmark.invalid",
            "port": 5432 if case.source_type == "postgresql" else 3306,
            "database": "demo",
        },
        tables=[{"name": case.source_table, "columns": columns, "max_level": "L3"}],
    )
    target_columns = [
        {**column, "name": case.mappings.get(column["name"], column["name"])} for column in columns
    ]
    tables = [{"name": case.target_table, "columns": target_columns, "max_level": "L3"}]
    if case.expect_clarification:
        tables.append({"name": "ods_order_daily", "columns": target_columns, "max_level": "L3"})
    target = EndpointContext(
        datasource_id=_TARGET_ID,
        name="合成评测目标",
        type="doris",
        config={"host": "benchmark.invalid", "port": 8030, "database": "ods"},
        tables=tables,
    )
    return source, target


def injected_responses(case: GenerationCase) -> list[str]:
    """L0 故意按预期构造响应，只用于评测器自检，不是模型成绩。"""
    parsed = {
        "understood": case.requirement,
        "source_table": case.source_table,
        "target_table": case.target_table,
        "fields": case.fields,
        "need_clarification": [],
    }
    responses: list[dict[str, Any]] = []
    if case.expect_clarification:
        responses.append(
            {
                **parsed,
                "target_table": None,
                "need_clarification": [
                    {"key": "target_table", "question": "写入哪张目标表？", "options": []}
                ],
            }
        )
    responses.append(parsed)
    if case.expect_unsupported:
        responses.append({"supported": False, "reason": "受管清单中没有该需求要求的算子"})
    else:
        is_http = case.source_type == "rest_api"
        source_id = "seatunnel.source.Http" if is_http else "seatunnel.source.Jdbc"
        query = "SELECT " + ", ".join(case.fields) + " FROM " + case.source_table
        source_attrs: dict[str, Any] = {"url": "https://benchmark.invalid/orders", "method": "GET"}
        if not is_http:
            source_attrs = {
                "driver": "org.postgresql.Driver"
                if case.source_type == "postgresql"
                else "com.mysql.cj.jdbc.Driver",
                "query": query,
            }
        responses.append(
            {
                "supported": True,
                "steps": [
                    {
                        "step_id": "source",
                        "operator_id": source_id,
                        "role": "source",
                        "attributes": source_attrs,
                    },
                    {
                        "step_id": "sink",
                        "operator_id": "seatunnel.sink.Doris",
                        "role": "sink",
                        "attributes": {"database": "ods", "table": case.target_table},
                    },
                ],
            }
        )
        responses.append(
            {
                "name": case.id,
                "source_table": case.source_table,
                "target_table": case.target_table,
                "field_mappings": [
                    {
                        "source_field": field,
                        "target_field": case.mappings.get(field, field),
                        "sensitivity_level": case.sensitivity.get(field, "L2"),
                        "transform": None,
                    }
                    for field in case.fields
                ],
            }
        )
    return [json.dumps(value, ensure_ascii=False) for value in responses]


def score_generation(
    case: GenerationCase, state: dict[str, Any], *, clarification_observed: bool = False
) -> CaseOutcome:
    if case.expect_unsupported:
        passed = (
            state.get("supported") is False
            and not state.get("plan")
            and bool(state.get("unsupported_reason"))
        )
        actual_status = (
            "unsupported"
            if passed
            else ("interrupted" if state.get("__interrupt__") else "incomplete")
        )
        return CaseOutcome(
            case.id,
            passed,
            None,
            None,
            (),
            (),
            (),
            actual_status,
            clarification_observed,
            "不支持出口正确" if passed else "未正确拒绝不支持需求",
        )
    plan = state.get("plan") or {}
    selected = tuple(str(step.get("operator_id") or "") for step in plan.get("steps") or [])
    expected = set(case.expect_operators)
    unexpected = tuple(sorted(set(selected) - expected))
    missing = tuple(sorted(expected - set(selected)))
    operator_hit = (
        bool(selected)
        and not missing
        and not unexpected
        and all(get_operator(operator) is not None for operator in selected)
        and len(selected) == len(expected)
    )
    operator_hit = operator_hit and all(
        (operator := get_operator(str(step.get("operator_id") or ""))) is not None
        and step.get("role") == operator.type.value
        for step in plan.get("steps") or []
    )
    gate = check_operators(plan)
    tables_hit = (
        plan.get("source_table") == case.source_table
        and plan.get("target_table") == case.target_table
    )
    expected_pairs = {(field, case.mappings.get(field, field)) for field in case.fields}
    mappings = plan.get("field_mappings") or []
    actual_pairs = {(item.get("source_field"), item.get("target_field")) for item in mappings}
    fields_hit = expected_pairs == actual_pairs and len(mappings) == len(expected_pairs)
    sensitivity_hit = all(
        item.get("sensitivity_level") == case.sensitivity[field]
        for item in mappings
        for field in case.sensitivity
        if item.get("source_field") == field
    )
    sources = [step for step in plan.get("steps") or [] if step.get("role") == "source"]
    expected_driver = (
        "org.postgresql.Driver" if case.source_type == "postgresql" else "com.mysql.cj.jdbc.Driver"
    )
    driver_hit = case.source_type == "rest_api" or (
        len(sources) == 1 and sources[0].get("attributes", {}).get("driver") == expected_driver
    )
    attributes_hit = (
        gate.status is CheckStatus.PASSED
        and tables_hit
        and fields_hit
        and sensitivity_hit
        and driver_hit
    )
    clarification_hit = not case.expect_clarification or clarification_observed
    passed = operator_hit and attributes_hit and clarification_hit and bool(state.get("config"))
    checks = {
        "算子选择": operator_hit,
        "属性门禁": gate.status is CheckStatus.PASSED,
        "表名": tables_hit,
        "字段映射": fields_hit,
        "敏感分级": sensitivity_hit,
        "驱动选择": driver_hit,
        "澄清行为": clarification_hit,
        "配置生成": bool(state.get("config")),
    }
    failed = [name for name, ok in checks.items() if not ok]
    return CaseOutcome(
        case.id,
        passed,
        operator_hit,
        attributes_hit,
        selected,
        unexpected,
        missing,
        "generated" if plan else "incomplete",
        clarification_observed,
        "全部符合预期" if not failed else "未通过：" + "、".join(failed),
    )


def run_generation_cases(
    cases: tuple[GenerationCase, ...],
    client_factory: Callable[[GenerationCase], LLMClient],
) -> tuple[CaseOutcome, ...]:
    outcomes: list[CaseOutcome] = []
    for case in cases:
        observed = False
        try:
            source, target = _endpoints(case)
            graph = build_generation_graph(client_factory(case), InMemorySaver())
            run_id = str(uuid.uuid4())
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": 24}
            state = graph.invoke(
                build_initial_state(
                    run_id=run_id,
                    requirement=case.requirement,
                    source=source,
                    target=target,
                ),
                config,
            )
            for _ in range(2):
                if not state.get("__interrupt__"):
                    break
                observed = True
                if not case.answers:
                    break
                state = graph.invoke(Command(resume={"answers": case.answers}), config)
            outcomes.append(score_generation(case, state, clarification_observed=observed))
        except Exception as exc:  # noqa: BLE001 —— 单例故障必须计入分母，且不泄露模型原文
            logger.warning("评测用例失败 case=%s kind=%s", case.id, type(exc).__name__)
            scored = not case.expect_unsupported
            outcomes.append(
                CaseOutcome(
                    case.id,
                    False,
                    False if scored else None,
                    False if scored else None,
                    (),
                    (),
                    (),
                    "error",
                    observed,
                    f"执行失败：{type(exc).__name__}",
                )
            )
    return tuple(outcomes)


def benchmark_pdp() -> PolicyDecisionPoint:
    """复用真实策略类的隔离夹具；不注册/修改进程级生产 PDP。"""
    return PolicyDecisionPoint(
        [
            AuthorizedSubjectPolicy(allowed_subjects=frozenset({"benchmark:operator"})),
            HighRiskToolPolicy(
                high_risk_tools=frozenset({EXECUTION_TOOL}),
                allowed_environments=frozenset({"dev", "demo"}),
            ),
        ]
    )


def run_guardrail_cases(
    cases: tuple[GuardrailCase, ...],
    *,
    pdp: PolicyDecisionPoint | None = None,
) -> tuple[GuardrailOutcome, ...]:
    policy = pdp or benchmark_pdp()
    outcomes = []
    for case in cases:
        decision = policy.decide(case.subject, case.tool, case.environment, case.artifact)
        code = decision.code.value if decision.code else None
        passed = decision.allow == case.expect_allow and code == case.expect_code
        outcomes.append(
            GuardrailOutcome(
                case.id,
                case.scenario,
                case.expect_allow,
                decision.allow,
                case.expect_code,
                code,
                passed,
            )
        )
    return tuple(outcomes)


def run_benchmark(
    dataset: Dataset,
    *,
    backend: Backend = "injected",
    client_factory: Callable[[GenerationCase], LLMClient] | None = None,
    pdp: PolicyDecisionPoint | None = None,
) -> BenchmarkReport:
    if backend not in ("injected", "deepseek"):
        raise ValueError("不支持的评测后端")
    if backend == "deepseek" and client_factory is not None:
        raise ValueError("真实模型模式不能注入替身客户端，以免错误标记真实成绩")
    if client_factory is None:
        if backend == "injected":

            def injected(case: GenerationCase) -> LLMClient:
                return FakeLLMClient(injected_responses(case))

            client_factory = injected
        else:
            if not settings.deepseek_api_key:
                raise ModelUnavailable("L2 评测需要已配置的 DEEPSEEK_API_KEY")
            client = DeepSeekClient(
                settings.deepseek_api_key,
                settings.deepseek_base_url,
                settings.deepseek_model,
                settings.llm_timeout_seconds,
            )

            def real_client(_case: GenerationCase) -> LLMClient:
                return client

            client_factory = real_client
    started = time.monotonic()
    outcomes = run_generation_cases(dataset.cases, client_factory)
    guards = run_guardrail_cases(dataset.guardrails, pdp=pdp)
    generation_cases = sum(not case.expect_unsupported for case in dataset.cases)
    denied = [outcome for outcome in guards if not outcome.expected_allow]
    if not generation_cases or not denied or not any(item.expected_allow for item in guards):
        raise DatasetError("评测分母不能为空")
    operator_hits = sum(outcome.operator_hit is True for outcome in outcomes)
    attribute_hits = sum(outcome.attribute_hit is True for outcome in outcomes)
    blocked = sum(outcome.passed for outcome in denied)
    special_ids = {
        case.id for case in dataset.cases if case.expect_unsupported or case.expect_clarification
    }
    return BenchmarkReport(
        dataset.version,
        dataset.sha256,
        backend,
        settings.deepseek_model if backend == "deepseek" else "FakeLLMClient",
        len(outcomes),
        sum(outcome.passed for outcome in outcomes),
        generation_cases,
        operator_hits,
        attribute_hits,
        operator_hits / generation_cases,
        attribute_hits / generation_cases,
        len(denied),
        blocked,
        blocked / len(denied),
        all(outcome.passed for outcome in guards if outcome.expected_allow),
        all(outcome.passed for outcome in outcomes if outcome.case_id in special_ids),
        outcomes,
        guards,
        round((time.monotonic() - started) * 1000),
    )
