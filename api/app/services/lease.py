"""执行租约与 fencing（M8，FR-29，SPEC §8「Worker 心跳失败不伪造成功」）。

**要解决的问题**：Worker 会因为 GC 停顿、网络分区、容器被驱逐而「假死」——
它还活着，还以为自己在干活，但实际已经很久没有心跳了。此时维护任务会把运行
回收并交给新 Worker。旧 Worker 醒过来后如果照常写回终态，就会把**新 Worker 的
结论覆盖成自己的**——一次已经跑完的执行被改写成失败，或者反过来。

**fencing 的做法是让写操作自带守卫**，而不是「先查后写」：

    UPDATE execution_runs
       SET status = 'succeeded', ...
     WHERE id = ? AND status = 'running' AND worker_lease_owner = ?

影响行数为 0 就说明自己已经不是持有者了，于是旧 Worker 只能安静退出。
「先查后写」中间有一个窗口，fencing 要挡的恰恰是这个窗口。

**为什么守卫里不判租约是否过期**：过期的后果由维护任务处理——它会把运行回收、
换一个持有者。「过期即禁止写入」会让一个没被别人接管的运行永远卡在 ``running``
（没人能给它写终态），而它本来只是慢了一点。换持有者才是真正的失去，
所以守卫认的是持有者，不是时钟。
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import LeaseLost, ResourceNotFound
from app.db.types import as_utc
from app.models.execution import ExecutionRun, ExecutionRunStatus

logger = logging.getLogger(__name__)


def now_utc() -> datetime:
    return datetime.now(UTC)


def lease_deadline(seconds: int | None = None) -> datetime:
    return now_utc() + timedelta(seconds=seconds or settings.run_lease_seconds)


def is_lease_expired(run: ExecutionRun, *, now: datetime | None = None) -> bool:
    if run.lease_expires_at is None:
        return True
    return as_utc(run.lease_expires_at) <= (now or now_utc())


def assert_lease_held(
    db: Session, *, run_id: uuid.UUID, worker_id: uuid.UUID
) -> ExecutionRun:
    """读取侧确认：本 Worker 仍是该运行的租约持有者。

    用于派发副作用**之前**的提前失败——让一个已经失去租约的 Worker 尽早停下，
    而不是先把作业提交出去、再在写终态时才发现自己没资格。
    """
    run = db.get(ExecutionRun, run_id)
    if run is None:
        raise ResourceNotFound("运行记录不存在")
    if run.status is not ExecutionRunStatus.RUNNING:
        raise LeaseLost(f"运行已处于 {run.status.value}，不再接受本 Worker 的推进")
    if run.worker_lease_owner != worker_id:
        raise LeaseLost("运行的租约已由其他 Worker 持有")
    return run


def renew_lease(
    db: Session,
    *,
    run_id: uuid.UUID,
    worker_id: uuid.UUID,
    lease_seconds: int | None = None,
) -> ExecutionRun:
    """心跳续期。续不上就说明租约已经易主或运行已终结——抛 :class:`LeaseLost`。"""
    now = now_utc()
    result = db.execute(
        update(ExecutionRun)
        .where(
            ExecutionRun.id == run_id,
            ExecutionRun.status == ExecutionRunStatus.RUNNING,
            ExecutionRun.worker_lease_owner == worker_id,
        )
        .values(lease_expires_at=lease_deadline(lease_seconds), heartbeat_at=now)
    )
    if result.rowcount != 1:
        db.rollback()
        raise LeaseLost("租约续期失败：本 Worker 已不是持有者")
    db.commit()
    return db.get(ExecutionRun, run_id)


def fenced_update(
    db: Session, *, run_id: uuid.UUID, worker_id: uuid.UUID, values: dict
) -> bool:
    """带租约守卫的条件更新；返回是否命中（即是否仍由本 Worker 持有）。

    未命中时**同时回滚当前事务**再返回 ``False``：守卫失败意味着这次写入本来
    就不该发生，把半截事务留在会话里只会让后续操作踩在未定义的中间态上。

    **提交权归调用方**：终态写入往往要与事件落库配合，本函数只负责把守卫写进
    ``WHERE``，不替调用方决定事务边界。
    """
    result = db.execute(
        update(ExecutionRun)
        .where(
            ExecutionRun.id == run_id,
            ExecutionRun.status == ExecutionRunStatus.RUNNING,
            ExecutionRun.worker_lease_owner == worker_id,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    return True


def reclaim_lease(
    db: Session,
    *,
    run_id: uuid.UUID,
    observed_expiry: datetime | None,
    values: dict,
) -> bool:
    """按「租约没有被续过」为守卫回收一次运行（T-19 维护回收）。

    维护任务没有 ``worker_id``——执行方失联正是它要处理的情形，认持有者这条路
    从一开始就走不通。它只能认那个**可观察且对方一定会改变的量**：读到的到期时刻。
    原持有者只要还活着就必须续期（长作业续不上租约等于自杀），一续期这个条件
    就不成立，回收自动让位；反之说明它确实失联了，条件成立，回收生效。

    这与 :func:`fenced_update` 是同一种纪律的两个侧面：**守卫必须建立在对方一定
    会改变的量上**，而不是建立在「我刚读过，所以现在还是这样」的假设上。

    命中时**不自作主张提交**，提交权同样归调用方——回收结果要与事件落库一起落定。
    """
    stmt = update(ExecutionRun).where(
        ExecutionRun.id == run_id,
        ExecutionRun.status == ExecutionRunStatus.RUNNING,
    )
    stmt = (
        stmt.where(ExecutionRun.lease_expires_at.is_(None))
        if observed_expiry is None
        else stmt.where(ExecutionRun.lease_expires_at == observed_expiry)
    )
    result = db.execute(stmt.values(**values))
    if result.rowcount != 1:
        db.rollback()
        return False
    return True
