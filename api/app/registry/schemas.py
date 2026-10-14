"""算子能力注册表的响应契约（SPEC §3 M4 / PRD 附录 B）。

注册表把「受管算子清单」固化为可校验的数据契约，供 M3 需求生成时约束模型的
生成空间（FR-11），并在无法匹配时明确返回「不支持」（FR-14）。
"""

import enum

from pydantic import BaseModel, ConfigDict, Field


class OperatorType(enum.StrEnum):
    """算子类型 —— 严格对应 SeaTunnel 配置中的 source / sink / transform 段。"""

    SOURCE = "source"
    SINK = "sink"
    TRANSFORM = "transform"


class AttributeType(enum.StrEnum):
    """算子属性值类型 —— 用于约束生成端应填入的值形态。"""

    STRING = "string"
    INT = "int"
    LONG = "long"
    BOOLEAN = "boolean"
    LIST = "list"
    MAP = "map"
    ENUM = "enum"


class OperatorAttribute(BaseModel):
    """单个算子属性的规格（名称 / 是否必填 / 默认值 / 类型）。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="属性名，严格对应 SeaTunnel 配置键")
    required: bool = Field(description="是否必填")
    default: str | None = Field(
        default=None, description="默认值（统一以文本表示）；无默认值或不确定时为 null"
    )
    type: AttributeType = Field(description="属性值类型")
    enum_values: list[str] | None = Field(default=None, description="枚举可选值；非枚举为 null")
    description: str = Field(description="属性用途说明")


class OperatorBrief(BaseModel):
    """算子极简信息 —— 仅「标识 + 类型 + 一句话描述」。

    供 M3 注入大模型上下文使用：模型只能从该清单里选算子，选不中即返回「不支持」。
    """

    model_config = ConfigDict(frozen=True)

    operator_id: str = Field(description="算子标识，如 seatunnel.source.MySQL-CDC")
    type: OperatorType = Field(description="算子类型")
    description: str = Field(description="一句话描述")


class OperatorSummary(OperatorBrief):
    """算子精简信息 —— 在极简信息基础上补充实现制品，用于列表展示。"""

    connector_artifact: str = Field(
        description="实现制品（connector jar 前缀），如 connector-cdc-mysql"
    )


class OperatorDetail(OperatorSummary):
    """算子完整信息 —— 含属性清单与方言约束，是注册表的权威条目。"""

    attributes: list[OperatorAttribute] = Field(min_length=1, description="属性清单，至少一项")
    dialect_constraints: list[str] = Field(
        default_factory=list, description="方言 / 使用约束（如参数唯一性、废弃写法）"
    )
    seatunnel_version: str = Field(default="2.3.13", description="算子来源的 SeaTunnel 版本")
