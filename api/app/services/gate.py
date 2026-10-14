"""确定性静态门禁（M5，FR-18）。

**「确定性」是硬要求**：相同输入必须得到相同结论——不调用模型、不做模糊匹配、
不做概率判断。五类检查：

  1. **配置语法** —— 用 HOCON 解析器真正解析，而不是靠正则猜；
  2. **方案结构** —— ``step_id`` 唯一、角色与注册表声明的算子类型一致、
     至少各有一个 source 与 sink；
  3. **算子在册与属性合法** —— 算子必须命中 M4 注册表、必填属性齐备、
     **不得出现注册表未声明的属性**、属性值的类型与取值必须符合注册表声明；
  4. **Schema 对齐** —— 字段映射的两端字段必须真实存在于探查结果中；
  5. **方案与配置一致** —— 配置里的算子必须就是被审方案里那几个。

门禁结论随版本一起冻结，作为审批与审计的证据（FR-19）。
缺少必要输入（如两端未做过探查）时该项记为 ``skipped`` 而非 ``failed``——
「没检查」和「检查不通过」必须区分开。但**数据源已不可用**属于「无从检查」，
一律按 ``failed`` 处理；生产环境（``strict=True``）连「没探查」也不放行。
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pyhocon import ConfigFactory

from app.registry.catalog import get_operator
from app.registry.schemas import AttributeType, OperatorDetail
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


# 已废弃 / 易误用的属性名。命中时给出可照做的修复指引，而不是一句「不在算子定义中」。
# 来源：各算子的 ``dialect_constraints``。
_DEPRECATED_ATTRIBUTES = {
    "table.identifier": "自 SeaTunnel 2.3.5 起已废弃，请改用 database + table",
}

_TYPE_LABEL = {
    AttributeType.STRING: "字符串",
    AttributeType.INT: "整数",
    AttributeType.LONG: "整数",
    AttributeType.BOOLEAN: "布尔值",
    AttributeType.LIST: "列表",
    AttributeType.MAP: "映射",
    AttributeType.ENUM: "枚举字符串",
}


def _value_matches(value: Any, expected: AttributeType) -> bool:
    """属性值是否符合注册表声明的类型。

    刻意**不做宽容转换**：注册表里 ``AttributeType`` 的用途就是约束生成端填入的值形态
    （见 ``app/registry/schemas.py``）。放松成「能转就转」会让一份渲染出来语法就错的
    配置（整数属性被写成带引号的 ``"2"``）通过门禁，把问题推到执行期才暴露。
    """
    if expected is AttributeType.STRING:
        return isinstance(value, str)
    if expected in (AttributeType.INT, AttributeType.LONG):
        # bool 是 int 的子类，不排除的话 True 会被当成合法整数
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is AttributeType.BOOLEAN:
        return isinstance(value, bool)
    if expected is AttributeType.LIST:
        return isinstance(value, list)
    if expected is AttributeType.MAP:
        return isinstance(value, dict)
    if expected is AttributeType.ENUM:
        return isinstance(value, str)
    return True


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


def check_plan_structure(plan: dict[str, Any]) -> CheckResult:
    """方案结构：``step_id`` 唯一、角色与算子类型一致、至少各一个源与汇。

    这些约束在配置渲染阶段也会被间接撞到（渲染器只取第一个 source / sink），
    但那时报出来的是「渲染失败」，归因不清；放在门禁里能一次说清是哪一步错了。
    """
    name = "方案结构"
    steps = plan.get("steps") or []
    if not steps:
        return CheckResult(name, CheckStatus.FAILED, "方案未包含任何算子")

    problems: list[str] = []
    seen: set[str] = set()
    roles: list[str] = []

    for step in steps:
        step_id = str(step.get("step_id") or "")
        # 渲染器以 step_id 标识算子块，重复会让 DAG 与配置指向同一个名字。
        if step_id in seen:
            problems.append(f"step_id 重复：{step_id}")
        seen.add(step_id)

        operator_id = str(step.get("operator_id") or "")
        role = str(step.get("role") or "")
        roles.append(role)

        detail = get_operator(operator_id)
        # 不在册的算子由「算子在册」检查负责，这里不重复报
        if detail is not None and role != detail.type.value:
            problems.append(
                f"{operator_id} 在注册表中是 {detail.type.value}，"
                f"方案中却标为 {role or '(空)'}"
            )

    if "source" not in roles:
        problems.append("方案缺少 source 算子，无法确定数据从哪来")
    if "sink" not in roles:
        problems.append("方案缺少 sink 算子，无法确定数据写到哪去")

    if problems:
        return CheckResult(name, CheckStatus.FAILED, f"{len(problems)} 处问题", problems)
    return CheckResult(name, CheckStatus.PASSED, f"{len(steps)} 个算子结构完整")


def _attribute_problems(
    operator_id: str, detail: OperatorDetail, attributes: dict[str, Any]
) -> list[str]:
    """单个算子的属性问题：未声明、必填缺失、类型不符、枚举越界。"""
    declared = {attribute.name: attribute for attribute in detail.attributes}
    problems: list[str] = []

    # 1) 未声明属性。注册表是权威清单（ADR-05），模型自造的属性名不会被渲染成有效配置，
    #    与其让它静默进入配置文件，不如在这里拒绝。
    for key in attributes:
        if key in declared:
            continue
        hint = _DEPRECATED_ATTRIBUTES.get(key)
        problems.append(
            f"{operator_id}：属性 {key} {hint}"
            if hint
            else f"{operator_id}：属性 {key} 不在该算子的属性定义中"
        )

    # 2) 必填齐备。凭据/连接端点由控制面注入、label-prefix 由渲染器生成，均不由方案负责。
    missing = [
        attribute.name
        for attribute in detail.attributes
        if attribute.required
        and requires_plan_attribute(attribute.name)
        and not _has_value(attributes.get(attribute.name))
    ]
    if missing:
        problems.append(f"{operator_id}：缺少必填属性 {'、'.join(missing)}")

    # 3) 类型与枚举。注入类属性的值会被占位符整体替换，校验它没有意义。
    for key, value in attributes.items():
        attribute = declared.get(key)
        if attribute is None or not requires_plan_attribute(key) or value is None:
            continue
        if not _value_matches(value, attribute.type):
            problems.append(
                f"{operator_id}：属性 {key} 应为{_TYPE_LABEL[attribute.type]}，"
                f"实际为 {type(value).__name__}"
            )
        elif (
            attribute.type is AttributeType.ENUM
            and attribute.enum_values
            and value not in attribute.enum_values
        ):
            problems.append(
                f"{operator_id}：属性 {key} 取值 {value!r} 不在允许范围 "
                f"（{'、'.join(attribute.enum_values)}）内"
            )

    return problems


def check_operators(plan: dict[str, Any]) -> CheckResult:
    """算子合法性：在册 + 必填齐备 + 属性声明合法 + 值与类型/枚举相符。"""
    name = "算子在册与属性齐备"
    steps = plan.get("steps") or []
    if not steps:
        return CheckResult(name, CheckStatus.FAILED, "方案未包含任何算子")

    problems: list[str] = []
    for step in steps:
        # 不在首个错误处返回，审批界面需要一次展示全部算子问题，减少往返修复次数。
        operator_id = str(step.get("operator_id") or "")
        detail = get_operator(operator_id)
        if detail is None:
            problems.append(f"{operator_id or '(空)'}：不在受管算子注册表中")
            continue

        problems.extend(
            _attribute_problems(operator_id, detail, step.get("attributes") or {})
        )

    if problems:
        return CheckResult(name, CheckStatus.FAILED, f"{len(problems)} 处问题", problems)
    return CheckResult(name, CheckStatus.PASSED, f"{len(steps)} 个算子全部在册且属性齐备")


def check_schema_alignment(
    plan: dict[str, Any],
    source: EndpointContext | None,
    target: EndpointContext | None,
    *,
    strict: bool = False,
) -> CheckResult:
    """Schema 对齐：字段映射的两端字段必须真实存在。

    三种「查不了」要区别对待，不能一律当没事：

    - **数据源上下文无法还原**（数据源或探查记录已被删除）→ 恒为 ``failed``。
      这不是「没检查」，而是「无从检查」——任何环境都不该带着未知 Schema 冻结。
    - **有数据源但没有探查结果** → 开发环境记 ``skipped``（不影响本地联调），
      生产环境记 ``failed``（``strict=True``）。没探查就等于没有依据放行。
    """
    name = "Schema 对齐"
    if source is None or target is None:
        missing_side = "源端" if source is None else "目标端"
        return CheckResult(
            name,
            CheckStatus.FAILED,
            f"{missing_side}数据源上下文不可用（数据源或探查记录可能已被删除），无法校验字段映射",
        )
    if not source.tables or not target.tables:
        missing_side = "源端" if not source.tables else "目标端"
        if strict:
            return CheckResult(
                name,
                CheckStatus.FAILED,
                f"{missing_side}缺少元数据探查结果，生产环境不允许跳过对齐校验",
            )
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


def check_plan_config_consistency(
    plan: dict[str, Any], config_text: str
) -> CheckResult:
    """方案 ↔ 配置一致性：配置里的算子必须就是被审方案里那几个。

    冻结是「方案 + 配置」的整体快照。两者若对不上，冻结出来的指纹固然唯一，
    却不代表任何真实可执行的东西——审的是一份方案，跑的是另一份配置。

    只比对**算子名**这一可判定的部分；属性值层面由渲染器的确定性负责
    （配置本就由方案渲染而来，不做二次解析比对）。
    """
    name = "方案与配置一致"
    if not (config_text or "").strip():
        # 空配置已经由「配置语法」判失败，这里不重复计一次失败
        return CheckResult(name, CheckStatus.SKIPPED, "配置为空，语法检查已报失败")

    try:
        parsed = ConfigFactory.parse_string(config_text, resolve=False)
    except Exception:  # noqa: BLE001 —— 解析器异常类型不稳定
        # 解析不了就无从比对；「配置语法」检查已经会把它判失败
        return CheckResult(name, CheckStatus.SKIPPED, "配置无法解析，语法检查已报失败")

    # 取块的动作放在 try 之外：缺 source / sink 块本身就是要报的结论，
    # 不能被当成「解析失败」悄悄跳过。pyhocon 在键缺失时抛异常而非返回 None，
    # 因此必须显式给出默认值。
    blocks = {"source": parsed.get("source", None), "sink": parsed.get("sink", None)}

    problems: list[str] = []
    for role, block in blocks.items():
        expected = {
            str(step.get("operator_id") or "").rsplit(".", 1)[-1]
            for step in (plan.get("steps") or [])
            if str(step.get("role") or "") == role
        }
        # 渲染器写出的算子块名取自 operator_id 末段，比对时同样归一化。
        actual = set(block.keys()) if isinstance(block, Mapping) else set()

        for operator_name in sorted(expected - actual):
            problems.append(
                f"方案中的 {role} 算子 {operator_name} 未出现在配置的 {role} 块中"
            )
        for operator_name in sorted(actual - expected):
            problems.append(
                f"配置的 {role} 块中出现了方案未声明的算子 {operator_name}"
            )

    if problems:
        return CheckResult(name, CheckStatus.FAILED, f"{len(problems)} 处不一致", problems)
    return CheckResult(name, CheckStatus.PASSED, "配置中的算子与方案一致")


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------


def run_gate(
    *,
    plan: dict[str, Any],
    config: str,
    source: EndpointContext | None,
    target: EndpointContext | None,
    strict: bool = False,
) -> GateReport:
    """执行全部门禁检查。

    ``skipped`` 不算失败——但会被如实记进报告，不掩盖未覆盖的检查项。
    ``strict=True``（生产环境）下，缺少探查结果的对齐检查由 ``skipped`` 升为 ``failed``。
    """
    checks = [
        check_config_syntax(config),
        check_plan_structure(plan),
        check_operators(plan),
        check_schema_alignment(plan, source, target, strict=strict),
        check_plan_config_consistency(plan, config),
    ]
    # skipped 代表证据缺失而非校验失败；只有明确 failed 才阻止冻结。
    return GateReport(
        passed=not any(check.status is CheckStatus.FAILED for check in checks),
        checks=checks,
    )
