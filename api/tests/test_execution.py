"""受管执行的认领与推进测试（FR-29 / FR-30）。"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import DataplaneUnavailable, ResourceNotFound
from app.db.types import as_utc
from app.models.execution import ExecutionRun, ExecutionRunStatus, RunEvent
from app.services.execution import (
    EVENT_CLAIMED,
    EVENT_FAILED,
    EVENT_PROGRESS,
    EVENT_SUBMITTED,
    EVENT_SUCCEEDED,
    ExecutionOutcome,
    claim_run,
    load_command,
    record_event,
    run_execution,
)
from app.services.redaction import REDACTED


def _run(db: Session, run_id) -> ExecutionRun:
    return db.get(ExecutionRun, run_id)


def _events(db: Session, run_id) -> list[RunEvent]:
    return list(
        db.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at)
        )
    )


# --------------------------------------------------------------------------
# 认领
# --------------------------------------------------------------------------


def test_claim_moves_run_to_running_with_a_lease(session_factory, execution_world):
    worker = uuid.uuid4()
    with session_factory() as db:
        run = claim_run(db, run_id=execution_world.run_id, worker_id=worker)

        assert run is not None
        assert run.status is ExecutionRunStatus.RUNNING
        assert run.worker_lease_owner == worker
        assert run.started_at is not None
        assert as_utc(run.lease_expires_at) > as_utc(run.started_at)


def test_only_one_worker_wins_the_claim(session_factory, execution_world):
    """重复投递是常态：认领用条件更新把它吸收在入口。"""
    with session_factory() as db:
        first = claim_run(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())
        second = claim_run(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())

        assert first is not None
        assert second is None
        # 输的一方不得改写租约持有者
        assert _run(db, execution_world.run_id).worker_lease_owner == first.worker_lease_owner


def test_claim_on_terminal_run_returns_none(session_factory, execution_world):
    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        run.status = ExecutionRunStatus.SUCCEEDED
        db.commit()

        assert claim_run(db, run_id=execution_world.run_id, worker_id=uuid.uuid4()) is None


def test_run_execution_skips_when_already_claimed(session_factory, execution_world):
    """已经被别人认领的运行，本次调用应当安静退出而不是报错。"""
    with session_factory() as db:
        assert run_execution(
            db, run_id=execution_world.run_id, worker_id=uuid.uuid4()
        ) is not None
        assert (
            run_execution(db, run_id=execution_world.run_id, worker_id=uuid.uuid4()) is None
        )


# --------------------------------------------------------------------------
# 执行推进
# --------------------------------------------------------------------------


class _FakePlane:
    def __init__(self, outcome: ExecutionOutcome | None = None, error: Exception | None = None):
        self._outcome = outcome or ExecutionOutcome()
        self._error = error
        self.commands: list = []

    def submit(self, command, *, db, on_poll):
        self.commands.append(command)
        if self._error is not None:
            raise self._error
        return self._outcome


def test_run_execution_succeeds_and_records_metrics(session_factory, execution_world):
    plane = _FakePlane(ExecutionOutcome(read_rows=120, written_bytes=4096, rejected_rows=3))

    with session_factory() as db:
        run = run_execution(
            db, run_id=execution_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )

        assert run.status is ExecutionRunStatus.SUCCEEDED
        assert (run.read_rows, run.written_bytes, run.rejected_rows) == (120, 4096, 3)
        assert run.finished_at is not None

        types = [event.event_type for event in _events(db, execution_world.run_id)]
        # 终态事件之后是度量事件：状态是快照，度量是流水里的一个点，两者分开记
        assert types == [EVENT_CLAIMED, EVENT_SUBMITTED, EVENT_SUCCEEDED, EVENT_PROGRESS]

        metrics = _events(db, execution_world.run_id)[-1].metrics
        assert metrics["read_rows"] == 120


def test_run_execution_passes_the_bound_artifact_to_the_plane(
    session_factory, execution_world
):
    """执行层拿到的必须是受审的那份制品。"""
    plane = _FakePlane()
    with session_factory() as db:
        run_execution(db, run_id=execution_world.run_id, worker_id=uuid.uuid4(), plane=plane)

    assert len(plane.commands) == 1
    assert plane.commands[0].artifact == execution_world.command.artifact
    assert plane.commands[0].capability_token == execution_world.command.capability_token


def test_unavailable_data_plane_fails_the_run(session_factory, execution_world):
    """数据面没接通就必须表现为失败，而不是空跑成功。"""
    with session_factory() as db:
        run = run_execution(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())

        assert run.status is ExecutionRunStatus.FAILED
        assert "SYS_DATAPLANE_UNAVAILABLE" in run.error
        assert [e.event_type for e in _events(db, execution_world.run_id)][-1] == EVENT_FAILED


def test_explicit_dataplane_error_is_recorded_as_category(session_factory, execution_world):
    plane = _FakePlane(error=DataplaneUnavailable("引擎拒绝连接"))
    with session_factory() as db:
        run = run_execution(
            db, run_id=execution_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )
        assert run.status is ExecutionRunStatus.FAILED
        assert "SYS_DATAPLANE_UNAVAILABLE" in run.error


def test_unexpected_error_is_converged_without_leaking_details(
    session_factory, execution_world
):
    """任何异常都必须收敛为终态——running 悬挂会让前端永远轮询不到结果。"""
    plane = _FakePlane(error=RuntimeError("内部细节不该外泄"))

    with session_factory() as db:
        run = run_execution(
            db, run_id=execution_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )

        assert run.status is ExecutionRunStatus.FAILED
        assert "内部细节" not in (run.error or "")
        events = _events(db, execution_world.run_id)
        assert all("内部细节" not in (event.message or "") for event in events)


def test_run_execution_reports_claim_event(session_factory, execution_world):
    seen: list[str] = []
    with session_factory() as db:
        run_execution(
            db,
            run_id=execution_world.run_id,
            worker_id=uuid.uuid4(),
            plane=_FakePlane(),
            on_event=seen.append,
        )
    assert seen == [EVENT_CLAIMED, EVENT_SUBMITTED]


def test_load_command_reads_the_outbox_payload(session_factory, execution_world):
    with session_factory() as db:
        command = load_command(db, execution_world.run_id)

    assert command.run_id == execution_world.run_id
    assert command.tool == execution_world.command.tool
    assert command.shadow_table == execution_world.command.shadow_table


def test_load_command_without_event_is_not_found(session_factory, execution_world):
    with session_factory() as db:
        with pytest.raises(ResourceNotFound):
            load_command(db, uuid.uuid4())


# --------------------------------------------------------------------------
# 事件时间线
# --------------------------------------------------------------------------


def test_event_snapshots_are_redacted_before_persisting(session_factory, execution_world):
    """快照是长期档案，写入路径上必须脱敏，而不是指望调用点自觉。"""
    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        record_event(
            db,
            run,
            EVENT_SUBMITTED,
            input_snapshot={
                "password": "p@ss",
                "jdbc_url": "jdbc:mysql://u:p@h/db",
                "nested": {"api_key": "k", "keep": "ok"},
                "idempotency_key": "execution-run:1",
            },
        )

        event = _events(db, execution_world.run_id)[-1]
        snapshot = event.input_snapshot

        assert snapshot["password"] == REDACTED
        assert snapshot["nested"]["api_key"] == REDACTED
        # 非凭据字段必须保留，否则快照失去排障价值
        assert snapshot["nested"]["keep"] == "ok"
        assert snapshot["idempotency_key"] == "execution-run:1"


def test_event_table_is_append_only_across_run_lifecycle(session_factory, execution_world):
    """事件是流水：状态会覆盖，事件只增。"""
    with session_factory() as db:
        run_execution(
            db,
            run_id=execution_world.run_id,
            worker_id=uuid.uuid4(),
            plane=_FakePlane(error=RuntimeError("boom")),
        )
        before = len(_events(db, execution_world.run_id))

        # 再跑一次：运行已是终态，不会产生新事件
        run_execution(db, run_id=execution_world.run_id, worker_id=uuid.uuid4())

        assert len(_events(db, execution_world.run_id)) == before


# --------------------------------------------------------------------------
# Celery 装配
# --------------------------------------------------------------------------


def test_worker_id_is_stable_within_a_process():
    from app.worker.tasks import worker_id

    assert worker_id() == worker_id()


def test_beat_schedule_targets_the_dispatch_task():
    """Beat 引用的是任务名；名字对不上等于定时任务静默不触发。"""
    from app.worker.celery_app import TASK_DISPATCH_OUTBOX, celery_app

    schedule = celery_app.conf.beat_schedule
    assert schedule["dispatch-outbox"]["task"] == TASK_DISPATCH_OUTBOX
    assert float(schedule["dispatch-outbox"]["schedule"]) == float(
        settings.outbox_dispatch_interval_seconds
    )


def test_tasks_are_registered_under_explicit_names():
    import app.worker.tasks  # noqa: F401 —— 导入即注册
    from app.worker.celery_app import (
        TASK_DISPATCH_OUTBOX,
        TASK_EXECUTION_RUN,
        celery_app,
    )

    assert TASK_DISPATCH_OUTBOX in celery_app.tasks
    assert TASK_EXECUTION_RUN in celery_app.tasks


def test_long_running_tasks_do_not_prefetch():
    """长任务预取会让一个 Worker 攥着一批任务，其余 Worker 空转。"""
    from app.worker.celery_app import celery_app

    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_acks_late is True


@pytest.fixture(autouse=True)
def _isolate_data_plane():
    """默认适配器是进程级单例，测试不得把替身留在里面。"""
    from app.services import execution

    original = execution.get_data_plane()
    yield
    execution.set_data_plane(original)
