"""T-16：质量报告落成可查询的事实（FR-33 / FR-40）。

报告此前只活在 ``EVENT_PROGRESS`` 的事件快照里——要回答「这次运行分流了多少行」
得先按事件类型过滤、再解析一段 JSON。这里测的是它作为**事实**的部分：能被按
运行查到、能被运行详情一次取全，以及**没有报告时不会凭空伪造出一份「零拒绝」**。
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.execution import ExecutionRun
from app.models.quality import QualityReport
from app.schemas.execution import ExecutionRunOut
from app.services.execution import (
    ExecutionOutcome,
    claim_run,
    mark_succeeded,
    record_quality_report,
)


def _outcome(*, with_report: bool) -> ExecutionOutcome:
    detail: dict[str, Any] = {"job_status": "FINISHED"}
    if with_report:
        detail["quality_report"] = {
            "source_rows": 8,
            "accepted_rows": 7,
            "rejected_rows": 1,
            "rejected_ratio": 0.125,
            "error_table": "orders_wide__shadow_v1__errors",
            "error_dist": {"USER_NAME_REGEX": 1},
        }
    return ExecutionOutcome(
        read_rows=8,
        written_bytes=64,
        rejected_rows=1 if with_report else 0,
        published=True,
        detail=detail,
    )


def _report_of(db: Session, run_id: uuid.UUID) -> QualityReport | None:
    return db.scalar(select(QualityReport).where(QualityReport.run_id == run_id))


def test_a_report_becomes_a_queryable_row(session_factory, execution_world):
    with session_factory() as db:
        run = db.get(ExecutionRun, execution_world.run_id)
        recorded = record_quality_report(db, run, _outcome(with_report=True))

    assert recorded is not None
    with session_factory() as db:
        stored = _report_of(db, execution_world.run_id)
        assert stored is not None
        assert (stored.source_rows, stored.valid_rows, stored.rejected_rows) == (8, 7, 1)
        assert stored.error_dist == {"USER_NAME_REGEX": 1}
        assert stored.error_table == "orders_wide__shadow_v1__errors"
        # 拒绝率是派生值：它与那两个行数必须是同一个数字
        assert stored.rejected_ratio == 0.125


def test_a_run_without_a_quality_contract_records_nothing(
    session_factory, execution_world
):
    """**「没有报告」不能与「报告显示零拒绝」混为一谈。**

    前者是这次运行压根没声明质量规则；后者是规则确实跑过并且全部通过。
    把两者都落成一份 ``rejected_rows=0`` 的报告，等于把「没检查」说成「检查通过」。
    """
    with session_factory() as db:
        run = db.get(ExecutionRun, execution_world.run_id)
        assert record_quality_report(db, run, _outcome(with_report=False)) is None

    with session_factory() as db:
        assert db.scalar(select(QualityReport.id)) is None


def test_recording_twice_does_not_duplicate(session_factory, execution_world):
    with session_factory() as db:
        run = db.get(ExecutionRun, execution_world.run_id)
        first = record_quality_report(db, run, _outcome(with_report=True))
        second = record_quality_report(db, run, _outcome(with_report=True))

    assert first is not None and second is None
    with session_factory() as db:
        assert len(list(db.scalars(select(QualityReport)))) == 1


def test_mark_succeeded_persists_the_report(session_factory, execution_world):
    """成功路径自己就会落报告——不指望调用方额外记得这件事。"""
    worker = uuid.uuid4()
    with session_factory() as db:
        claim_run(db, run_id=execution_world.run_id, worker_id=worker)
        run = db.get(ExecutionRun, execution_world.run_id)
        mark_succeeded(db, run, _outcome(with_report=True), worker_id=worker)

    with session_factory() as db:
        assert _report_of(db, execution_world.run_id) is not None


def test_run_out_carries_the_report(session_factory, execution_world):
    """运行详情一次取全：前端不该为了拿拒绝明细再去翻事件流。"""
    with session_factory() as db:
        run = db.get(ExecutionRun, execution_world.run_id)
        record_quality_report(db, run, _outcome(with_report=True))
        db.refresh(run)
        out = ExecutionRunOut.model_validate(run)

    assert out.quality_report is not None
    assert out.quality_report.valid_rows == 7
    assert out.quality_report.rejected_ratio == 0.125
    assert out.quality_report.error_dist == {"USER_NAME_REGEX": 1}


def test_run_out_carries_no_report_when_there_is_none(session_factory, execution_world):
    with session_factory() as db:
        run = db.get(ExecutionRun, execution_world.run_id)
        out = ExecutionRunOut.model_validate(run)

    assert out.quality_report is None
