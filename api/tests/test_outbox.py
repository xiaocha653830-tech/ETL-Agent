"""事务性 Outbox 分发测试（FR-28，ADR-04）。

核心断言：**投递失败不丢事件**，且失败以可观察的方式退避与收敛。
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.db.types import as_utc
from app.models.execution import OutboxStatus
from app.services.outbox import (
    EVENT_RUN_REQUESTED,
    MAX_ATTEMPTS,
    backoff_seconds,
    deliver_to_queue,
    dispatch_pending,
    enqueue,
    execution_run_key,
    mark_dispatched,
    pending_events,
)

# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------


def test_enqueue_is_idempotent(session_factory: sessionmaker[Session]):
    """同一幂等键不产生第二条记录——「只投一次」由唯一约束保证。"""
    from sqlalchemy import func, select

    from app.models.execution import OutboxEvent

    with session_factory() as db:
        first = enqueue(
            db,
            idempotency_key="k-1",
            event_type=EVENT_RUN_REQUESTED,
            payload={"a": 1},
        )
        db.commit()
        second = enqueue(
            db,
            idempotency_key="k-1",
            event_type=EVENT_RUN_REQUESTED,
            payload={"a": 1},
        )
        db.commit()

        assert first.id == second.id
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_enqueue_does_not_roll_back_the_caller_transaction(
    session_factory: sessionmaker[Session],
):
    """重复键只回滚到保存点，调用方事务里的其余写入必须还在。"""
    from app.models.execution import OutboxEvent

    with session_factory() as db:
        event = OutboxEvent(
            idempotency_key="k-2",
            event_type=EVENT_RUN_REQUESTED,
            payload={},
            status=OutboxStatus.PENDING,
            attempts=0,
            available_at=datetime.now(UTC),
        )
        db.add(event)
        db.flush()

        # 同一键再写一次：命中既有记录，而不是抛异常把上面的写入一起带走
        again = enqueue(
            db, idempotency_key="k-2", event_type=EVENT_RUN_REQUESTED, payload={}
        )
        db.commit()

        assert again.id == event.id


# --------------------------------------------------------------------------
# 分发
# --------------------------------------------------------------------------


def _deliver_recording(sink: list[uuid.UUID]):
    def _deliver(event):
        sink.append(event.id)

    return _deliver


def test_dispatch_marks_delivered(session_factory, execution_world):
    with session_factory() as db:
        delivered: list[uuid.UUID] = []
        report = dispatch_pending(db, _deliver_recording(delivered))

        assert report.delivered == [execution_world.event_id]
        assert report.retried == []
        assert delivered == [execution_world.event_id]

        from app.models.execution import OutboxEvent

        event = db.get(OutboxEvent, execution_world.event_id)
        assert event.status is OutboxStatus.DISPATCHED
        assert event.dispatched_at is not None
        assert event.attempts == 1


def test_dispatched_event_is_not_scanned_again(session_factory, execution_world):
    with session_factory() as db:
        dispatch_pending(db, _deliver_recording([]))
        second = dispatch_pending(db, _deliver_recording([]))
        assert second.scanned == 0


def test_failed_delivery_is_retried_with_backoff(session_factory, execution_world):
    """投递失败不丢事件：原地退避后重来。"""
    from app.models.execution import OutboxEvent

    def _boom(_event):
        raise ConnectionError("broker 拒绝连接")

    with session_factory() as db:
        report = dispatch_pending(db, _boom)
        assert report.retried == [execution_world.event_id]
        assert report.dead == []

        event = db.get(OutboxEvent, execution_world.event_id)
        assert event.status is OutboxStatus.PENDING
        assert event.attempts == 1
        assert "broker 拒绝连接" in event.last_error
        # 到期时间被推后，因此下一次扫描不会再碰到它
        assert as_utc(event.available_at) > datetime.now(UTC)

        assert dispatch_pending(db, _deliver_recording([])).scanned == 0


def test_retry_becomes_visible_again_after_backoff(session_factory, execution_world):
    from app.models.execution import OutboxEvent

    def _boom(_event):
        raise ConnectionError("broker 拒绝连接")

    with session_factory() as db:
        dispatch_pending(db, _boom)

        # 把到期时间拨回过去，模拟退避窗口已过
        event = db.get(OutboxEvent, execution_world.event_id)
        event.available_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()

        delivered: list[uuid.UUID] = []
        report = dispatch_pending(db, _deliver_recording(delivered))
        assert report.delivered == [execution_world.event_id]
        assert db.get(OutboxEvent, execution_world.event_id).attempts == 2


def test_retry_exhaustion_marks_failed(session_factory, execution_world):
    """重试耗尽后必须停下来并被看见，而不是无限重试。"""
    from app.models.execution import OutboxEvent

    def _boom(_event):
        raise ConnectionError("broker 拒绝连接")

    with session_factory() as db:
        for _ in range(MAX_ATTEMPTS):
            event = db.get(OutboxEvent, execution_world.event_id)
            event.available_at = datetime.now(UTC) - timedelta(seconds=1)
            db.commit()
            dispatch_pending(db, _boom)

        event = db.get(OutboxEvent, execution_world.event_id)
        assert event.status is OutboxStatus.FAILED
        assert event.attempts == MAX_ATTEMPTS
        assert dispatch_pending(db, _deliver_recording([])).scanned == 0


def test_one_bad_event_does_not_block_the_rest(session_factory, execution_world):
    """一条投不出去的消息不该让整批停摆。"""
    from app.models.execution import OutboxEvent

    with session_factory() as db:
        other = enqueue(
            db,
            idempotency_key="k-other",
            event_type=EVENT_RUN_REQUESTED,
            payload={"run_id": str(execution_world.run_id)},
        )
        db.commit()
        other_id = other.id

        def _deliver(event):
            if event.id == execution_world.event_id:
                raise ConnectionError("这一条坏掉了")

        report = dispatch_pending(db, _deliver)

        assert report.delivered == [other_id]
        assert report.retried == [execution_world.event_id]
        assert db.get(OutboxEvent, other_id).status is OutboxStatus.DISPATCHED


def test_batch_limit_bounds_one_scan(session_factory, execution_world):
    """批量上限保证一次扫描不会把队列压满，也不会长时间持锁。"""
    with session_factory() as db:
        for index in range(3):
            enqueue(
                db,
                idempotency_key=f"extra-{index}",
                event_type=EVENT_RUN_REQUESTED,
                payload={"run_id": str(execution_world.run_id)},
            )
        db.commit()

        report = dispatch_pending(db, _deliver_recording([]), limit=2)
        assert report.scanned == 2
        assert len(pending_events(db)) == 2


def test_events_ordered_by_availability(session_factory, execution_world):
    """先到期的先处理——退避中的事件不该插在刚到的事件之前。"""
    from app.models.execution import OutboxEvent

    with session_factory() as db:
        early = db.get(OutboxEvent, execution_world.event_id)
        early.available_at = datetime.now(UTC) - timedelta(minutes=5)

        later = enqueue(
            db,
            idempotency_key="k-later",
            event_type=EVENT_RUN_REQUESTED,
            payload={"run_id": str(execution_world.run_id)},
            available_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        db.commit()

        order = [event.id for event in pending_events(db)]
        assert order == [early.id, later.id]


def test_future_event_is_not_scanned(session_factory, execution_world):
    from app.models.execution import OutboxEvent

    with session_factory() as db:
        event = db.get(OutboxEvent, execution_world.event_id)
        event.available_at = datetime.now(UTC) + timedelta(hours=1)
        db.commit()

        assert pending_events(db) == []


# --------------------------------------------------------------------------
# 投递方与退避
# --------------------------------------------------------------------------


def test_deliver_to_queue_enqueues_the_run(session_factory, execution_world):
    """投递动作的语义：把 run_id 交给执行任务。"""
    from app.models.execution import OutboxEvent

    calls: list[str] = []

    class _Task:
        @staticmethod
        def delay(run_id: str) -> None:
            calls.append(run_id)

    with session_factory() as db:
        deliver = deliver_to_queue(_Task)
        deliver(db.get(OutboxEvent, execution_world.event_id))

    assert calls == [str(execution_world.run_id)]


def test_execution_run_key_is_stable():
    run_id = uuid.uuid4()
    assert execution_run_key(run_id) == f"execution-run:{run_id}"


@pytest.mark.parametrize("attempts", [1, 2, 3, 4])
def test_backoff_is_exponential(attempts: int):
    assert backoff_seconds(attempts) == min(
        settings.outbox_backoff_base_seconds * (2 ** (attempts - 1)),
        settings.outbox_backoff_max_seconds,
    )


def test_backoff_is_capped():
    assert backoff_seconds(50) == settings.outbox_backoff_max_seconds


def test_mark_dispatched_clears_previous_error(session_factory, execution_world):
    """成功投递后上一次的错误信息不该继续挂在记录上误导排查。"""
    from app.models.execution import OutboxEvent

    with session_factory() as db:
        event = db.get(OutboxEvent, execution_world.event_id)
        event.last_error = "上一次失败"
        mark_dispatched(db, event)
        db.commit()

        refreshed = db.get(OutboxEvent, execution_world.event_id)
        assert refreshed.last_error is None
        assert refreshed.status is OutboxStatus.DISPATCHED
