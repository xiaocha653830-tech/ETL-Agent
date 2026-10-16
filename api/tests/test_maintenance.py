"""T-19 维护回收（FR-29 / FR-30 的兜底）。

维护任务是系统里唯一「替别人宣布死亡」的地方：它写下的每一条终态，原本都该由
某个已经不在了的 Worker 写下。所以这里测的重点不是「它会不会回收」，而是
**它什么时候必须拒绝动手**——活着的租约、拥塞的队列、还没到宽限期的等待，
看起来都像故障，但把它们判死等于在系统最忙的时候主动制造失败。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode
from app.models.approval import Preparation, PreparationStatus
from app.models.execution import (
    ExecutionRun,
    ExecutionRunStatus,
    OutboxStatus,
    RunEvent,
    RunOperation,
    RunOperationStatus,
)
from app.services import maintenance
from app.services.execution import (
    EVENT_ABANDONED,
    EVENT_CANCELLED,
    EVENT_RECLAIMED,
    OPERATION_SUBMIT_JOB,
    claim_run,
)
from app.services.maintenance import (
    expire_preparations,
    reclaim_expired_leases,
    reclaim_stalled_runs,
    sweep,
)
from app.services.outbox import find_by_run


def _run(db: Session, run_id: uuid.UUID) -> ExecutionRun:
    db.expire_all()
    return db.get(ExecutionRun, run_id)


def _event_types(db: Session, run_id: uuid.UUID) -> list[str]:
    return [
        row.event_type
        for row in db.scalars(
            select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at)
        )
    ]


def _start_run(db: Session, run_id: uuid.UUID, *, lease_seconds: float) -> uuid.UUID:
    """把运行推进到 ``running``；``lease_seconds`` 为负即租约已过期。"""
    worker = uuid.uuid4()
    claim_run(db, run_id=run_id, worker_id=worker)
    run = _run(db, run_id)
    run.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    db.commit()
    return worker


def _record_submit_attempt(db: Session, run_id: uuid.UUID) -> None:
    """在幂等台账里留下一条「已经向数据面提交过」的痕迹。"""
    db.add(
        RunOperation(
            run_id=run_id,
            operation_key=OPERATION_SUBMIT_JOB,
            status=RunOperationStatus.STARTED,
        )
    )
    db.commit()


def _deliver(db: Session, run_id: uuid.UUID) -> None:
    """把投递事件推进到「已交给执行层」——这是运行开始之后的常态。"""
    event = find_by_run(db, run_id)
    event.status = OutboxStatus.DISPATCHED
    event.dispatched_at = datetime.now(UTC)
    event.attempts = 3
    db.commit()


def _expire(
    db: Session,
    preparation_id: uuid.UUID,
    *,
    status: PreparationStatus = PreparationStatus.APPROVED,
) -> None:
    preparation = db.get(Preparation, preparation_id)
    preparation.status = status
    preparation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()


# --------------------------------------------------------------------------
# 租约回收
# --------------------------------------------------------------------------


def test_expired_lease_without_side_effects_is_requeued(session_factory, execution_world):
    """失联 Worker 的遗孤，且没碰过数据面：重排就是让作业真正跑起来。"""
    with session_factory() as db:
        _start_run(db, execution_world.run_id, lease_seconds=-5)
        _deliver(db, execution_world.run_id)

        requeued, abandoned, superseded = reclaim_expired_leases(db)

    assert requeued == [execution_world.run_id]
    assert abandoned == []
    assert superseded == []

    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        assert run.status is ExecutionRunStatus.PENDING
        assert run.worker_lease_owner is None
        assert run.lease_expires_at is None
        assert run.started_at is None
        assert EVENT_RECLAIMED in _event_types(db, execution_world.run_id)

        event = find_by_run(db, execution_world.run_id)
        assert event.status is OutboxStatus.PENDING
        # 尝试次数归零：原来的 3 次记的是「投递不出去」，与这次回收不是同一件事
        assert event.attempts == 0


def test_expired_lease_with_a_submit_attempt_is_abandoned_not_requeued(
    session_factory, execution_world
):
    """碰过数据面的运行不重排：控制面没有那个 jobId，停不掉也查不到。"""
    with session_factory() as db:
        _start_run(db, execution_world.run_id, lease_seconds=-5)
        _deliver(db, execution_world.run_id)
        _record_submit_attempt(db, execution_world.run_id)

        requeued, abandoned, superseded = reclaim_expired_leases(db)

    assert requeued == []
    assert abandoned == [execution_world.run_id]
    assert superseded == []

    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        assert run.status is ExecutionRunStatus.FAILED
        assert ErrorCode.BIZ_RUN_ABANDONED.value in run.error
        assert EVENT_RECLAIMED in _event_types(db, execution_world.run_id)
        # 事件保持已投递：重排它等于向数据面递交第二份写入
        assert find_by_run(db, execution_world.run_id).status is OutboxStatus.DISPATCHED


def test_a_live_lease_is_not_touched(session_factory, execution_world):
    with session_factory() as db:
        _start_run(db, execution_world.run_id, lease_seconds=60)
        requeued, abandoned, superseded = reclaim_expired_leases(db)

    assert (requeued, abandoned, superseded) == ([], [], [])
    with session_factory() as db:
        assert _run(db, execution_world.run_id).status is ExecutionRunStatus.RUNNING


def test_a_run_without_an_outbox_event_is_abandoned(session_factory, execution_world):
    """没有投递事件的运行永远等不到 Worker，重排无处可发。"""
    with session_factory() as db:
        _start_run(db, execution_world.run_id, lease_seconds=-5)
        db.delete(find_by_run(db, execution_world.run_id))
        db.commit()

        requeued, abandoned, superseded = reclaim_expired_leases(db)

    assert requeued == []
    assert abandoned == [execution_world.run_id]
    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        assert run.status is ExecutionRunStatus.FAILED
        assert "投递事件缺失" in run.error


def test_a_renewal_during_the_sweep_wins_the_run(
    session_factory, execution_world, monkeypatch
):
    """扫描判定过期之后、写回之前，原持有者续上了租约——回收必须让位。

    这正是「先查后写」挡不住的窗口，也是 ``reclaim_lease`` 认到期时刻而不是
    认时钟的理由。
    """
    with session_factory() as db:
        _start_run(db, execution_world.run_id, lease_seconds=-5)
        real = maintenance.is_lease_expired
        renewed: list[bool] = []

        def renew_then_report_expired(run, *, now=None):
            if not renewed:
                renewed.append(True)
                other = session_factory()
                other.execute(
                    update(ExecutionRun)
                    .where(ExecutionRun.id == run.id)
                    .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=60))
                )
                other.commit()
                other.close()
            return real(run, now=now)

        monkeypatch.setattr(maintenance, "is_lease_expired", renew_then_report_expired)
        requeued, abandoned, superseded = reclaim_expired_leases(db)

    assert (requeued, abandoned) == ([], [])
    assert superseded == [execution_world.run_id]
    with session_factory() as db:
        assert _run(db, execution_world.run_id).status is ExecutionRunStatus.RUNNING


# --------------------------------------------------------------------------
# 超时回收：投递已确认不可达
# --------------------------------------------------------------------------


def test_a_stalled_run_with_exhausted_delivery_is_abandoned(
    session_factory, execution_world
):
    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        run.created_at = datetime.now(UTC) - timedelta(hours=2)
        event = find_by_run(db, execution_world.run_id)
        event.status = OutboxStatus.FAILED
        event.attempts = 8
        db.commit()

        abandoned = reclaim_stalled_runs(db)

    assert abandoned == [execution_world.run_id]
    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        assert run.status is ExecutionRunStatus.FAILED
        assert ErrorCode.BIZ_RUN_ABANDONED.value in run.error
        assert "重试已耗尽" in run.error
        assert EVENT_ABANDONED in _event_types(db, execution_world.run_id)


def test_a_pending_run_within_the_grace_window_is_not_touched(
    session_factory, execution_world
):
    """刚进来的 pending 是正常等待，不是故障。"""
    with session_factory() as db:
        event = find_by_run(db, execution_world.run_id)
        event.status = OutboxStatus.FAILED
        db.commit()

        assert reclaim_stalled_runs(db) == []

    with session_factory() as db:
        assert _run(db, execution_world.run_id).status is ExecutionRunStatus.PENDING


def test_a_pending_run_with_live_delivery_is_not_touched(
    session_factory, execution_world
):
    """队列拥塞时 pending 会积压很久，但投递还在队列上——把它判死是主动制造失败。"""
    with session_factory() as db:
        run = _run(db, execution_world.run_id)
        run.created_at = datetime.now(UTC) - timedelta(hours=2)
        db.commit()

        assert reclaim_stalled_runs(db) == []

    with session_factory() as db:
        assert _run(db, execution_world.run_id).status is ExecutionRunStatus.PENDING


# --------------------------------------------------------------------------
# 审批过期级联终止
# --------------------------------------------------------------------------


def test_an_expired_preparation_cancels_a_run_that_never_started(
    session_factory, execution_world
):
    with session_factory() as db:
        _expire(db, execution_world.preparation_id)
        expired, cancelled = expire_preparations(db)

    assert expired == [execution_world.preparation_id]
    assert cancelled == [execution_world.run_id]

    with session_factory() as db:
        assert (
            db.get(Preparation, execution_world.preparation_id).status
            is PreparationStatus.EXPIRED
        )
        run = _run(db, execution_world.run_id)
        assert run.status is ExecutionRunStatus.CANCELLED
        assert run.finished_at is not None
        assert EVENT_CANCELLED in _event_types(db, execution_world.run_id)


def test_an_expired_preparation_leaves_a_running_execution_alone(
    session_factory, execution_world
):
    """跑起来之后审批时限不再有权回溯撤销授权——强杀只会制造半截数据。"""
    with session_factory() as db:
        _start_run(db, execution_world.run_id, lease_seconds=60)
        _expire(db, execution_world.preparation_id)

        expired, cancelled = expire_preparations(db)

    assert expired == [execution_world.preparation_id]
    assert cancelled == []
    with session_factory() as db:
        assert _run(db, execution_world.run_id).status is ExecutionRunStatus.RUNNING


def test_a_live_preparation_is_not_touched(session_factory, execution_world):
    with session_factory() as db:
        expired, cancelled = expire_preparations(db)

    assert (expired, cancelled) == ([], [])
    with session_factory() as db:
        assert _run(db, execution_world.run_id).status is ExecutionRunStatus.PENDING


def test_a_requeued_run_is_terminated_once_its_approval_has_expired(
    session_factory, execution_world
):
    """重排不是复活：授权早就不成立了，被重排回队列的运行下一轮就该终止。

    这是「顺着刚过期的准备单找运行」会漏掉的那一种——准备单在第一轮就已经是
    ``expired``，第二轮它根本不在候选集里，可它的运行又回到了 ``pending``。
    """
    with session_factory() as db:
        _expire(db, execution_world.preparation_id)
        _start_run(db, execution_world.run_id, lease_seconds=-5)

        # 第一轮：准备单落 expired（运行正在跑，不受影响），随后被租约回收重排
        expire_preparations(db)
        requeued, _, _ = reclaim_expired_leases(db)
        assert requeued == [execution_world.run_id]

        # 第二轮：它以 pending 的姿态重新出现在扫描里
        _, cancelled = expire_preparations(db)

    assert cancelled == [execution_world.run_id]
    with session_factory() as db:
        assert _run(db, execution_world.run_id).status is ExecutionRunStatus.CANCELLED


def test_an_expired_committed_preparation_keeps_its_status(session_factory, execution_world):
    """Commit 是已经发生的事实，不因超时被改写；但它的 pending 运行同样不该启动。"""
    with session_factory() as db:
        _expire(db, execution_world.preparation_id, status=PreparationStatus.COMMITTED)

        expired, cancelled = expire_preparations(db)

    assert expired == []
    assert cancelled == [execution_world.run_id]
    with session_factory() as db:
        assert (
            db.get(Preparation, execution_world.preparation_id).status
            is PreparationStatus.COMMITTED
        )


# --------------------------------------------------------------------------
# 一次完整扫描
# --------------------------------------------------------------------------


def test_sweep_leaves_a_healthy_run_alone(session_factory, execution_world):
    """维护任务最大的风险不是漏掉故障，是把正常等待判成故障。"""
    with session_factory() as db:
        report = sweep(db)

    assert report.touched == 0
    assert report.superseded == ()
    with session_factory() as db:
        assert _run(db, execution_world.run_id).status is ExecutionRunStatus.PENDING


def test_sweep_runs_termination_before_reclaim(session_factory, execution_world):
    """一轮扫描里三类故障各归各位，且报告如实反映动了哪些。"""
    with session_factory() as db:
        _expire(db, execution_world.preparation_id)
        report = sweep(db)

    assert report.expired_preparations == (execution_world.preparation_id,)
    assert report.cancelled_runs == (execution_world.run_id,)
    assert report.requeued_runs == ()
    assert report.abandoned_runs == ()
    assert report.touched == 2


def test_the_maintenance_sweep_is_on_the_beat_schedule():
    """任务名进了调度表才算真的会被触发——调度表里引用的是字符串，不是函数。"""
    from app.core.config import settings
    from app.worker.celery_app import TASK_MAINTENANCE_SWEEP, celery_app

    entry = celery_app.conf.beat_schedule["maintenance-sweep"]
    assert entry["task"] == TASK_MAINTENANCE_SWEEP
    assert entry["schedule"] == float(settings.maintenance_sweep_interval_seconds)
