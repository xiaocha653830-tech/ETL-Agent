"""维护任务（M8，T-19）：超时回收、审批过期级联终止、租约回收重排。

三项合起来回答同一个问题：**哪些运行已经不可能自己走到终态了**。

一个运行卡住的形态只有三种，本模块按形态收口（SPEC §10 T-19 的三项即这三条）：

1. **超时回收**——``pending`` 且投递已确认不可达（重试耗尽，或投递事件缺失）。
   没有任何东西持有它：队列不会再捡起来，租约机制也无从谈起（它压根没起过租约）。
   这是三种形态里唯一「连告警都没有」的一种，重试耗尽的日志沉在 Outbox 里，
   而运行列表上一片安静。
2. **租约回收**——``running`` 且租约已过期。执行方失联之后，行上的 ``running``
   就是一句已经失效的陈述：它说「有人在干活」，而那个人已经不在了（FR-29）。
3. **审批过期级联终止**——审批已失效而运行尚未开始。授权不成立了，它不该再被执行。

**只有第 2 项可能「重排」，而且只在还没有尝试过任何副作用时才重排**。提交作业的
幂等台账里已有记录，意味着数据面上可能已经有一个作业在往影子表里写；再排一次就是
第二份写入，而控制面无从确认第一份还在不在。这种情形收敛为失败并请人工核对残留，
这不是保守，是唯一诚实的选项。

**为什么时间判定放在 Python 而不是 SQL**：项目已有的领域谓词（``is_expired`` /
``is_lease_expired``）都用 :func:`app.db.types.as_utc` 判时间，为的是跨 SQLite 与
PostgreSQL 语义一致。维护扫描若另起一套 SQL 时间比较，就等于让同一个「过期」概念
在两条路径上有两种含义。状态过滤留在 SQL 里（走索引），时间判定统一走领域谓词。
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import ErrorCode
from app.db.types import as_utc
from app.models.approval import Preparation, PreparationStatus
from app.models.execution import ExecutionRun, ExecutionRunStatus, OutboxStatus
from app.services.execution import (
    EVENT_ABANDONED,
    EVENT_RECLAIMED,
    OPERATION_SUBMIT_JOB,
    cancel_unstarted,
    record_event,
)
from app.services.lease import is_lease_expired, reclaim_lease
from app.services.outbox import find_by_run, requeue

logger = logging.getLogger(__name__)

# 台账键直接复用执行层的那个常量，不在这里另写一份字面量：同一个键出现两处
# 定义，将来改一处就会让维护任务去查一个永远不会被写入的键，而那种失效是静默的。


@dataclass(frozen=True)
class MaintenanceReport:
    """一次维护扫描的结果（可序列化，供日志与人工排查）。"""

    expired_preparations: tuple[uuid.UUID, ...] = ()
    cancelled_runs: tuple[uuid.UUID, ...] = ()
    requeued_runs: tuple[uuid.UUID, ...] = ()
    abandoned_runs: tuple[uuid.UUID, ...] = ()
    # 扫描期间被 Worker 抢先推进的运行：不是错误，是正常的并发让位
    superseded: tuple[uuid.UUID, ...] = ()
    details: dict[str, list[str]] = field(default_factory=dict)

    @property
    def touched(self) -> int:
        return (
            len(self.expired_preparations)
            + len(self.cancelled_runs)
            + len(self.requeued_runs)
            + len(self.abandoned_runs)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "expired_preparations": [str(item) for item in self.expired_preparations],
            "cancelled_runs": [str(item) for item in self.cancelled_runs],
            "requeued_runs": [str(item) for item in self.requeued_runs],
            "abandoned_runs": [str(item) for item in self.abandoned_runs],
            "superseded": [str(item) for item in self.superseded],
            "touched": self.touched,
        }


# --------------------------------------------------------------------------
# 审批过期级联终止
# --------------------------------------------------------------------------


def expire_preparations(
    db: Session, *, limit: int | None = None
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """把过期准备单置为 ``expired``，并终止其尚未开始的运行。

    返回 ``(过期的准备单, 被终止的运行)``。

    **级联只落到尚未开始的运行上**。已经跑起来的运行不受影响：它的授权在 Commit
    那一刻已经校验并消费（能力令牌是一次性的，5 分钟即失效），此后审批时限的流逝
    不构成对既有授权的回溯撤销。把「过了几个小时就杀掉正在跑的作业」当成安全，
    实际上只会制造半截数据——而半截数据比晚一点的授权更危险。

    真正需要级联的是 ``pending``：它还没开始，而它启动所需的授权已经不成立，
    继续放它进队列就是在执行一张过期通行证。

    **不接受注入的时钟**：过期判定统一走 :attr:`Preparation.is_expired`，而那个
    谓词自己取系统时间。要构造过期场景就去改数据的 ``expires_at``——把时钟挪走
    会让这里判出的「过期」与系统其余部分判出的「过期」不是同一个概念。
    """
    batch = limit or settings.maintenance_batch_size

    # 第一遍：给过期的准备单落状态。审批列表与审计看的是这一列，不能指望
    # 「下次有人来点审批时顺手改掉」——没人来点时它就永远显示成待审。
    expired: list[uuid.UUID] = []
    for preparation in db.scalars(
        select(Preparation)
        .where(
            Preparation.status.in_(
                (PreparationStatus.PENDING, PreparationStatus.APPROVED)
            )
        )
        .order_by(Preparation.expires_at)
        .limit(batch)
    ).all():
        # 领域谓词判过期（``Preparation.is_expired``），不在 SQL 里另写一套
        if not preparation.is_expired:
            continue
        preparation.status = PreparationStatus.EXPIRED
        db.commit()
        expired.append(preparation.id)
        logger.info("准备单已过期 preparation=%s", preparation.id)

    # 第二遍：级联**按运行扫**，而不是顺着刚过期的准备单往下找。准备单一被置为
    # ``expired`` 就退出了第一遍的候选集，可它的 ``pending`` 运行可能在那之后才
    # 出现——租约回收重排产生的正是这种运行。顺着准备单扫会漏掉它们，而漏掉的
    # 后果是：一张已经失效的通行证被重新塞进队列并执行。
    #
    # 判据只看「这个运行的准备单过期了吗」，不看准备单状态：``committed`` 的准备单
    # 过期后不该被改写成 ``expired``（Commit 是已经发生的事实），但它下面那张还没
    # 开始的运行同样不该再启动。
    cancelled: list[uuid.UUID] = []
    for run in db.scalars(
        select(ExecutionRun)
        .where(ExecutionRun.status == ExecutionRunStatus.PENDING)
        .order_by(ExecutionRun.created_at)
        .limit(batch)
    ).all():
        preparation = db.get(Preparation, run.preparation_id)
        if preparation is None or not preparation.is_expired:
            continue
        if (
            cancel_unstarted(
                db,
                run,
                actor="system:maintenance",
                reason="审批已过期，执行授权不再成立，运行未开始即终止",
            )
            is None
        ):
            # 扫描期间被 Worker 抢先认领：授权过期的结论仍然成立，但它已经跑起来了。
            # 此时终止等于制造半截数据，交给租约回收按失联处理。
            continue
        cancelled.append(run.id)
        logger.info("运行授权已过期，未开始即终止 run=%s", run.id)

    return expired, cancelled


# --------------------------------------------------------------------------
# 租约回收（重排或收敛）
# --------------------------------------------------------------------------


def _submit_was_attempted(db: Session, run_id: uuid.UUID) -> bool:
    """这个运行有没有向数据面提交过作业（哪怕只试了一次）。"""
    from app.models.execution import RunOperation

    return (
        db.scalar(
            select(RunOperation.id).where(
                RunOperation.run_id == run_id,
                RunOperation.operation_key == OPERATION_SUBMIT_JOB,
            )
        )
        is not None
    )


def reclaim_expired_leases(
    db: Session, *, now: datetime | None = None, limit: int | None = None
) -> tuple[list[uuid.UUID], list[uuid.UUID], list[uuid.UUID]]:
    """回收租约已过期的运行，返回 ``(重排, 收敛为失败, 被抢先)``。

    **重排只发给还没有产生副作用的运行**：没有提交记录意味着数据面上没有作业，
    把运行退回 ``pending`` 并重新投递是安全的，作业会在新 Worker 上真正跑起来。

    有提交记录的一律收敛为失败：数据面可能已经有一个作业在写影子表，重排就是
    第二次写入。控制面没有那个 jobId，既停不掉也查不到，因此不猜。
    """
    moment = now or datetime.now(UTC)
    batch = limit or settings.maintenance_batch_size
    candidates = db.scalars(
        select(ExecutionRun)
        .where(ExecutionRun.status == ExecutionRunStatus.RUNNING)
        .order_by(ExecutionRun.lease_expires_at)
        .limit(batch)
    ).all()

    requeued: list[uuid.UUID] = []
    abandoned: list[uuid.UUID] = []
    superseded: list[uuid.UUID] = []
    for run in candidates:
        if not is_lease_expired(run, now=moment):
            continue

        if _submit_was_attempted(db, run.id):
            if not reclaim_lease(
                db,
                run_id=run.id,
                observed_expiry=run.lease_expires_at,
                values={
                    "status": ExecutionRunStatus.FAILED,
                    "error": (
                        f"{ErrorCode.BIZ_RUN_ABANDONED.value}: "
                        "租约过期且已尝试提交数据面作业，不自动重排，请核对目标端残留"
                    )[:512],
                    "finished_at": moment,
                    "worker_lease_owner": None,
                    "lease_expires_at": moment,
                },
            ):
                superseded.append(run.id)
                continue
            db.commit()
            db.refresh(run)
            record_event(
                db,
                run,
                EVENT_RECLAIMED,
                message="租约过期且已尝试提交数据面作业，不自动重排（请核对目标端残留）",
            )
            abandoned.append(run.id)
            logger.warning(
                "运行租约过期且已有提交记录，收敛为失败 run=%s", run.id
            )
            continue

        event = find_by_run(db, run.id)
        if event is None:
            # 没有投递事件就永远不会有 Worker 来认领，重排无处可发
            if not reclaim_lease(
                db,
                run_id=run.id,
                observed_expiry=run.lease_expires_at,
                values={
                    "status": ExecutionRunStatus.FAILED,
                    "error": (
                        f"{ErrorCode.BIZ_RUN_ABANDONED.value}: 投递事件缺失，无法重排"
                    )[:512],
                    "finished_at": moment,
                    "worker_lease_owner": None,
                    "lease_expires_at": moment,
                },
            ):
                superseded.append(run.id)
                continue
            db.commit()
            db.refresh(run)
            record_event(
                db, run, EVENT_RECLAIMED, message="投递事件缺失，无法重排，收敛为失败"
            )
            abandoned.append(run.id)
            continue

        if not reclaim_lease(
            db,
            run_id=run.id,
            observed_expiry=run.lease_expires_at,
            values={
                "status": ExecutionRunStatus.PENDING,
                "started_at": None,
                "finished_at": None,
                "worker_lease_owner": None,
                "lease_expires_at": None,
                "heartbeat_at": None,
            },
        ):
            superseded.append(run.id)
            continue
        # 运行与它的投递事件在同一事务里回到待执行状态：只回一半会留下
        # 「运行是 pending 但事件已 dispatched」的死单，而它不会被任何人捡起
        requeue(db, event, now=moment)
        db.commit()
        db.refresh(run)
        record_event(
            db,
            run,
            EVENT_RECLAIMED,
            message="租约过期且尚无副作用，已重排等待新 Worker 认领",
        )
        requeued.append(run.id)
        logger.info("运行租约过期且尚无副作用，已重排 run=%s", run.id)

    return requeued, abandoned, superseded


# --------------------------------------------------------------------------
# 超时回收：投递已确认不可达
# --------------------------------------------------------------------------


def reclaim_stalled_runs(
    db: Session,
    *,
    now: datetime | None = None,
    grace_seconds: int | None = None,
    limit: int | None = None,
) -> list[uuid.UUID]:
    """回收永远不会被执行的 ``pending`` 运行，返回被回收的运行 id。

    判据是**投递已确认不可达**（事件重试耗尽或事件缺失）**且**滞留超过宽限期。
    只看「pending 很久了」是不够的：队列拥塞时 pending 是正常的等待，把它判死
    等于在系统最忙的时候主动制造失败。重试耗尽才是不可达的证据。

    「事件缺失」也算不可达：没有投递事件的运行没有任何人会来认领，它会一直
    ``pending`` 下去，而且是静默的。
    """
    moment = now or datetime.now(UTC)
    grace = grace_seconds if grace_seconds is not None else settings.run_stalled_grace_seconds
    cutoff = moment - timedelta(seconds=grace)
    candidates = db.scalars(
        select(ExecutionRun)
        .where(ExecutionRun.status == ExecutionRunStatus.PENDING)
        .order_by(ExecutionRun.created_at)
        .limit(limit or settings.maintenance_batch_size)
    ).all()

    abandoned: list[uuid.UUID] = []
    for run in candidates:
        if as_utc(run.created_at) > cutoff:
            continue
        event = find_by_run(db, run.id)
        if event is not None and event.status is not OutboxStatus.FAILED:
            continue

        reason = (
            "投递事件缺失，运行不可能被认领"
            if event is None
            else f"投递重试已耗尽（尝试 {event.attempts} 次），运行不可能被执行"
        )
        run.status = ExecutionRunStatus.FAILED
        run.error = f"{ErrorCode.BIZ_RUN_ABANDONED.value}: {reason}"[:512]
        run.finished_at = moment
        db.commit()
        db.refresh(run)
        record_event(db, run, EVENT_ABANDONED, message=reason)
        abandoned.append(run.id)
        logger.error("运行不可能被执行，已回收 run=%s reason=%s", run.id, reason)

    return abandoned


# --------------------------------------------------------------------------
# 一次完整扫描
# --------------------------------------------------------------------------


def sweep(db: Session, *, now: datetime | None = None) -> MaintenanceReport:
    """按固定次序跑完三项维护（Celery beat 周期调用）。

    次序是刻意的：**先让不该开始的运行停下来，再处理已经开始的**。反过来的话，
    一个审批已过期的运行可能先被租约回收重排进队列，下一刻又被级联终止——
    两次动作互相抵消，而日志里留下两条都「成功」的记录。
    """
    moment = now or datetime.now(UTC)
    expired, cancelled = expire_preparations(db)
    requeued, abandoned_by_lease, superseded = reclaim_expired_leases(db, now=moment)
    stalled = reclaim_stalled_runs(db, now=moment)

    report = MaintenanceReport(
        expired_preparations=tuple(expired),
        cancelled_runs=tuple(cancelled),
        requeued_runs=tuple(requeued),
        abandoned_runs=tuple(abandoned_by_lease) + tuple(stalled),
        superseded=tuple(superseded),
    )
    if report.touched or report.superseded:
        logger.info("维护扫描完成 %s", report.as_dict())
    return report
