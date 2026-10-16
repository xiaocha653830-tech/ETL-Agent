"""T-17：运行时预算监督（FR-35）。

**监督必须发生在作业运行期间**。预算在审批阶段被声明并写进准备单，但声明的价值
取决于有人盯着它：只做「跑完之后核对一次」等于没有监督——一个把 5 万行上限跑到
500 万行的作业会完整跑完、把目标端写满，然后才被告知超了。

**判定是纯函数**。:func:`evaluate` 不读库、不联网、不看时钟，只接收一份
:class:`BudgetUsage` 观测值：同一组输入永远给出同一结论。超预算硬中断会终止一个
正在搬运数据的作业，这个决定必须可复现、可测试、可申诉。

**维度（FR-35）**：行数、字节、时长、放大比、拒绝率，共五项。

**未声明的维度一律跳过，不按 0 处理。** 把「没声明上限」当成「上限为 0」，
会让每一个真实作业在第一次轮询时就被误杀——这是这套判据里最容易写错的一处。
"""

import math
from dataclasses import dataclass
from typing import Any

# 维度名与准备单 budget 字典的键一一对应：快照里因此不需要再做一次翻译，
# 读快照的人能直接和审批时看到的那份声明对上。
DIMENSION_ROWS = "max_rows"
DIMENSION_BYTES = "max_bytes"
DIMENSION_DURATION = "max_duration_seconds"
DIMENSION_AMPLIFICATION = "max_amplification_ratio"
DIMENSION_REJECT_RATIO = "max_reject_ratio"


@dataclass(frozen=True)
class BudgetUsage:
    """监督时刻的观测值。

    ``None`` 表示**该维度本轮不可判定**（引擎没回传、探针失败、分母为零），
    与 ``0`` 是两件事：``0`` 是「观测到没有」，``None`` 是「不知道」。
    把两者混为一谈，超预算就会以「看起来一切正常」的方式漏过去。
    """

    read_rows: int = 0
    written_bytes: int = 0
    read_bytes: int | None = None
    rejected_rows: int | None = None
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class BudgetBreach:
    """单项维度的越界事实。"""

    dimension: str
    observed: float
    limit: float


def _readable(value: float) -> str:
    """把数值渲染成适合人读的形式：整数不带小数位，比值保留四位。"""
    return f"{int(value):,}" if float(value).is_integer() else f"{value:.4f}"


def declared_limit(value: Any) -> float | None:
    """把预算项解析成可比的上限；无法解析的一律视为「未声明」。

    布尔值必须被显式排除：``True`` 在 Python 里就是 ``1``，一个误填的
    ``true`` 会变成「上限 1 行」这种荒唐却完全合法的判据。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _ratio(numerator: int | None, denominator: int | None) -> float | None:
    """比值维度：任一侧不可判定或分母非正时返回 ``None``（跳过该维度）。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


@dataclass(frozen=True)
class BudgetVerdict:
    """一次观测的判定结论。"""

    breaches: tuple[BudgetBreach, ...]
    usage: BudgetUsage

    @property
    def exceeded(self) -> bool:
        return bool(self.breaches)

    @property
    def headline(self) -> str:
        """一行可读结论，用于运行终态文案与事件消息。"""
        return "；".join(
            f"{breach.dimension} 观测 {_readable(breach.observed)}"
            f" 超过上限 {_readable(breach.limit)}"
            for breach in self.breaches
        )

    def as_snapshot(self) -> dict[str, Any]:
        """失败快照（FR-36）。

        **只记数字，不记数据行**：快照会被长期保存并对外展示，它的职责是回答
        「中断那一刻越到了什么程度」，而不是把业务数据复制一份进证据链。
        原始行留在错误表里，那才是它们该待的地方。
        """
        return {
            "breaches": [
                {
                    "dimension": breach.dimension,
                    "observed": breach.observed,
                    "limit": breach.limit,
                }
                for breach in self.breaches
            ],
            "observed": {
                "read_rows": self.usage.read_rows,
                "written_bytes": self.usage.written_bytes,
                "read_bytes": self.usage.read_bytes,
                "rejected_rows": self.usage.rejected_rows,
                "elapsed_seconds": round(float(self.usage.elapsed_seconds), 3),
            },
        }


def evaluate(budget: dict[str, Any] | None, usage: BudgetUsage) -> BudgetVerdict:
    """判定一次观测是否越过批准预算（FR-35）。

    **严格大于才算越界**：``observed == limit`` 是「用满」，不是「超了」。
    这与发布前的拒绝率把关（``quality.DorisPublisher.report`` 的
    ``ratio > limit``）保持同一判据——否则同一个拒绝率会在运行中和发布前
    得到两个互相矛盾的结论。
    """
    declared = budget or {}
    candidates: tuple[tuple[str, float | None], ...] = (
        (DIMENSION_ROWS, float(usage.read_rows)),
        (DIMENSION_BYTES, float(usage.written_bytes)),
        (DIMENSION_DURATION, max(0.0, float(usage.elapsed_seconds))),
        (DIMENSION_AMPLIFICATION, _ratio(usage.written_bytes, usage.read_bytes)),
        (DIMENSION_REJECT_RATIO, _ratio(usage.rejected_rows, usage.read_rows)),
    )

    breaches: list[BudgetBreach] = []
    for dimension, observed in candidates:
        limit = declared_limit(declared.get(dimension))
        if limit is None or observed is None:
            continue
        if observed > limit:
            breaches.append(
                BudgetBreach(dimension=dimension, observed=observed, limit=limit)
            )
    return BudgetVerdict(breaches=tuple(breaches), usage=usage)
