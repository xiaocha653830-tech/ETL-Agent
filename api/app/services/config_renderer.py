"""SeaTunnel 作业配置渲染（FR-13）。

**为什么用模板渲染而不是让模型生成配置**：
HOCON 的结构完全由 EtlPlan 决定，确定性渲染可复现、可测试；
而让模型写配置会产出「语义正确但语法无效」的结果——那类错误要到 T-08 门禁
才暴露，返工成本远高于在这里用模板兜住。

**凭据不落明文（FR-03）**：凭据类属性一律渲染成 ``${VAR}`` 占位符，
由控制面在执行时（T-15）注入，配置文件本身不含任何口令。
"""

from typing import Any

from app.core.errors import ModelOutputInvalid
from app.services.endpoint_context import EndpointContext

# 属性名最后一段命中这些词，其**值**就要占位化，绝不落明文：
#   - 凭据类：FR-03 的硬要求；
#   - 连接端点类：按数据源登记信息注入——模型填的目标地址不可信。
_PLACEHOLDER_TAILS = frozenset(
    {
        "password",
        "passwd",
        "username",
        "user",
        "token",
        "access_key",
        "secret_key",
        "url",
        "fenodes",
        "host",
        "port",
    }
)

# 由**渲染器**按运行上下文自动生成、不要求方案提供的属性。
# 注意与上面的区别：这些属性的值是实打实写进配置的（不占位化），
# 只是不指望方案来提供它们——门禁不该因此判方案不合格。
_RENDERER_SUPPLIED_TAILS = frozenset({"label-prefix"})

# Doris Sink 的幂等标签前缀（SPEC §11.1 V2：按作业全局唯一，用 run_id 天然满足 FR-27）
_LABEL_PREFIX_OPERATOR = "seatunnel.sink.Doris"


def is_injected_attribute(attribute_name: str) -> bool:
    """该属性的值是否由控制面在执行时注入（因而必须占位化）。"""
    return attribute_name.rsplit(".", 1)[-1].lower() in _PLACEHOLDER_TAILS


def requires_plan_attribute(attribute_name: str) -> bool:
    """门禁是否应要求方案提供该属性。

    凭据与连接端点由控制面注入、label-prefix 由渲染器生成——三者都不该由方案负责，
    因此门禁对它们一律不做必填校验。
    """
    tail = attribute_name.rsplit(".", 1)[-1].lower()
    return tail not in _PLACEHOLDER_TAILS and tail not in _RENDERER_SUPPLIED_TAILS


def _render_value(attribute_name: str, value: Any, *, endpoint: str) -> str:
    """渲染单个属性值。"""
    if is_injected_attribute(attribute_name):
        # 占位符只表达“运行时从哪一端注入”，绝不复用模型给出的潜在敏感值。
        tail = attribute_name.rsplit(".", 1)[-1].upper()
        return f'"${{{endpoint}_{tail}}}"'

    if isinstance(value, bool):
        return '"true"' if value else '"false"'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        items = ", ".join(f'"{item}"' for item in value)
        return f"[{items}]"
    if value is None:
        return "null"
    return f'"{value}"'


def _pick_step(steps: list[dict[str, Any]], role: str) -> dict[str, Any]:
    for step in steps:
        if step.get("role") == role:
            return step
    raise ModelOutputInvalid(f"方案缺少 {role} 算子，无法渲染配置")


def _render_operator_block(
    step: dict[str, Any],
    *,
    endpoint: str,
    extra_attributes: dict[str, str] | None = None,
) -> str:
    operator_name = step["operator_id"].rsplit(".", 1)[-1]
    # 在副本上补充系统属性，避免渲染动作反向修改已冻结或待冻结的方案对象。
    attributes: dict[str, Any] = dict(step.get("attributes") or {})
    attributes.update(extra_attributes or {})

    lines = [f"  {operator_name} {{"]
    for name, value in attributes.items():
        lines.append(f"    {name} = {_render_value(name, value, endpoint=endpoint)}")
    lines.append("  }")
    return "\n".join(lines)


def render_seatunnel_config(
    *,
    plan: dict[str, Any],
    source: EndpointContext,
    target: EndpointContext,
    run_id: str,
    parallelism: int = 2,
) -> str:
    """把 EtlPlan 渲染成可提交给 SeaTunnel 的 HOCON 配置文本。"""
    steps: list[dict[str, Any]] = list(plan.get("steps") or [])
    # P0 配置是单源单目标链；转换信息保留在 EtlPlan，后续扩展 transform 块时再渲染。
    source_step = _pick_step(steps, "source")
    sink_step = _pick_step(steps, "sink")

    if sink_step.get("operator_id") == _LABEL_PREFIX_OPERATOR:
        # 影子表写入需要全局唯一的导入标签；用 run_id 前缀同时满足幂等（FR-27）
        extra = {"sink.label-prefix": f"etl-{run_id}"}
    else:
        extra = {}

    header = "\n".join(
        [
            "# ============================================================",
            "# ETL-Agent 生成的作业配置候选（FR-13）",
            f"# 方案：{plan.get('name', '')}",
            f"# run_id：{run_id}",
            f"# 源端：{source.name}　　目标端：{target.name}",
            "# ⚠ 凭据以 ${VAR} 占位，由控制面在执行时注入（FR-03），配置不落明文",
            "# ============================================================",
        ]
    )

    body = "\n".join(
        [
            "env {",
            f"  parallelism = {parallelism}",
            '  job.mode = "BATCH"',
            "}",
            "",
            "source {",
            _render_operator_block(source_step, endpoint="SOURCE"),
            "}",
            "",
            "sink {",
            _render_operator_block(sink_step, endpoint="TARGET", extra_attributes=extra),
            "}",
        ]
    )

    return f"{header}\n\n{body}\n"
