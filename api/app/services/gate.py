"""确定性静态门禁（M5，FR-18）。

**「确定性」是硬要求**：相同输入必须得到相同结论——不调用模型、不做模糊匹配、
不做概率判断。三类检查：

  1. **配置语法** —— 用 HOCON 解析器真正解析，而不是靠正则猜；
  2. **算子在册** —— 方案里的算子必须命中 M4 注册表，且必填属性齐备；
  3. **Schema 对齐** —— 字段映射的两端字段必须真实存在于探查结果中。

门禁结论随版本一起冻结，作为审批与审计的证据（FR-19）。
缺少必要输入（如两端未做过探查）时该项记为 ``skipped`` 而非 ``failed``——
「没检查」和「检查不通过」必须区分开。
"""

import enum
from dataclasses import dataclass, field
from typing import Any

from pyhocon import ConfigFactory

from app.registry.catalog import get_operator
from app.services.config_renderer import requires_plan_attribute
from app.services.endpoint_context import EndpointContext


class CheckStatus(enum.StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"  # 缺少必要输入，无法判定——不等于通过


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    message: str
    details: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GateReport:
    passed: bool
    checks: list[CheckResult]

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [check for check in self.checks if check.status is CheckStatus.FAILED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status.value,
                    "message": check.message,
                    "details": check.details,
                }
                for check in self.checks
            ],
            "summary": {
                "passed": sum(1 for c in self.checks if c.status is CheckStatus.PASSED),
                "failed": len(self.failed_checks),
                "skipped": sum(1 for c in self.checks if c.status is CheckStatus.SKIPPED),
            },
        }


def _has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


# --------------------------------------------------------------------------
# 三项检查
# --------------------------------------------------------------------------


def check_config_syntax(config_text: str) -> CheckResult:
    """配置语法检查 —— 真正解析一次，解析不过就是不过。"""
    name = "配置语法"
    if not (config_text or "").strip():
        return CheckResult(name, CheckStatus.FAILED, "配置内容为空")

    try:
        # resolve=False 是必须的：SeaTunnel 用 ${VAR} 表示待注入的环境变量，
        # 而 HOCON 把 ${...} 也当变量引用语法。开启解析会因「变量不存在」误报，
        # 把一份完全合法的 SeaTunnel 配置判成语法错误。这里只校验语法结构。
        ConfigFactory.parse_string(config_text, resolve=False)
    except Exception as exc:  # noqa: BLE001 —— 解析器异常类型不稳定
        return CheckResult(
            name,
            CheckStatus.FAILED,
            f"HOCON 解析失败（{exc.__class__.__name__}）",
            [str(exc)[:300]],
        )
    return CheckResult(name, CheckStatus.PASSED, "HOCON 语法有效")


def check_operators(plan: dict[str, Any]) -> CheckResult:
    """算子合法性：全部在册 + 必填属性齐备。"""
    name = "算子在册与属性齐备"
    steps = plan.get("steps") or []
    if not steps:
        return CheckResult(name, CheckStatus.FAILED, "方案未包含任何算子")

    problems: list[str] = []
    for step in steps:
        operator_id = str(step.get("operator_id") or "")
        detail = get_operator(operator_id)
        if detail is None:
            problems.append(f"{operator_id or '(空)'}：不在受管算子注册表中")
            continue

        attributes = step.get("attributes") or {}
        missing = [
            attribute.name
            for attribute in detail.attributes
            if attribute.required
            # 凭据/连接端点由控制面注入、label-prefix 由渲染器生成，均不由方案负责
            and requires_plan_attribute(attribute.name)
            and not _has_value(attributes.get(attribute.name))
        ]
        if missing:
            problems.append(f"{operator_id}：缺少必填属性 {'、'.join(missing)}")

    if problems:
        return CheckResult(name, CheckStatus.FAILED, f"{len(problems)} 处问题", problems)
    return CheckResult(name, CheckStatus.PASSED, f"{len(steps)} 个算子全部在册且属性齐备")


def check_schema_alignment(
    plan: dict[str, Any],
    source: EndpointContext,
    target: EndpointContext,
) -> CheckResult:
    """Schema 对齐：字段映射的两端字段必须真实存在。

    没有探查结果时无法判定，记为 ``skipped`` —— 冻结仍可继续，
    但报告里会明确写出「未覆盖」，让审批人自己决定要不要先补探查。
    """
    name = "Schema 对齐"
    if not source.tables or not target.tables:
        missing_side = "源端" if not source.tables else "目标端"
        return CheckResult(
            name,
            CheckStatus.SKIPPED,
            f"{missing_side}缺少元数据探查结果，本次未执行对齐校验",
        )

    problems: list[str] = []
    source_table = str(plan.get("source_table") or "")
    target_table = str(plan.get("target_table") or "")

    source_fields = source.known_fields(source_table)
    if not source_fields:
        problems.append(f"源表 {source_table or '(未指定)'} 不在探查结果中")

    target_fields = target.known_fields(target_table)
    if not target_fields:
        problems.append(f"目标表 {target_table or '(未指定)'} 不在探查结果中")

    for mapping in plan.get("field_mappings") or []:
        source_field = str(mapping.get("source_field") or "")
        target_field = str(mapping.get("target_field") or "")
        if source_fields and source_field not in source_fields:
            problems.append(f"源字段 {source_field} 不存在于表 {source_table}")
        if target_fields and target_field not in target_fields:
            problems.append(f"目标字段 {target_field} 不存在于表 {target_table}")

    if problems:
        return CheckResult(name, CheckStatus.FAILED, f"{len(problems)} 处不对齐", problems)
    return CheckResult(name, CheckStatus.PASSED, "字段映射与两端 Schema 一致")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def run_gate(
    *,
    plan: dict[str, Any],
    config: str,
    source: EndpointContext,
    target: EndpointContext,
) -> GateReport:
    """执行全部门禁检查。

    ``skipped`` 不算失败——但会被如实记进报告，不掩盖未覆盖的检查项。
    """
    checks = [
        check_config_syntax(config),
        check_operators(plan),
        check_schema_alignment(plan, source, target),
    ]
    return GateReport(
        passed=not any(check.status is CheckStatus.FAILED for check in checks),
        checks=checks,
    )
