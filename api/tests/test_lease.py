"""租约 fencing、心跳续期、协作式取消与幂等台账测试（FR-27 / FR-29）。

核心断言：**失去租约的 Worker 不得写回终态**——这是「Worker 心跳失败不伪造成功」
在代码层面的落点。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import LeaseLost, ResourceConflict
from app.db.types import as_utc
from app.models.execution import (
    ExecutionRun,
    ExecutionRunStatus,
    RunEvent,
    RunOperation,
    RunOperationStatus,
)
from app.services.execution import (
    EVENT_CANCEL_REQUESTED,
    EVENT_CANCELLED,
    EVENT_CLAIMED,
    EVENT_FAILED,
    OPERATION_SUBMIT_JOB,
    ExecutionOutcome,
    claim_run,
    load_command,
    mark_failed,
    mark_succeeded,
    record_event,
    request_cancel,
    run_execution,
    run_operation,
    submit_job_once,
)
from app.services.lease import assert_lease_held, is_lease_expired, renew_lease
from app.services.redaction import REDACTED


class _Plane:
    """可注入的数据面替身。"""

    def __init__(self, outcome: ExecutionOutcome | None = None, error: Exception | None = None):
        self._outcome = outcome or ExecutionOutcome()
        self._error = error
        self.calls = 0

    def submit(self, command, *, db, on_poll):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._outcome


def _run(db: Session, run_id) -> ExecutionRun:
    db.expire_all()
    return db.get(ExecutionRun, run_id)


def _event_types(db: Session, run_id) -> list[str]:
    return [
        row.event_type
        for row in db.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at)
        )
    ]


def _cancel_from_another_process(session_factory, run_id) -> None:
    """模拟取消请求来自另一个进程：另开一个会话写入。"""
    with session_factory() as other:
        request_cancel(db=other, run=other.get(ExecutionRun, run_id), user_id=uuid.uuid4())


# --------------------------------------------------------------------------
# 心跳续期
# --------------------------------------------------------------------------


def test_renew_lease_extends_the_deadline(session_factory, execution_world):
    worker = uuid.uuid4()
    with session_factory() as db:
        claimed = claim_run(db, run_id=execution_world.run_id, worker_id=worker)
        before = as_utc(claimed.lease_expires_at)

        renewed = renew_lease(db, run_id=execution_world.run_id, worker_id=worker)

        assert as_utc(renewed.lease_expires_at) >= before
        assert renewed.heartbeat_at is not None


def test_renew_by_a_non_owner_is_refused(session_factory, execution_world):
    with session_factory() as db:
        claim_run(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())
        with pytest.raises(LeaseLost):
            renew_lease(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())


def test_renew_after_terminal_state_is_refused(session_factory, execution_world):
    worker = uuid.uuid4()
    with session_factory() as db:
        run = claim_run(db, run_id=execution_world.run_id, worker_id=worker)
        mark_succeeded(db, run, ExecutionOutcome(), worker_id=worker)

        with pytest.raises(LeaseLost):
            renew_lease(db, run_id=execution_world.run_id, worker_id=worker)


# --------------------------------------------------------------------------
# fencing：失去租约不得写回终态
# --------------------------------------------------------------------------


def test_taken_over_run_cannot_be_written_by_the_old_worker(
    session_factory, execution_world
):
    """维护任务把运行交还给队列、新 Worker 认领后，旧 Worker 的写回必须失败。"""
    stale_worker = uuid.uuid4()
    with session_factory() as db:
        run = claim_run(db, run_id=execution_world.run_id, worker_id=stale_worker)

        # 模拟租约回收：运行回到 pending，随后被新 Worker 认领
        run.status = ExecutionRunStatus.PENDING
        run.worker_lease_owner = None
        db.commit()
        new_worker = uuid.uuid4()
        claim_run(db, run_id=execution_world.run_id, worker_id=new_worker)

        with pytest.raises(LeaseLost):
            mark_succeeded(
                db,
                _run(db, execution_world.run_id),
                ExecutionOutcome(read_rows=1),
                worker_id=stale_worker,
            )

        # 新持有者的结论没有被覆盖
        current = _run(db, execution_world.run_id)
        assert current.status is ExecutionRunStatus.RUNNING
        assert current.worker_lease_owner == new_worker


def test_terminal_write_fences_on_owner(session_factory, execution_world):
    with session_factory() as db:
        run = claim_run(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())
        with pytest.raises(LeaseLost):
            mark_failed(db, run, "boom", worker_id=uuid.uuid4())


def test_terminal_write_releases_the_lease(session_factory, execution_world):
    worker = uuid.uuid4()
    with session_factory() as db:
        run = claim_run(db, run_id=execution_world.run_id, worker_id=worker)
        finished = mark_succeeded(db, run, ExecutionOutcome(), worker_id=worker)

        assert finished.worker_lease_owner is None
        assert is_lease_expired(finished)


def test_assert_lease_held_detects_takeover(session_factory, execution_world):
    with session_factory() as db:
        claim_run(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())
        with pytest.raises(LeaseLost):
            assert_lease_held(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())


def test_expired_lease_without_takeover_still_permits_writing(
    session_factory, execution_world
):
    """守卫认的是持有者，不是时钟：只是慢了一点的运行不该被永久卡死。"""
    worker = uuid.uuid4()
    with session_factory() as db:
        run = claim_run(db, run_id=execution_world.run_id, worker_id=worker)
        run.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.commit()

        finished = mark_succeeded(
            db, _run(db, execution_world.run_id), ExecutionOutcome(), worker_id=worker
        )
        assert finished.status is ExecutionRunStatus.SUCCEEDED


def test_fenced_terminal_write_leaves_no_event_behind(session_factory, execution_world):
    """事件是事实的流水：被拒绝的写回不能留下「已成功」的记录。"""
    with session_factory() as db:
        claim_run(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())
        with pytest.raises(LeaseLost):
            mark_succeeded(
                db,
                _run(db, execution_world.run_id),
                ExecutionOutcome(),
                worker_id=uuid.uuid4(),
            )

    with session_factory() as db:
        assert _event_types(db, execution_world.run_id) == []


# --------------------------------------------------------------------------
# 协作式取消
# --------------------------------------------------------------------------


def test_cancel_pending_run_finishes_immediately(session_factory, execution_world):
    """还没开始的运行没有副作用需要收尾，不必协商。"""
    with session_factory() as db:
        cancelled = request_cancel(
            db, run=_run(db, execution_world.run_id), user_id=uuid.uuid4()
        )

        assert cancelled.status is ExecutionRunStatus.CANCELLED
        assert cancelled.cancel_requested is True
        assert cancelled.finished_at is not None
        assert _event_types(db, execution_world.run_id) == [EVENT_CANCELLED]


def test_cancel_running_run_is_cooperative(session_factory, execution_world):
    """运行中的作业由 Worker 在安全点停下，控制面不强杀。"""
    with session_factory() as db:
        claim_run(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())
        requested = request_cancel(
            db, run=_run(db, execution_world.run_id), user_id=uuid.uuid4()
        )

        assert requested.status is ExecutionRunStatus.RUNNING
        assert requested.cancel_requested is True
        assert requested.finished_at is None
        assert _event_types(db, execution_world.run_id)[-1] == EVENT_CANCEL_REQUESTED


def test_cancel_after_terminal_state_is_refused(session_factory, execution_world):
    worker = uuid.uuid4()
    with session_factory() as db:
        run = claim_run(db, run_id=execution_world.run_id, worker_id=worker)
        mark_succeeded(db, run, ExecutionOutcome(), worker_id=worker)

        with pytest.raises(ResourceConflict):
            request_cancel(db, run=_run(db, execution_world.run_id), user_id=uuid.uuid4())


def test_worker_stops_before_touching_the_data_plane(session_factory, execution_world):
    """取消请求在认领之后、副作用之前到达：一份数据都不该被搬。"""
    plane = _Plane()

    def _cancel_while_claiming(event_type: str) -> None:
        if event_type == EVENT_CLAIMED:
            _cancel_from_another_process(session_factory, execution_world.run_id)

    with session_factory() as db:
        run = run_execution(
            db,
            run_id=execution_world.run_id,
            worker_id=uuid.uuid4(),
            plane=plane,
            on_event=_cancel_while_claiming,
        )
        assert run.status is ExecutionRunStatus.CANCELLED

    assert plane.calls == 0


def test_cancel_after_submit_prevents_publishing(session_factory, execution_world):
    """提交之后、写终态之前收到取消：不发布结果，落 cancelled。"""

    class _CancellingPlane(_Plane):
        def submit(self, command, *, db, on_poll):
            outcome = super().submit(command, db=db, on_poll=on_poll)
            _cancel_from_another_process(session_factory, execution_world.run_id)
            return outcome

    with session_factory() as db:
        run_execution(
            db,
            run_id=execution_world.run_id,
            worker_id=uuid.uuid4(),
            plane=_CancellingPlane(ExecutionOutcome(read_rows=99)),
        )

    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        assert run.status is ExecutionRunStatus.CANCELLED
        # 被取消的运行不落度量：它没有产出可发布的结果
        assert run.read_rows == 0


# --------------------------------------------------------------------------
# 幂等台账
# --------------------------------------------------------------------------


def test_operation_runs_at_most_once(session_factory, execution_world):
    calls: list[int] = []

    with session_factory() as db:
        first = run_operation(
            db,
            run_id=execution_world.run_id,
            operation_key=OPERATION_SUBMIT_JOB,
            action=lambda: (calls.append(1), {"value": 1})[1],
        )
        second = run_operation(
            db,
            run_id=execution_world.run_id,
            operation_key=OPERATION_SUBMIT_JOB,
            action=lambda: (calls.append(2), {"value": 2})[1],
        )

    assert first.executed is True
    assert second.executed is False
    assert calls == [1]
    assert second.status is RunOperationStatus.SUCCEEDED


def test_successful_operation_is_resumable_after_a_crash(
    session_factory, execution_world
):
    """副作用已发生但终态没写上：下一轮沿用当时的投影收尾，不重做副作用。"""
    calls: list[int] = []

    with session_factory() as db:
        run_operation(
            db,
            run_id=execution_world.run_id,
            operation_key=OPERATION_SUBMIT_JOB,
            action=lambda: (calls.append(1), {"read_rows": 7})[1],
        )

    with session_factory() as db:
        later = run_operation(
            db,
            run_id=execution_world.run_id,
            operation_key=OPERATION_SUBMIT_JOB,
            action=lambda: (calls.append(2), {"read_rows": 999})[1],
        )

    assert calls == [1]
    assert later.executed is False
    assert later.value == {"read_rows": 7}


def test_failed_operation_is_not_retried(session_factory, execution_world):
    """「已尝试且失败」不再自动重放：要不要重来取决于目标端残留，需人判断。"""
    calls: list[int] = []

    def _boom() -> dict:
        calls.append(1)
        raise RuntimeError("提交失败")

    with session_factory() as db:
        with pytest.raises(RuntimeError):
            run_operation(
                db,
                run_id=execution_world.run_id,
                operation_key=OPERATION_SUBMIT_JOB,
                action=_boom,
            )

    with session_factory() as db:
        later = run_operation(
            db,
            run_id=execution_world.run_id,
            operation_key=OPERATION_SUBMIT_JOB,
            action=lambda: (calls.append(2), {"read_rows": 1})[1],
        )

    assert calls == [1]
    assert later.executed is False
    assert later.status is RunOperationStatus.FAILED
    assert later.value is None


def test_operation_projection_is_redacted(session_factory, execution_world):
    with session_factory() as db:
        run_operation(
            db,
            run_id=execution_world.run_id,
            operation_key="probe",
            action=lambda: {"password": "p@ss", "rows": 3},
        )

    with session_factory() as db:
        stored = db.scalar(select(RunOperation).where(RunOperation.operation_key == "probe"))

    assert stored.result["value"]["password"] == REDACTED
    assert stored.result["value"]["rows"] == 3


def test_second_submission_is_not_replayed(session_factory, execution_world):
    """数据面提交只发生一次——重复提交一次全量作业的代价远高于一次失败运行。"""
    first_plane = _Plane(ExecutionOutcome(read_rows=5))
    second_plane = _Plane()

    with session_factory() as db:
        command = load_command(db, execution_world.run_id)
        first = submit_job_once(
            db, run_id=execution_world.run_id, command=command, plane=first_plane
        )
        again = submit_job_once(
            db, run_id=execution_world.run_id, command=command, plane=second_plane
        )

    assert first_plane.calls == 1
    assert second_plane.calls == 0
    assert first is not None and first.read_rows == 5
    assert again is not None and again.read_rows == 5


def test_full_run_uses_the_operation_ledger(session_factory, execution_world):
    plane = _Plane(ExecutionOutcome(read_rows=5))

    with session_factory() as db:
        run = run_execution(
            db, run_id=execution_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )
        assert run.status is ExecutionRunStatus.SUCCEEDED

    with session_factory() as db:
        ledger = db.scalar(
            select(RunOperation).where(RunOperation.operation_key == OPERATION_SUBMIT_JOB)
        )
    assert ledger.status is RunOperationStatus.SUCCEEDED


# --------------------------------------------------------------------------
# 失败与事件
# --------------------------------------------------------------------------


def test_failure_event_carries_the_category_message(session_factory, execution_world):
    worker = uuid.uuid4()
    with session_factory() as db:
        run = claim_run(db, run_id=execution_world.run_id, worker_id=worker)
        mark_failed(db, run, "SYS_DATAPLANE_UNAVAILABLE: 引擎不可达", worker_id=worker)

        assert _event_types(db, execution_world.run_id) == [EVENT_FAILED]


def test_record_event_is_append_only(session_factory, execution_world):
    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        record_event(db, run, "run.progress", metrics={"read_rows": 1})
        record_event(db, run, "run.progress", metrics={"read_rows": 2})

        rows = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run.id)))

    assert len(rows) == 2
    assert {row.metrics["read_rows"] for row in rows} == {1, 2}


@pytest.fixture(autouse=True)
def _isolate_data_plane():
    """默认适配器是进程级单例，测试不得把替身留在里面。"""
    from app.services import execution

    original = execution.get_data_plane()
    yield
    execution.set_data_plane(original)
