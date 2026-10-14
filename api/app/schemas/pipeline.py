"""需求生成契约（FR-08 ~ FR-14）。

四类产物对应 PRD 第 7 章 P06「Studio 方案审查」页的四个 Tab：
结构化方案（EtlPlan）/ 可执行配置 / DAG 拓扑 / 字段映射。

**算子约束（FR-11 / FR-14）**：``OperatorStep.operator_id`` 必须存在于
M4 受管注册表（``app/registry/catalog.py``）。模型只能从这个清单里选，
选不中就要返回「不支持」并请求澄清，**不得臆造组件**。
"""

import enum
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class OperatorRole(enum.StrEnum):
    SOURCE = "source"
    TRANSFORM = "transform"
    SINK = "sink"


class OperatorStep(BaseModel):
    """方案中的一个算子实例。"""

    step_id: str = Field(min_length=1, max_length=64)
    # 必须是 M4 注册表里的标识，如 seatunnel.source.Jdbc
    operator_id: str = Field(min_length=1, max_length=128)
    role: OperatorRole
    attributes: dict[str, Any] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=200)


class FieldMapping(BaseModel):
    source_field: str
    target_field: str
    transform: str | None = Field(default=None, max_length=512)
    # 携带探查阶段定出的敏感级别，供审批环节直接引用（PRD 第 7 章 P06）
    sensitivity_level: str | None = Field(default=None, pattern=r"^L[1-4]$")


class QualityRule(BaseModel):
    """质量契约（FR-32）。

    **由数据工程师显式声明，不由模型生成** —— 因此这是方案里的一个空槽位，
    生成阶段只做结构性占位，取值必须人工填写。
    """

    field: str
    rule: Literal["not_null", "range", "enum", "regex"]
    params: dict[str, Any] = Field(default_factory=dict)
    error_code: str = Field(max_length=64)
    action: Literal["reject", "warn"] = "reject"


class DagNode(BaseModel):
    id: str
    label: str
    kind: OperatorRole


class DagEdge(BaseModel):
    source: str
    target: str


class DagView(BaseModel):
    nodes: list[DagNode]
    edges: list[DagEdge]


class EtlPlan(BaseModel):
    """结构化作业方案（FR-12）。"""

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=1000)
    source_datasource_id: uuid.UUID
    target_datasource_id: uuid.UUID
    source_table: str
    target_table: str
    steps: list[OperatorStep] = Field(min_length=1)
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    quality_rules: list[QualityRule] = Field(default_factory=list)

    def to_dag(self) -> DagView:
        """由算子链推导 DAG —— 本阶段为线性链，后续支持分支时在此扩展。"""
        nodes = [
            DagNode(id=step.step_id, label=step.operator_id, kind=step.role)
            for step in self.steps
        ]
        edges = [
            DagEdge(source=self.steps[i].step_id, target=self.steps[i + 1].step_id)
            for i in range(len(self.steps) - 1)
        ]
        return DagView(nodes=nodes, edges=edges)


class ConfigCandidate(BaseModel):
    """可执行配置候选（FR-13）。"""

    format: Literal["hocon"] = "hocon"
    content: str
    seatunnel_version: str = "2.3.13"


class ClarificationQuestion(BaseModel):
    """澄清问题（FR-09）。"""

    key: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=500)
    hint: str | None = Field(default=None, max_length=500)
    options: list[str] = Field(default_factory=list)


class GenerationStatus(enum.StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    INTERRUPTED = "interrupted"
    # 受管算子清单无法实现该需求（FR-14）——这是业务结论，不是系统故障
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class GenerateRequest(BaseModel):
    """API-03 提交自然语言需求。"""

    project_id: uuid.UUID
    requirement: str = Field(min_length=1, max_length=4000)
    source_datasource_id: uuid.UUID
    target_datasource_id: uuid.UUID


class ResumeRequest(BaseModel):
    """API-04 补充澄清信息后从断点继续。"""

    answers: dict[str, Any] = Field(default_factory=dict)


class GenerationResult(BaseModel):
    """生成结果 —— 三种状态共用同一响应体，前端按 ``status`` 分支。"""

    model_config = ConfigDict(from_attributes=True)

    run_id: uuid.UUID
    status: GenerationStatus
    plan: EtlPlan | None = None
    config: ConfigCandidate | None = None
    dag: DagView | None = None
    questions: list[ClarificationQuestion] = Field(default_factory=list)
    # 非致命问题（如某字段未识别、某敏感字段未脱敏），供 P06 页的「幻觉防护提示」
    warnings: list[str] = Field(default_factory=list)
    # FR-14：受管算子无法实现时的具体原因，前端据此提示「不支持」而非「失败」
    unsupported_reason: str | None = None
    error: str | None = None


class PipelineRunOut(BaseModel):
    """运行记录（列表与详情）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    requirement: str
    status: str
    created_at: Any
