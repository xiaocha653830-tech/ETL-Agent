"""M3 的提示词模板（FR-08 ~ FR-14）。

三条纪律贯穿全部提示词，直接对应三条 FR：

  1. **只输出 JSON** —— 便于 :func:`llm.extract_json` 确定性地解析；
  2. **不确定就问，不要猜** —— 缺失参数进 ``need_clarification``（FR-09）；
  3. **算子只能从受管清单里选** —— 清单外一律「不支持」（FR-11 / FR-14）。

注意：提示词只是**第一道约束**。模型输出回到服务端后仍会被独立校验
（算子标识必须命中注册表），因为提示词约束不了幻觉。
"""

from typing import Any

from app.registry.catalog import list_operators
from app.services.llm import Message

_MANIFEST_PLACEHOLDER = "%%MANIFEST%%"


# --------------------------------------------------------------------------
# 受管算子清单渲染
# --------------------------------------------------------------------------


def render_registry_manifest(*, with_attributes: bool = True) -> str:
    """把受管算子清单渲染成提示词片段。

    这份文本是模型能看到的**全部**算子空间——注册表里没有的，模型就选不出来。
    """
    lines: list[str] = []
    for operator in list_operators():
        lines.append(f"- {operator.operator_id} [{operator.type.value}] {operator.description}")
        if not with_attributes:
            continue
        required = [attr.name for attr in operator.attributes if attr.required]
        optional = [attr.name for attr in operator.attributes if not attr.required]
        lines.append(f"    必填属性：{'、'.join(required) if required else '（无）'}")
        if optional:
            shown = "、".join(optional[:8])
            lines.append(f"    可选属性：{shown}{' …' if len(optional) > 8 else ''}")
        for constraint in operator.dialect_constraints[:3]:
            lines.append(f"    ⚠ {constraint}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 节点一：需求解析
# --------------------------------------------------------------------------

_PARSE_SYSTEM = """你是 ETL 数据集成平台的【需求解析器】。

职责：把用户的自然语言需求解析成结构化参数。

铁律（违反即视为失败）：
1. 只输出一个 JSON 对象，不要输出解释、前言或 Markdown 代码块。
2. 缺失或不明确的**关键**参数必须放进 need_clarification，绝对不要猜测或补默认值。
3. 不要编造用户未提到的表名或字段名。
4. 用户已补充过的信息视为已知，不要重复追问。
5. 仅当缺少的参数会导致方案无法生成时才提问；能从需求中合理推断的不要问。

输出 JSON 结构：
{
  "understood": "用一句话复述你对需求的理解",
  "source_table": "源表名，未提及则为 null",
  "target_table": "目标表名，未提及则为 null",
  "fields": ["需要同步的字段名，未提及则为空数组"],
  "filters": "过滤条件，未提及则为 null",
  "schedule": "调度需求，未提及则为 null",
  "need_clarification": [
    {"key": "唯一短标识", "question": "要问用户的问题", "hint": "补充说明或 null",
     "options": ["可选项，没有则空数组"]}
  ]
}"""


def build_parse_messages(
    *,
    requirement: str,
    source_context: str,
    target_context: str,
    clarifications: dict[str, Any] | None = None,
) -> list[Message]:
    parts = [
        f"【源端数据源】\n{source_context}",
        f"【目标端数据源】\n{target_context}",
        f"【用户需求】\n{requirement}",
    ]
    if clarifications:
        answered = "\n".join(f"- {key}：{value}" for key, value in clarifications.items())
        parts.append(f"【用户已补充的参数】\n{answered}")
    return [
        Message(role="system", content=_PARSE_SYSTEM),
        Message(role="user", content="\n\n".join(parts)),
    ]


# --------------------------------------------------------------------------
# 节点二：算子匹配（FR-11 / FR-14 的关键节点）
# --------------------------------------------------------------------------

_OPERATOR_SYSTEM = """你是 ETL 平台的【算子选择器】。

你**只能**从下面这份受管算子清单中选择。清单之外的一切算子都视为不存在。

%%MANIFEST%%

铁律（违反即视为失败）：
1. 只输出一个 JSON 对象。
2. 若需求无法由清单中的算子实现，必须返回 supported=false 并说明原因。
3. 绝对不要编造不存在的算子标识，也不要勉强套用语义不符的算子——宁可判为不支持。
4. attributes 的属性名必须来自清单，值必须符合该属性的类型与约束。

输出结构（不支持时）：
{"supported": false, "reason": "具体、可操作的原因"}

输出结构（支持时）：
{"supported": true, "steps": [
  {"step_id": "唯一短标识", "operator_id": "清单中的完整标识",
   "role": "source 或 sink", "attributes": {"属性名": "属性值"}, "note": "选择理由或 null"}
]}"""


def build_operator_messages(
    *,
    requirement: str,
    parsed: dict[str, Any],
    source_context: str,
    target_context: str,
    clarifications: dict[str, Any] | None = None,
) -> list[Message]:
    system = _OPERATOR_SYSTEM.replace(_MANIFEST_PLACEHOLDER, render_registry_manifest())

    lines = [
        f"【源端数据源】\n{source_context}",
        f"【目标端数据源】\n{target_context}",
        f"【原始需求】\n{requirement}",
        f"【已解析的参数】\n{parsed}",
    ]
    if clarifications:
        lines.append(f"【用户补充】\n{clarifications}")
    lines.append("请从受管清单中选择实现该需求所需的算子，按数据流向排列（先 source 后 sink）。")

    return [
        Message(role="system", content=system),
        Message(role="user", content="\n\n".join(lines)),
    ]


# --------------------------------------------------------------------------
# 节点三：方案生成
# --------------------------------------------------------------------------

_PLAN_SYSTEM = """你是 ETL 平台的【方案生成器】。

给定已确定的算子链与两端的表结构，产出完整的结构化作业方案。

铁律（违反即视为失败）：
1. 只输出一个 JSON 对象，不要输出解释或 Markdown 代码块。
2. 字段映射只能使用给定的源端字段与目标端字段，**不要发明字段**。
3. 源端存在但目标端没有对应列的字段，放进 unmapped_source_fields，不要硬套到别的列上。
4. 敏感字段（分级 L3 及以上）的映射要保留其分级信息，供审批环节参考。

输出 JSON 结构：
{
  "name": "简短方案名",
  "description": "一句话说明这个方案做什么",
  "source_table": "源表名",
  "target_table": "目标表名",
  "field_mappings": [
    {"source_field": "源字段", "target_field": "目标字段",
     "transform": "转换表达式或 null", "sensitivity_level": "L1~L4 或 null"}
  ],
  "unmapped_source_fields": ["未映射的源字段"],
  "warnings": ["需要人工注意的点"]
}"""


def build_plan_messages(
    *,
    parsed: dict[str, Any],
    steps: list[dict[str, Any]],
    source_context: str,
    target_context: str,
) -> list[Message]:
    user_content = "\n\n".join(
        [
            f"【源端数据源】\n{source_context}",
            f"【目标端数据源】\n{target_context}",
            f"【已解析的需求】\n{parsed}",
            f"【已确定的算子链】\n{steps}",
        ]
    )
    return [
        Message(role="system", content=_PLAN_SYSTEM),
        Message(role="user", content=user_content),
    ]
