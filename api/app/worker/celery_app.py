"""Celery 应用与 Beat 调度（M8，FR-28 / FR-29，SPEC §4 部署拓扑）。

Redis 既作 broker 也作 backend（SPEC §2 技术栈），与 API 共用同一个 PostgreSQL
——API 与 Worker 必须连同一个库，否则 Outbox 写了也没人看得见（SPEC §4 部署约束）。

**Beat 必须单实例**（SPEC §4）：多个 Beat 会让分发任务被重复触发。
重复触发本身是安全的（认领是原子的、投递是幂等的），但那是在用幂等去兜一个
本可以避免的重复，白白放大队列压力。

任务名**显式指定**（``outbox.dispatch`` / ``execution.run``）：Beat 的调度表里
引用的是任务名，隐式名会随模块路径重构而变，而调度表在容器重启后才生效——
名字一改就变成「定时任务静默不触发」，这类故障不会报错，只会让队列慢慢积压。
"""

from celery import Celery

from app.core.config import settings

TASK_DISPATCH_OUTBOX = "outbox.dispatch"
TASK_EXECUTION_RUN = "execution.run"

celery_app = Celery(
    "etl_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # 执行是长任务：不预取，避免一个 Worker 攥着一批任务而其他 Worker 空转
    worker_prefetch_multiplier=1,
    # 崩溃后让任务回到队列而不是凭空消失；重复由 run 的原子认领吸收
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    result_expires=3600,
    beat_schedule={
        "dispatch-outbox": {
            "task": TASK_DISPATCH_OUTBOX,
            "schedule": float(settings.outbox_dispatch_interval_seconds),
        }
    },
)
