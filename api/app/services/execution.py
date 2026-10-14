"""受管执行的认领与推进（M8，FR-29 / FR-30）。

**认领是原子的**：``UPDATE ... WHERE status = 'pending'`` 且影响行数必须为 1。
Outbox 只能保证「不丢」，无法保证「不重」——投递与标记之间崩溃会再投一次，
所以重复是常态而非例外。认领用条件更新把这个重复吸收在入口：
同一运行只会被一个 Worker 从 ``pending`` 推进到 ``running``。

**能力令牌从 Outbox 载荷取，不落在运行表上**：令牌是一次性凭据，
运行事实是长期档案。把凭据写进档案，等于让每一份历史运行记录都留着一把
（虽然已失效的）钥匙，徒增泄漏面却没有任何好处。

**数据面是一道缝**：本模块只认 ``DataPlane`` 协议，不认 SeaTunnel。
T-15 接上真实适配器即可，认领、事件、终态写入都不必改。默认实现是
「数据面不可用」——在适配器接通之前，执行必然会失败，这是实情而不是占位。
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import (
    DataplaneUnavailable,
    LeaseLost,
    ResourceConflict,
    ResourceNotFound,
)
from app.models.execution import (
    ExecutionRun,
    ExecutionRunStatus,
    OutboxEvent,
    RunEvent,
    RunOperation,
    RunOperationStatus,
)
from app.schemas.execution import ExecutionCommand
from app.services.lease import fenced_update, renew_lease
from app.services.outbox import execution_run_key
from app.services.redaction import redact_snapshot

logger = logging.getLogger(__name__)

# 运行事件类型。事件是流水而不是状态，命名用「run.动作」的形式。
EVENT_CLAIMED = "run.claimed"
EVENT_SUBMITTED = "run.submitted"
EVENT_SUCCEEDED = "run.succeeded"
EVENT_FAILED = "run.failed"
EVENT_CANCELLED = "run.cancelled"
EVENT_CANCEL_REQUESTED = "run.cancel_requested"
EVENT_PROGRESS = "run.progress"

# 幂等台账的键：向数据面提交作业
OPERATION_SUBMIT_JOB = "data-plane.submit-job"


# --------------------------------------------------------------------------
# 事件时间线
# --------------------------------------------------------------------------


def record_event(
    db: Session,
    run: ExecutionRun,
    event_type: str,
    *,
    message: str | None = None,
    input_snapshot: Any = None,
    output_snapshot: Any = None,
    metrics: dict[str, Any] | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    commit: bool = True,
) -> RunEvent:
    """追加一条运行事件。

    **所有快照先过脱敏**（SPEC §8）：事件表是长期档案，任何一次「顺手记一下」
    都可能让凭据在其中沉淀下来。脱敏放在这里而不是各调用点，是因为调用点会变多。
    """
    event = RunEvent(
        run_id=run.id,
        event_type=event_type,
        message=message[:512] if message else None,
        input_snapshot=redact_snapshot(input_snapshot) if input_snapshot is not None else None,
        output_snapshot=(
            redact_snapshot(output_snapshot) if output_snapshot is not None else None
        ),
        metrics=redact_snapshot(metrics) if metrics is not None else None,
        trace_id=trace_id,
        span_id=span_id,
    )
    db.add(event)
    if commit:
        db.commit()
    else:
        db.flush()
    return event


# --------------------------------------------------------------------------
# 认领
# --------------------------------------------------------------------------


def claim_run(
    db: Session,
    *,
    run_id: uuid.UUID,
    worker_id: uuid.UUID,
    lease_seconds: int | None = None,
) -> ExecutionRun | None:
    """把运行从 ``pending`` 原子推进到 ``running`` 并起租约。

    返回 ``None`` 表示**没抢到**：已被别的 Worker 认领，或已处于终态。
    调用方应安静退出——这不是错误，而是重复投递被正常吸收的结果。
    """
    now = datetime.now(UTC)
    lease = now + timedelta(seconds=lease_seconds or settings.run_lease_seconds)

    result = db.execute(
        update(ExecutionRun)
        .where(
            ExecutionRun.id == run_id,
            ExecutionRun.status == ExecutionRunStatus.PENDING,
        )
        .values(
            status=ExecutionRunStatus.RUNNING,
            worker_lease_owner=worker_id,
            lease_expires_at=lease,
            heartbeat_at=now,
            started_at=now,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        return None

    db.commit()
    return db.get(ExecutionRun, run_id)


def load_command(db: Session, run_id: uuid.UUID) -> ExecutionCommand:
    """按运行 id 取回执行命令（即投递给本运行的那条 Outbox 载荷）。"""
    event = db.scalar(
        select(OutboxEvent).where(OutboxEvent.idempotency_key == execution_run_key(run_id))
    )
    if event is None:
        raise ResourceNotFound("该运行没有对应的投递事件")
    return ExecutionCommand.model_validate(event.payload)


# --------------------------------------------------------------------------
# 数据面（T-15 接入真实适配器）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionOutcome:
    """一次执行的数据面回执（FR-30 状态与度量回传）。"""

    read_rows: int = 0
    written_bytes: int = 0
    rejected_rows: int = 0
    detail: dict[str, Any] | None = None


class DataPlane(Protocol):
    """数据面适配器协议 —— M8 只依赖这个形状，不依赖 SeaTunnel。"""

    def submit(self, command: ExecutionCommand) -> ExecutionOutcome: ...


class UnavailableDataPlane:
    """默认适配器：数据面尚未接通。

    刻意让「没接数据面」表现为**执行失败**而不是空跑成功——
    一个假装跑通了的运行比一个明确失败的运行危险得多。
    """

    def submit(self, command: ExecutionCommand) -> ExecutionOutcome:
        raise DataplaneUnavailable("数据面适配器尚未接通（见 T-15）")


_data_plane: DataPlane = UnavailableDataPlane()


def get_data_plane() -> DataPlane:
    return _data_plane


def set_data_plane(plane: DataPlane) -> None:
    """装配 / 测试期替换适配器。"""
    global _data_plane
    _data_plane = plane


def submit_job_once(
    db: Session,
    *,
    run_id: uuid.UUID,
    command: ExecutionCommand,
    plane: DataPlane,
) -> ExecutionOutcome | None:
    """把「向数据面提交作业」做成至多一次（FR-27）。

    返回 ``None`` 表示此前已经尝试过且没有留下可用结论——调用方应按不可自动
    重试处理，而不是再提交一次。重复提交一次全量作业的代价远高于一次失败运行。
    """
    outcome = run_operation(
        db,
        run_id=run_id,
        operation_key=OPERATION_SUBMIT_JOB,
        action=lambda: _as_projection(plane.submit(command)),
    )
    if outcome.executed:
        return _from_projection(outcome.value or {})
    if outcome.status is RunOperationStatus.SUCCEEDED and outcome.value is not None:
        # 上一轮提交成功但进程死在写终态之前：沿用当时的度量继续收尾
        return _from_projection(outcome.value)
    return None


def _as_projection(outcome: ExecutionOutcome) -> dict[str, Any]:
    return {
        "read_rows": outcome.read_rows,
        "written_bytes": outcome.written_bytes,
        "rejected_rows": outcome.rejected_rows,
        "detail": outcome.detail,
    }


def _from_projection(payload: dict[str, Any]) -> ExecutionOutcome:
    return ExecutionOutcome(
        read_rows=int(payload.get("read_rows") or 0),
        written_bytes=int(payload.get("written_bytes") or 0),
        rejected_rows=int(payload.get("rejected_rows") or 0),
        detail=payload.get("detail"),
    )


# --------------------------------------------------------------------------
# 终态（全部经租约守卫，见 app/services/lease.py）
# --------------------------------------------------------------------------


def _finish(
    db: Session,
    run: ExecutionRun,
    *,
    worker_id: uuid.UUID,
    event_type: str,
    message: str,
    **values: Any,
) -> ExecutionRun:
    """带租约守卫地写终态。

    **先写终态、再写事件**：如果反过来，事件里会出现一条「已成功」而运行其实
    被 fencing 拒绝的流水——事件是给人看的，它必须如实反映发生了什么。
    这里的顺序保证：事件只在终态确实写成功之后才落。
    """
    values["finished_at"] = datetime.now(UTC)
    # 终态落定后租约随即失效：持有者不再需要它，别人也不该再续
    values["lease_expires_at"] = datetime.now(UTC)
    values["worker_lease_owner"] = None

    if not fenced_update(db, run_id=run.id, worker_id=worker_id, values=values):
        raise LeaseLost("写回终态被拒绝：本 Worker 已不是该运行的租约持有者")

    db.refresh(run)
    record_event(db, run, event_type, message=message)
    return run


def mark_succeeded(
    db: Session, run: ExecutionRun, outcome: ExecutionOutcome, *, worker_id: uuid.UUID
) -> ExecutionRun:
    finished = _finish(
        db,
        run,
        worker_id=worker_id,
        event_type=EVENT_SUCCEEDED,
        message="执行完成",
        status=ExecutionRunStatus.SUCCEEDED,
        error=None,
        read_rows=outcome.read_rows,
        written_bytes=outcome.written_bytes,
        rejected_rows=outcome.rejected_rows,
    )
    record_event(
        db,
        finished,
        EVENT_PROGRESS,
        message="数据面度量回传",
        output_snapshot=outcome.detail,
        metrics={
            "read_rows": outcome.read_rows,
            "written_bytes": outcome.written_bytes,
            "rejected_rows": outcome.rejected_rows,
        },
    )
    return finished


def mark_failed(
    db: Session, run: ExecutionRun, error: str, *, worker_id: uuid.UUID
) -> ExecutionRun:
    """把运行收敛为 ``failed``。

    **错误文案走受控路径**：调用方传入的应是稳定的分类文案，而不是底层异常
    的原始字符串——原始字符串可能带上连接信息，且会随依赖版本变化。
    """
    return _finish(
        db,
        run,
        worker_id=worker_id,
        event_type=EVENT_FAILED,
        message=error[:512],
        status=ExecutionRunStatus.FAILED,
        error=error[:512],
    )


def mark_cancelled(
    db: Session, run: ExecutionRun, *, worker_id: uuid.UUID, reason: str
) -> ExecutionRun:
    """协作式取消：由持有租约的 Worker 自己停在安全点上（FR-29）。"""
    return _finish(
        db,
        run,
        worker_id=worker_id,
        event_type=EVENT_CANCELLED,
        message=reason[:512],
        status=ExecutionRunStatus.CANCELLED,
        error=None,
    )


# --------------------------------------------------------------------------
# 幂等台账（FR-27）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationOutcome:
    """一次幂等操作的结果。``executed=False`` 表示本次没有真的执行副作用。"""

    status: RunOperationStatus
    executed: bool
    value: dict[str, Any] | None = None


def run_operation(
    db: Session,
    *,
    run_id: uuid.UUID,
    operation_key: str,
    action: Callable[[], dict[str, Any]],
) -> OperationOutcome:
    """执行一个**至多一次**的副作用。``action`` 必须返回 JSON 可序列化的投影。

    三种情形：

    - **无记录** → 先抢占台账再执行，成功则把投影存进台账；
    - **已有成功记录** → 不重放，返回台账里的投影（崩溃后重来也能拿到同样的结论）；
    - **已有失败 / 未完成记录** → 不重放，交调用方按「不可自动重试」处理。

    第三条是刻意的：对副作用而言「试过」本身就是事实，要不要重来取决于目标端
    是否留下残留——那需要人看一眼，不是自动化该猜的。
    """
    existing = db.scalar(
        select(RunOperation).where(
            RunOperation.run_id == run_id,
            RunOperation.operation_key == operation_key,
        )
    )
    if existing is not None:
        logger.info(
            "副作用此前已尝试，跳过 key=%s status=%s", operation_key, existing.status
        )
        stored = (existing.result or {}).get("value")
        return OperationOutcome(
            status=existing.status,
            executed=False,
            value=stored if isinstance(stored, dict) else None,
        )

    operation = RunOperation(
        run_id=run_id,
        operation_key=operation_key,
        status=RunOperationStatus.STARTED,
    )
    try:
        # 先抢占台账再执行：反过来会出现「已产生副作用但台账没记上」的窗口，
        # 那正是幂等台账要消除的东西。保存点让唯一键冲突只回滚这一步，
        # 不牵连调用方事务里已经写下的认领与事件。
        with db.begin_nested():
            db.add(operation)
            db.flush()
    except IntegrityError:
        # 同一运行的同一操作不可能被并发执行（认领是排他的），到这里说明
        # 上一轮已经登记过——按「不重放」处理。
        logger.warning("幂等台账登记冲突 key=%s", operation_key)
        return OperationOutcome(status=RunOperationStatus.STARTED, executed=False)

    try:
        value = action()
    except Exception as exc:  # noqa: BLE001 —— 失败也要留下「已尝试」的事实
        _complete_operation(db, operation, RunOperationStatus.FAILED, error=str(exc))
        raise

    _complete_operation(db, operation, RunOperationStatus.SUCCEEDED, result=value)
    return OperationOutcome(
        status=RunOperationStatus.SUCCEEDED, executed=True, value=value
    )


def _complete_operation(
    db: Session,
    operation: RunOperation,
    status: RunOperationStatus,
    *,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    operation.status = status
    operation.error = error[:512] if error else None
    operation.completed_at = datetime.now(UTC)
    if result is not None:
        # 投影同样过脱敏：它会被长期保存，和事件快照是同一类东西
        operation.result = {"value": redact_snapshot(result)}
    db.commit()


# --------------------------------------------------------------------------
# 协作式取消（FR-29）
# --------------------------------------------------------------------------


def request_cancel(db: Session, *, run: ExecutionRun, user_id: uuid.UUID) -> ExecutionRun:
    """请求取消一次运行。

    **不做强杀**：运行中的作业正在数据面上搬运数据，从控制面强杀会留下未知残留。
    正确做法是置一个标志，由持有租约的 Worker 在安全点上停下来。

    尚未开始（``pending``）的运行不需要协商——它还没有产生任何副作用，
    直接落到终态即可。
    """
    if run.is_terminal:
        raise ResourceConflict(f"运行已处于 {run.status.value}，无法取消")

    if run.status is ExecutionRunStatus.PENDING:
        run.status = ExecutionRunStatus.CANCELLED
        run.cancel_requested = True
        run.cancel_requested_at = datetime.now(UTC)
        run.finished_at = datetime.now(UTC)
        db.commit()
        db.refresh(run)
        record_event(
            db, run, EVENT_CANCELLED, message=f"由 {user_id} 取消（尚未开始执行）"
        )
        return run

    run.cancel_requested = True
    run.cancel_requested_at = datetime.now(UTC)
    db.commit()
    db.refresh(run)
    record_event(db, run, EVENT_CANCEL_REQUESTED, message=f"由 {user_id} 请求取消")
    return run


def cancel_requested(db: Session, run: ExecutionRun) -> bool:
    """重新读取取消标志。

    必须从库里读：取消请求来自另一个进程，当前进程内的对象不知道它。
    """
    db.refresh(run)
    return bool(run.cancel_requested)


# --------------------------------------------------------------------------
# 执行编排
# --------------------------------------------------------------------------


def run_execution(
    db: Session,
    *,
    run_id: uuid.UUID,
    worker_id: uuid.UUID,
    plane: DataPlane | None = None,
    on_event: Callable[[str], None] | None = None,
) -> ExecutionRun | None:
    """认领并推进一次执行。

    返回 ``None`` 表示本次调用没有抢到运行（重复投递），调用方应直接结束。
    """
    run = claim_run(db, run_id=run_id, worker_id=worker_id)
    if run is None:
        logger.info("运行已被认领或已结束，跳过 run=%s", run_id)
        return None

    record_event(db, run, EVENT_CLAIMED, message=f"由 Worker {worker_id} 认领")
    if on_event is not None:
        on_event(EVENT_CLAIMED)

    if cancel_requested(db, run):
        return mark_cancelled(db, run, worker_id=worker_id, reason="认领时已被请求取消")

    try:
        command = load_command(db, run.id)
        # 提交前续一次租约：真正耗时的动作马上开始，租约得先续上
        renew_lease(db, run_id=run.id, worker_id=worker_id)
        record_event(db, run, EVENT_SUBMITTED, input_snapshot=command.model_dump(mode="json"))
        if on_event is not None:
            on_event(EVENT_SUBMITTED)

        outcome = submit_job_once(
            db, run_id=run.id, command=command, plane=plane or get_data_plane()
        )
        if outcome is None:
            # 此前已尝试过提交且没有可用结论：不重复提交，交给人工核对目标端残留
            return mark_failed(
                db,
                run,
                f"{OPERATION_SUBMIT_JOB} 此前已尝试过且结论不明，不自动重放",
                worker_id=worker_id,
            )
    except DataplaneUnavailable as exc:
        # 数据面不可达是可预期的运维状态，用分类文案而不是异常原文
        return mark_failed(db, run, f"{exc.code.value}: {exc.message}", worker_id=worker_id)
    except LeaseLost:
        logger.warning("运行已失去租约，放弃推进 run=%s", run.id)
        raise
    except Exception as exc:  # noqa: BLE001 —— 任何异常都必须收敛，不能让运行悬在 running
        logger.exception("执行异常 run=%s", run.id, exc_info=exc)
        return mark_failed(
            db, run, "执行过程中发生未预期错误（详见服务端日志）", worker_id=worker_id
        )

    if cancel_requested(db, run):
        return mark_cancelled(
            db, run, worker_id=worker_id, reason="执行完成后收到取消请求，不发布结果"
        )

    return mark_succeeded(db, run, outcome, worker_id=worker_id)
