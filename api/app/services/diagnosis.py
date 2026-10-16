"""可解释根因诊断（M10，FR-37）。

预算监督回答「越界了没有」，诊断回答「为什么会越界、接下来该动什么」。两件事
必须分开：判定是确定性的（``budget.evaluate`` 只看数字，同一份输入永远同一个
结论），而诊断是给人看的解释，措辞会演进、建议会随经验增长。

**为什么要覆盖两种情形而不是只看越界**：还有一种情形同样需要解释——**声明了
却判不了**。声明了放大比预算、而引擎没回传源端字节数，这个维度会被静默跳过；
从外面看它「没有越界」，但它其实「没被检查过」。不把这种情形一并说出来，预算
就成了一种看起来生效、实际不设防的摆设。两类诊断都发生在中断那一刻（见
``seatunnel.governed_poll``），随快照一起冻结。

**诊断随中断事件落库**（并入 ``EVENT_BUDGET_EXCEEDED`` 的快照），而不是查询时
现算：诊断是「当时为什么中断」的结论，若每次查询重算，将来调整这里的一句建议
就会悄悄改写历史运行的结论，与「事件只增不改」冲突。质量报告则相反——它是
「这次运行产出了什么」的事实，本就该独立可查（见 ``app.models.quality``）。
"""

from dataclasses import dataclass
from typing import Any

from app.services.budget import (
    DIMENSION_AMPLIFICATION,
    DIMENSION_BYTES,
    DIMENSION_DURATION,
    DIMENSION_REJECT_RATIO,
    DIMENSION_ROWS,
    BudgetVerdict,
    declared_limit,
)


@dataclass(frozen=True)
class Diagnosis:
    """一条根因诊断。

    ``cause`` 是给机器看的稳定分类（前端据此分组或选图标，不随文案调整而变），
    ``explanation`` 与 ``suggestion`` 给人看——后者必须**可操作**，不能把
    ``headline`` 换个说法再念一遍。
    """

    dimension: str
    cause: str
    explanation: str
    suggestion: str
    observed: float | None = None
    limit: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "cause": self.cause,
            "explanation": self.explanation,
            "suggestion": self.suggestion,
            "observed": self.observed,
            "limit": self.limit,
        }


# 越界维度的诊断模板：根因分类 + 解释 + 建议。
# 建议必须指向「下一步去看什么、动什么」；只复述一遍超了多少，等于没诊断。
_BREACH: dict[str, tuple[str, str, str]] = {
    DIMENSION_ROWS: (
        "source_volume",
        "源端读取行数超过批准上限",
        "核对源表的过滤条件与时间窗口是否与审批时一致；确认无误后按实际量级上调"
        " max_rows 并重新走审批——绕过审批直接改预算会让授权与执行脱节",
    ),
    DIMENSION_BYTES: (
        "data_volume",
        "写入字节数超过批准上限",
        "检查字段映射是否选入了超出预期的宽表列或大文本列；确认属于正常数据增长后"
        "再上调 max_bytes",
    ),
    DIMENSION_DURATION: (
        "runtime",
        "作业耗时超过批准上限",
        "对比历史同作业耗时，判断是数据量增长还是引擎资源不足；两者都排除后再上调"
        " max_duration_seconds",
    ),
    DIMENSION_AMPLIFICATION: (
        "row_amplification",
        "写出量相对读入量的放大比过高",
        "放大比高通常来自关联键不唯一导致的行数膨胀；检查 join 条件与键的唯一性。"
        "不要直接放宽这条预算——放宽等于把一份本就不该发布的宽表放行",
    ),
    DIMENSION_REJECT_RATIO: (
        "data_quality",
        "被质量规则拒绝的行数比例超过批准上限",
        "查看本次运行质量报告里的错误码分布，定位是哪条规则在拒绝、拒的是哪类样本；"
        "规则过严就调规则，源数据劣化就先修源头",
    ),
}


# 「声明了却判不了」——这类不会出现在 breaches 里，因为没判过的维度不会越界。
_UNMEASURED: dict[str, tuple[str, str]] = {
    DIMENSION_AMPLIFICATION: (
        "引擎本轮未回传源端字节数，放大比无法判定",
        "确认数据面是否上报 SourceReceivedBytes；在它可被观测之前，这条预算形同未设"
        "——它既不会报警，也不会拦截",
    ),
    DIMENSION_REJECT_RATIO: (
        "本轮取不到拒绝行数，拒绝率无法判定",
        "拒绝率只有在声明了质量规则时才有意义：没有质量规则就没有错误表，"
        "这条预算永远不会生效",
    ),
}


def diagnose(
    budget: dict[str, Any] | None, verdict: BudgetVerdict
) -> tuple[Diagnosis, ...]:
    """给一次预算判定配上可解释的根因与建议（FR-37）。

    必须同时收下 ``budget`` 与 ``verdict``：判定结论里没有「当初声明了什么」，
    而没有声明就无从区分某个维度是「观测不到」还是「根本没要求观测」——
    把后者也报成「没测到」，等于凭空制造一条不存在的告警。

    是否「已声明」一律走 :func:`app.services.budget.declared_limit`，与判定
    同源。自己另写一套解析，就会出现诊断说「你没声明」而判定说「声明了」的
    局面，而这两句话是同一次中断的结论。
    """
    declared = budget or {}
    found: list[Diagnosis] = []

    for breach in verdict.breaches:
        template = _BREACH.get(breach.dimension)
        if template is None:
            continue
        cause, explanation, suggestion = template
        found.append(
            Diagnosis(
                dimension=breach.dimension,
                cause=cause,
                explanation=explanation,
                suggestion=suggestion,
                observed=breach.observed,
                limit=breach.limit,
            )
        )

    usage = verdict.usage
    unmeasurable = {
        DIMENSION_AMPLIFICATION: usage.read_bytes is None,
        DIMENSION_REJECT_RATIO: usage.rejected_rows is None,
    }
    breached = {breach.dimension for breach in verdict.breaches}
    for dimension, missing in unmeasurable.items():
        # 已经越界的维度不再补一条「没测到」——它测到了，只是超了
        if not missing or dimension in breached:
            continue
        if declared_limit(declared.get(dimension)) is None:
            # 没声明就无所谓「判不了」：这个维度本就不在监督范围内
            continue
        explanation, suggestion = _UNMEASURED[dimension]
        found.append(
            Diagnosis(
                dimension=dimension,
                cause="metric_unavailable",
                explanation=explanation,
                suggestion=suggestion,
            )
        )

    return tuple(found)
