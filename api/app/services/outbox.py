"""事务性 Outbox（M8，FR-28，ADR-04）。

**解决的问题**：Commit 若「先落业务事实、再调投递」，两步之间失败就留下
「运行记录存在但永远不被执行」的静默死单；若反过来，则可能出现「任务已在跑、
运行记录还不存在」。Outbox 把投递变成**一条与业务事实同事务写入的记录**，
真实投递由异步分发任务完成——于是要么两件事都成立，要么都不成立。

**幂等键是稳定逻辑键，不是随机 id**：写入方用 ``execution-run:<run_id>`` 这样的
键声明「这件事只应发生一次」。重复写入被唯一约束挡下，而不是靠投递方自觉去重。

本模块的写入函数**不自行提交事务**：Commit 需要把运行事实、能力令牌与 Outbox 记录
放进同一个事务，提交权归调用方。
"""

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.execution import OutboxEvent, OutboxStatus

logger = logging.getLogger(__name__)

# 事件类型。命名用「主体.动作」的形式，便于分发任务按前缀路由。
EVENT_RUN_REQUESTED = "execution.run_requested"

# 投递尝试上限：超过即置 failed 等人工介入，不再无限重试。
# 无限重试会让一条永远投不出去的事件反复占用扫描窗口，把队列的其余部分拖慢。
MAX_ATTEMPTS = 8


