"""Celery 任务（M8，FR-28 ~ FR-30 / T-19）。

三条任务构成一条闭环：

  - ``outbox.dispatch``（Beat 周期触发）：扫描待发事件，投给队列；
  - ``execution.run``（队列消费）：认领运行并推进执行；
  - ``maintenance.sweep``（Beat 周期触发）：回收不会自己走到终态的运行。

**每条任务各开自己的会话**：任务运行在 Worker 进程里，与 API 的请求级会话
生命周期完全不同（一次请求一个会话，一个任务可能跑几十分钟）。
共用会话会让长任务的未提交事务一直占着连接。
"""

import logging
import os
import socket
import uuid

from app.db.session import SessionLocal
from app.services.audit_verifier import record_verification, verify_chain
from app.services.execution import run_execution
from app.services.maintenance import sweep
from app.services.outbox import deliver_to_queue, dispatch_pending
from app.worker.celery_app import (
    TASK_AUDIT_VERIFY,
    TASK_DISPATCH_OUTBOX,
    TASK_EXECUTION_RUN,
    TASK_MAINTENANCE_SWEEP,
    celery_app,
)

logger = logging.getLogger(__name__)


def worker_id() -> uuid.UUID:
    """本 Worker 进程的稳定标识（租约持有者）。

    由「主机名 + 进程号」推导，因此同一进程内反复调用得到同一个 id——
    租约续期与终态写入都要凭它证明「我还是那个持有者」。
    """
    return uuid.uuid5(uuid.NAMESPACE_DNS, f"{socket.gethostname()}:{os.getpid()}")


@celery_app.task(name=TASK_DISPATCH_OUTBOX, ignore_result=False)
def dispatch_outbox(limit: int | None = None) -> dict:
    """分发一批待投递事件（FR-28）。"""
    db = SessionLocal()
    try:
        report = dispatch_pending(db, deliver_to_queue(), limit=limit)
        if report.dead:
            # 重试耗尽是需要人看的事件：此时系统已经无法自行恢复
            logger.error("Outbox 有事件投递失败且重试耗尽：%s", report.dead)
        return {
            "delivered": len(report.delivered),
            "retried": len(report.retried),
            "dead": len(report.dead),
        }
    finally:
        db.close()


@celery_app.task(name=TASK_EXECUTION_RUN)
def execute_run(run_id: str) -> dict:
    """认领并执行一次运行（FR-29）。

    **不做自动重试**：任务级重试会把「已被认领」与「真的失败」混为一谈——
    前者重试毫无意义，后者重试要先解决失败原因（数据面不可达时重试一百次
    还是一样的结果）。失败的重排由维护任务按租约回收处理，那里能看到全貌。
    """
    db = SessionLocal()
    try:
        run = run_execution(db, run_id=uuid.UUID(run_id), worker_id=worker_id())
        if run is None:
            return {"run_id": run_id, "status": "skipped"}
        return {"run_id": str(run.id), "status": run.status.value}
    finally:
        db.close()


@celery_app.task(name=TASK_MAINTENANCE_SWEEP, ignore_result=False)
def maintenance_sweep() -> dict:
    """跑一轮维护扫描（T-19）。

    **不吞异常**：这里处理的三种情形——租约失联、投递不可达、审批过期——都是
    「系统已经无法自行恢复」那一类。把异常咽下去只会让下一轮再撞一次同样的墙；
    让它失败，至少 Celery 会留下一条可查的记录。
    """
    db = SessionLocal()
    try:
        return sweep(db).as_dict()
    finally:
        db.close()


@celery_app.task(name=TASK_AUDIT_VERIFY, ignore_result=False)
def audit_verify() -> dict:
    """跑一轮证据链巡检（T-20，SPEC §8「启动时与定期巡检」的后者）。

    结论落 ``audit_verifications``，**不写进被校验的那条链**——往链里插一条
    「链是好的」并不能证明链是好的，只会把校验动作变成链的一部分，于是校验器
    要校验自己。

    **不吞异常**：巡检失败本身就是需要人看的事（多半是数据库不可达），
    咽下去只会让下一轮撞同一堵墙，而且这一轮发生过什么也无从查起。
    """
    db = SessionLocal()
    try:
        result = verify_chain(db, trigger="periodic")
        record_verification(db, result, trigger="periodic")
        return result.as_dict()
    finally:
        db.close()
