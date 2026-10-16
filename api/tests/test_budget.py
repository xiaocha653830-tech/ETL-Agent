"""T-17：运行时预算判定的纯函数契约（FR-35 / FR-36）。

这一层没有替身、没有数据库、没有时钟——判据本身必须能被单独钉死，
否则「为什么这个作业被中断了」就永远只能去日志里猜。
"""

import json

import pytest

from app.services.budget import (
    DIMENSION_AMPLIFICATION,
    DIMENSION_BYTES,
    DIMENSION_DURATION,
    DIMENSION_REJECT_RATIO,
    DIMENSION_ROWS,
    BudgetUsage,
    evaluate,
)


def test_undeclared_dimension_is_skipped_not_treated_as_zero():
    """只声明了行数上限时，天文数字的字节、时长、拒绝行数都不该触发中断。

    把「没声明上限」读成「上限为 0」，会让每一个真实作业在第一次轮询时就被误杀。
    """
    verdict = evaluate(
        {"max_rows": 1000},
        BudgetUsage(
            read_rows=10,
            written_bytes=10**12,
            read_bytes=1,
            rejected_rows=10**6,
            elapsed_seconds=10**6,
        ),
    )
    assert verdict.exceeded is False
    assert verdict.headline == ""


@pytest.mark.parametrize(
    ("budget", "usage", "dimension"),
    [
        ({"max_rows": 100}, BudgetUsage(read_rows=101), DIMENSION_ROWS),
        ({"max_bytes": 100}, BudgetUsage(written_bytes=101), DIMENSION_BYTES),
        (
            {"max_duration_seconds": 10},
            BudgetUsage(elapsed_seconds=10.5),
            DIMENSION_DURATION,
        ),
        (
            {"max_amplification_ratio": 2.0},
            BudgetUsage(read_bytes=100, written_bytes=201),
            DIMENSION_AMPLIFICATION,
        ),
        (
            {"max_reject_ratio": 0.2},
            BudgetUsage(read_rows=100, rejected_rows=21),
            DIMENSION_REJECT_RATIO,
        ),
    ],
)
def test_each_dimension_breaches_when_observed_exceeds_limit(budget, usage, dimension):
    verdict = evaluate(budget, usage)
    assert [breach.dimension for breach in verdict.breaches] == [dimension]


@pytest.mark.parametrize(
    ("budget", "usage"),
    [
        ({"max_rows": 100}, BudgetUsage(read_rows=100)),
        ({"max_bytes": 100}, BudgetUsage(written_bytes=100)),
        ({"max_duration_seconds": 10}, BudgetUsage(elapsed_seconds=10.0)),
        (
            {"max_amplification_ratio": 2.0},
            BudgetUsage(read_bytes=100, written_bytes=200),
        ),
        ({"max_reject_ratio": 0.2}, BudgetUsage(read_rows=100, rejected_rows=20)),
    ],
)
def test_using_the_budget_to_the_limit_is_not_a_breach(budget, usage):
    """用满是「在批准范围内」，不是「超出批准范围」。

    判据必须与发布前的拒绝率把关（``ratio > limit``）一致，否则同一个拒绝率
    会在运行中和发布前得到两个互相矛盾的结论。
    """
    assert evaluate(budget, usage).exceeded is False


def test_unmeasurable_ratio_dimensions_are_skipped():
    """量不出来时不判定：``None``（不知道）不是 ``0``（观测到没有）。"""
    verdict = evaluate(
        {"max_reject_ratio": 0.0, "max_amplification_ratio": 0.0},
        BudgetUsage(read_rows=0, rejected_rows=None, read_bytes=None),
    )
    assert verdict.exceeded is False
    assert verdict.usage.rejected_rows is None


def test_declared_zero_limit_still_bites_when_measurable():
    """上限 0 是有效声明（一行都不许错），只在真的量得出来时才生效。"""
    assert (
        evaluate(
            {"max_reject_ratio": 0.0}, BudgetUsage(read_rows=10, rejected_rows=1)
        ).exceeded
        is True
    )


@pytest.mark.parametrize(
    "declared",
    [
        {"max_rows": "100"},
        {"max_rows": None},
        {"max_rows": True},  # bool 是 int 的子类，误填 true 不能变成「上限 1 行」
        {"max_rows": -5},
        {"max_rows": float("nan")},
        {"max_rows": float("inf")},
    ],
)
def test_malformed_limit_is_treated_as_undeclared(declared):
    assert evaluate(declared, BudgetUsage(read_rows=5)).exceeded is False


@pytest.mark.parametrize("declared", [None, {}])
def test_absent_budget_never_interrupts(declared):
    verdict = evaluate(declared, BudgetUsage(read_rows=10**9, elapsed_seconds=10**9))
    assert verdict.exceeded is False


def test_snapshot_names_every_breach_and_stays_serializable():
    verdict = evaluate(
        {"max_rows": 5, "max_duration_seconds": 1},
        BudgetUsage(read_rows=9, written_bytes=48, elapsed_seconds=7.25),
    )
    snapshot = verdict.as_snapshot()
    assert [item["dimension"] for item in snapshot["breaches"]] == [
        DIMENSION_ROWS,
        DIMENSION_DURATION,
    ]
    assert snapshot["observed"] == {
        "read_rows": 9,
        "written_bytes": 48,
        "read_bytes": None,
        "rejected_rows": None,
        "elapsed_seconds": 7.25,
    }
    json.dumps(snapshot)  # 快照要落库并对外展示，必须可序列化


def test_headline_is_readable_and_lists_all_breaches():
    verdict = evaluate(
        {"max_rows": 5, "max_reject_ratio": 0.1},
        BudgetUsage(read_rows=1000, rejected_rows=500),
    )
    headline = verdict.headline
    assert "max_rows 观测 1,000 超过上限 5" in headline
    assert "max_reject_ratio 观测 0.5000 超过上限 0.1000" in headline
