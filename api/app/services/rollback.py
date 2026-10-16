"""受管回滚（M10，FR-38 / API-10）。

**回滚执行的是准备单里已经批准过的那份方案**，而不是调用方临时提出的动作。
``build_rollback_plan`` 在 Prepare 时就把 ``strategy = "discard-shadow"`` 写进了
准备单，审批人背书的是这一条：

    丢弃未发布的影子表残留并释放资源，正式表保持不变

因此 API-10 没有能力做别的事——它没有接收目标表名，也没有接收动作名。目标库、
正式表、影子表全部来自**该运行当初那份已冻结、已批准的执行命令**（Outbox 载荷），
调用方只能指出「哪一次运行」，不能指出「删哪张表」。

**为什么必须先问「换过表没有」**：``swap=true`` 之后，影子表名下躺着的是被换下来
的旧正式表。此时「丢弃影子表残留」不再是清理，而是删掉上一版生产数据。所以回滚
的第一件事是核对这个版本的影子表名下现在是谁的数据；一旦有过发布，回滚直接拒绝，
而不是把一次数据删除伪装成一次清理。

**副作用走幂等台账**（FR-27）：重复点击回滚不会删第二次，只会拿回上一次的结论。
这条约束不是礼貌，而是必需的——``DROP TABLE`` 没有撤回。
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ResourceConflict, RollbackUnavailable
from app.models.execution import ExecutionRun, RunOperationStatus
from app.schemas.execution import ExecutionCommand
from app.services.audit import (
    EVENT_ROLLBACK_EXECUTED,
    EVENT_ROLLBACK_SKIPPED,
    record_audit,
)
from app.services.audit_verifier import assert_ledger_healthy
from app.services.credentials import CredentialService, get_credential_service
from app.services.execution import (
    EVENT_ROLLED_BACK,
    load_command,
    record_event,
    run_operation,
)
from app.services.quality import DorisPublisher

logger = logging.getLogger(__name__)

# 幂等台账的键：丢弃未发布的影子表残留
OPERATION_DISCARD_SHADOW = "data-plane.discard-shadow"

# 删除目标表的执行体工厂：注入以便测试隔离真实 Doris
PublisherFactory = Callable[
    [Session, ExecutionCommand, CredentialService], "DorisPublisher"
]


@dataclass(frozen=True)
class RollbackOutcome:
    """一次受管回滚的结果。"""

    run_id: uuid.UUID
    executed: bool
    dropped: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "executed": self.executed,
            "dropped": list(self.dropped),
            "reason": self.reason,
        }


def _assert_shadow_is_disposable(db: Session, run: ExecutionRun) -> None:
    """核对影子表名下现在是谁的数据。

    影子表名由版本决定（``<正式表>__shadow_v<n>``），同一版本的多次运行共用它。
    只要这个版本上有过任何一次发布，该名字下就可能是被换下来的旧正式表——
    于是回滚拒绝，而不是删掉它。
    """
    published = db.scalar(
        select(ExecutionRun.id)
        .where(
            ExecutionRun.version_id == run.version_id,
            ExecutionRun.published_at.is_not(None),
        )
        .limit(1)
    )
    if published is not None:
        raise RollbackUnavailable(
            "该版本的影子表名下已是某次成功发布换下来的旧正式表，"
            "受管回滚只清理未发布的残留，不撤销已生效的发布"
        )


def rollback_run(
    db: Session,
    *,
    run: ExecutionRun,
    actor: str,
    publisher_factory: PublisherFactory | None = None,
    credentials: CredentialService | None = None,
) -> RollbackOutcome:
    """清理一次未发布运行的影子表残留（FR-38）。

    调用方负责角色与租户校验。副作用经幂等台账，重复调用返回上一次的结论。
    """
    # 回滚是删数据动作，与 Commit 同理必须以可信的证据链为前提（FR-39）
    assert_ledger_healthy(db)

    if not run.is_terminal:
        # 运行中的作业还在往影子表里写，此时删表等于把数据写到一张正在消失的表上。
        raise ResourceConflict("运行尚未结束，请先取消并等待其收敛，再执行回滚")

    _assert_shadow_is_disposable(db, run)

    factory = publisher_factory or DorisPublisher
    service = credentials or get_credential_service()
    command = load_command(db, run.id)

    def discard() -> dict[str, Any]:
        publisher = factory(db, command, service)
        try:
            dropped = publisher.discard_shadow()
        finally:
            publisher.close()
        return {
            "dropped": dropped,
            "target_table": command.target_table,
            "shadow_table": command.shadow_table,
        }

    outcome = run_operation(
        db,
        run_id=run.id,
        operation_key=OPERATION_DISCARD_SHADOW,
        action=discard,
    )

    if outcome.status is not RunOperationStatus.SUCCEEDED:
        # 台账上有记录却没有可用结论：上一轮已经动过手，但不知道删到哪一步了。
        # 不重放——``DROP TABLE`` 的「试过」本身就是必须由人来看的事实。
        record_audit(
            db,
            event_type=EVENT_ROLLBACK_SKIPPED,
            actor=actor,
            project_id=run.project_id,
            run_id=run.id,
            payload={"reason": "此前已尝试回滚且结论不明，未重复执行"},
        )
        record_event(
            db,
            run,
            EVENT_ROLLED_BACK,
            message="此前已尝试回滚且结论不明，未重复执行，需人工核对目标端残留",
        )
        return RollbackOutcome(
            run_id=run.id,
            executed=False,
            dropped=(),
            reason="此前已尝试回滚且结论不明，未重复执行",
        )

    dropped = tuple(str(name) for name in (outcome.value or {}).get("dropped") or ())
    if run.rolled_back_at is None:
        run.rolled_back_at = datetime.now(UTC)
        db.commit()
        db.refresh(run)

    if outcome.executed:
        record_audit(
            db,
            event_type=EVENT_ROLLBACK_EXECUTED,
            actor=actor,
            project_id=run.project_id,
            run_id=run.id,
            payload={"dropped": list(dropped), "reason": "丢弃未发布的影子表残留"},
        )
        record_event(
            db,
            run,
            EVENT_ROLLED_BACK,
            message=f"已丢弃影子表残留：{', '.join(dropped) or '无需清理'}",
            input_snapshot={"actor": actor},
            output_snapshot=dict(outcome.value or {}),
        )
        logger.info("受管回滚完成 run=%s dropped=%s", run.id, list(dropped))
    else:
        logger.info("受管回滚此前已完成，直接返回既有结论 run=%s", run.id)

    return RollbackOutcome(
        run_id=run.id,
        executed=bool(outcome.executed),
        dropped=dropped,
        reason="已丢弃影子表残留" if dropped else "影子表不存在，无需清理",
    )