def enqueue(
    db: Session,
    *,
    idempotency_key: str,
    event_type: str,
    payload: dict[str, Any],
    available_at: datetime | None = None,
) -> OutboxEvent:
    """写入一条待投递事件；键已存在时返回既有记录（不重复插入）。

    使用 SAVEPOINT 而非整段回滚：调用方（Commit）的事务里还有运行事实与令牌，
    为了一个重复的幂等键把它们一起回滚，等于把「重复提交」变成「提交失败」。
    """
    existing = db.scalar(
        select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    event = OutboxEvent(
        idempotency_key=idempotency_key,
        event_type=event_type,
        payload=payload,
        status=OutboxStatus.PENDING,
        attempts=0,
        available_at=available_at or datetime.now(UTC),
    )
    try:
        with db.begin_nested():
            db.add(event)
            db.flush()
    except IntegrityError:
        # 并发写入同一幂等键：另一个事务已插入，取回它即可
        logger.info("Outbox 幂等键并发命中，复用既有事件 key=%s", idempotency_key)
        return db.scalar(
            select(OutboxEvent).where(OutboxEvent.idempotency_key == idempotency_key)
        )
    return event


def execution_run_key(run_id: uuid.UUID) -> str:
    """执行任务的稳定幂等键。"""
    return f"execution-run:{run_id}"


def backoff_seconds(attempts: int) -> int:
    """第 ``attempts`` 次失败后的退避时长（指数，带上限）。

    上限是必要的：没有上限的指数退避会在几次失败后把重试推到遥遥无期，
    与「等人工介入」无异，却既不告警也不可见。
    """
    if attempts < 1:
        return settings.outbox_backoff_base_seconds
    delay = settings.outbox_backoff_base_seconds * (2 ** (attempts - 1))
    return min(delay, settings.outbox_backoff_max_seconds)


# --------------------------------------------------------------------------
# 分发（T-13）
# --------------------------------------------------------------------------

# 投递方：把一条事件交给执行层。返回值无意义，抛异常即视为投递失败。
Deliver = Callable[[OutboxEvent], None]


@dataclass(frozen=True)
class DispatchReport:
    """一次分发扫描的结果。"""

    delivered: list[uuid.UUID] = field(default_factory=list)
    retried: list[uuid.UUID] = field(default_factory=list)
    dead: list[uuid.UUID] = field(default_factory=list)

    @property
    def scanned(self) -> int:
        return len(self.delivered) + len(self.retried) + len(self.dead)


def pending_events(
    db: Session, *, now: datetime | None = None, limit: int | None = None
) -> list[OutboxEvent]:
    """取出到期待投递的事件。

    按 ``available_at`` 排序而不是 ``created_at``：退避中的事件会把自己的
    ``available_at`` 推后，按到期时间取才能让「早该重试的」先于「刚进来的」被处理。
    """
    moment = now or datetime.now(UTC)
    stmt = (
        select(OutboxEvent)
        .where(
            OutboxEvent.status == OutboxStatus.PENDING,
            OutboxEvent.available_at <= moment,
        )
        .order_by(OutboxEvent.available_at)
        .limit(limit or settings.outbox_dispatch_batch_size)
    )
    return list(db.scalars(stmt))


def mark_dispatched(db: Session, event: OutboxEvent) -> None:
    event.status = OutboxStatus.DISPATCHED
    event.dispatched_at = datetime.now(UTC)
    event.last_error = None
    event.attempts += 1


def mark_retry(db: Session, event: OutboxEvent, error: str) -> None:
    """一次投递失败：记一次尝试、退避后移到期时间；超过上限则不再重试。"""
    event.attempts += 1
    event.last_error = error[:512]

    if event.attempts >= MAX_ATTEMPTS:
        # 置 failed 而不是继续退避：一条永远投不出去的事件反复占用扫描窗口，
        # 会把队列里其余部分一起拖慢，而且「一直在重试」看起来像还在努力。
        event.status = OutboxStatus.FAILED
        logger.error(
            "Outbox 投递重试耗尽 key=%s attempts=%s error=%s",
            event.idempotency_key,
            event.attempts,
            error,
        )
        return

    event.available_at = datetime.now(UTC) + timedelta(
        seconds=backoff_seconds(event.attempts)
    )


def dispatch_pending(
    db: Session,
    deliver: Deliver,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> DispatchReport:
    """扫描并投递一批待发事件。

    **投递与标记不是原子的**：若进程在「投递成功」与「标记已投递」之间崩溃，
    事件仍是 ``pending``，下一轮会**再投一次**。这个窗口无法靠本地事务消除，
    因此真正的约束放在消费侧——执行任务认领用条件 UPDATE 保证同一运行
    只会被一个 Worker 推进（见 ``app/services/execution.py``）。
    换句话说：Outbox 保证「不丢」，认领保证「不重」。

    单条事件投递失败不影响同批其余事件：逐条独立标记，一条坏消息不该让整批停摆。
    """
    report_delivered: list[uuid.UUID] = []
    report_retried: list[uuid.UUID] = []
    report_dead: list[uuid.UUID] = []

    for event in pending_events(db, now=now, limit=limit):
        try:
            deliver(event)
        except Exception as exc:  # noqa: BLE001 —— 投递方异常类型不可控
            already_failed = event.attempts + 1 >= MAX_ATTEMPTS
            mark_retry(db, event, f"{exc.__class__.__name__}: {exc}")
            db.commit()
            (report_dead if already_failed else report_retried).append(event.id)
            continue

        mark_dispatched(db, event)
        db.commit()
        report_delivered.append(event.id)

    return DispatchReport(
        delivered=report_delivered, retried=report_retried, dead=report_dead
    )


def deliver_to_queue(task: Callable[..., Any] | None = None) -> Deliver:
    """把事件投给 Celery 队列的投递方。

    延迟导入 Celery 任务：本模块被 API 进程导入，而 API 进程不应当因为
    引入一个消息队列客户端就产生启动期依赖。
    """
    if task is None:
        from app.worker.tasks import execute_run  # 循环导入：仅在调用时解析

        task = execute_run

    def _deliver(event: OutboxEvent) -> None:
        task.delay(str(event.payload["run_id"]))

    return _deliver


def events_by_status(db: Session, status: OutboxStatus, limit: int = 100) -> Iterable[OutboxEvent]:
    """按状态列出事件（运维排查用）。"""
    return db.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.status == status)
        .order_by(OutboxEvent.created_at.desc())
        .limit(limit)
    )
