"""T-17：可解释根因诊断（FR-37）。

诊断不是判定的复述。判定回答「超没超」，诊断回答「为什么、接下来动什么」——
所以这里测的不是「有没有报出那个维度」，而是**报出来的东西能不能指导下一步**，
以及**什么时候必须闭嘴**：没声明上限的维度量不出来，那不是告警，是噪音；
把「没要求监控」报成「监控失效」，每次运行都会凭空多出一条待办。
"""

import json

from app.services.budget import (
    DIMENSION_AMPLIFICATION,
    DIMENSION_REJECT_RATIO,
    DIMENSION_ROWS,
    BudgetUsage,
    evaluate,
)
from app.services.diagnosis import diagnose


def test_a_breach_is_explained_with_an_actionable_suggestion():
    budget = {"max_rows": 5}
    verdict = evaluate(budget, BudgetUsage(read_rows=9))

    (item,) = diagnose(budget, verdict)
    assert item.dimension == DIMENSION_ROWS
    assert item.cause == "source_volume"
    assert item.observed == 9 and item.limit == 5
    # 建议必须指向下一步动作，而不是把 headline 换个说法念一遍
    assert "审批" in item.suggestion


def test_every_dimension_has_its_own_cause():
    """五个维度各有各的根因，不能共用一句万能文案。"""
    cases = [
        ({"max_rows": 1}, BudgetUsage(read_rows=2), "source_volume"),
        ({"max_bytes": 1}, BudgetUsage(written_bytes=2), "data_volume"),
        ({"max_duration_seconds": 1}, BudgetUsage(elapsed_seconds=2), "runtime"),
        (
            {"max_amplification_ratio": 1.0},
            BudgetUsage(read_bytes=1, written_bytes=2),
            "row_amplification",
        ),
        (
            {"max_reject_ratio": 0.1},
            BudgetUsage(read_rows=10, rejected_rows=5),
            "data_quality",
        ),
    ]
    causes = set()
    for budget, usage, expected in cases:
        (item,) = diagnose(budget, evaluate(budget, usage))
        assert item.cause == expected
        causes.add(item.cause)
    assert len(causes) == len(cases)


def test_a_clean_run_yields_no_diagnosis():
    budget = {"max_rows": 100}
    assert diagnose(budget, evaluate(budget, BudgetUsage(read_rows=1))) == ()


def test_declared_but_unmeasurable_amplification_is_reported():
    """声明了放大比却量不出源端字节数：这次运行「没被检查过」，必须说出来。

    它在 ``breaches`` 里永远不出现——没判过的维度不会越界。只看 ``breaches``
    的实现会把这种「预算形同未设」的状态报成一切正常。
    """
    budget = {"max_amplification_ratio": 2.0}
    verdict = evaluate(budget, BudgetUsage(read_rows=10, read_bytes=None))

    (item,) = diagnose(budget, verdict)
    assert item.dimension == DIMENSION_AMPLIFICATION
    assert item.cause == "metric_unavailable"
    assert item.observed is None and item.limit is None


def test_declared_reject_ratio_without_quality_rules_is_reported():
    """没有质量规则就没有错误表，拒绝率预算永远不会生效。"""
    budget = {"max_reject_ratio": 0.2}
    verdict = evaluate(budget, BudgetUsage(read_rows=10, rejected_rows=None))

    (item,) = diagnose(budget, verdict)
    assert item.dimension == DIMENSION_REJECT_RATIO
    assert item.cause == "metric_unavailable"


def test_an_undeclared_dimension_is_not_reported_as_unmeasured():
    verdict = evaluate({}, BudgetUsage(read_rows=10, read_bytes=None, rejected_rows=None))
    assert diagnose({}, verdict) == ()


def test_a_breached_dimension_is_not_also_reported_as_unmeasured():
    """一个维度要么「超了」要么「没测到」，不能两条都报。"""
    budget = {"max_amplification_ratio": 2.0, "max_rows": 5}
    verdict = evaluate(budget, BudgetUsage(read_rows=9, read_bytes=None))

    items = diagnose(budget, verdict)
    assert [item.dimension for item in items] == [DIMENSION_ROWS, DIMENSION_AMPLIFICATION]
    assert {item.cause for item in items} == {"source_volume", "metric_unavailable"}


def test_malformed_limit_is_not_treated_as_declared():
    """畸形上限（``True`` 在 Python 里就是 1）不算声明，否则会凭空造出告警。"""
    budget = {"max_amplification_ratio": True}
    assert diagnose(budget, evaluate(budget, BudgetUsage(read_bytes=None))) == ()


def test_diagnosis_is_serializable():
    """诊断要并进失败快照落库，必须可序列化。"""
    budget = {"max_bytes": 10, "max_reject_ratio": 0.1}
    verdict = evaluate(
        budget, BudgetUsage(written_bytes=99, read_rows=10, rejected_rows=None)
    )
    payload = [item.as_dict() for item in diagnose(budget, verdict)]
    json.dumps(payload, ensure_ascii=False)
    assert {item["cause"] for item in payload} == {"data_volume", "metric_unavailable"}
