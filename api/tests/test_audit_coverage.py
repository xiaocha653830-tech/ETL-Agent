"""T-20：关键事件的账本覆盖率（PRD「Prepare / Approve / Commit / 执行 / 回滚 覆盖率 100%」）。

覆盖率是个**量化指标**，所以这里用表驱动断言，而不是「看起来都记上了」。
将来新增一类关键动作却忘了接进账本，只要清单更新了而接入没跟上，清单那条断言
就会失败。

这一组走服务层而不是 HTTP：覆盖率关心的是「接入点有没有被走到」，而 HTTP 那一层
已经有各自的测试。``commit_preparation`` 需要真实签名密钥与 PDP 装配，其覆盖率
由 ``test_commits.py`` 的端到端链路承担，这里只断言它在清单里。

夹具只交出 **ID**，不在会话之间传递 ORM 对象——``commit()`` 会让对象过期，
跨会话读属性只会拿到 ``DetachedInstanceError``。
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select

from app.core.errors import SelfApprovalDenied
from app.core.keys import SigningKey
from app.models.approval import ApprovalDecision, ApprovalDimension
from app.models.audit import AuditRecord
from app.models.execution import ExecutionRun, ExecutionRunStatus
from app.models.identity import Membership, Project, User, UserRole
from app.models.version import PipelineVersion, VersionStatus
from app.schemas.execution import ExecutionCommand
from app.services import audit
from app.services.approval import create_preparation, decide
from app.services.audit import (
    EVENT_APPROVAL_DECIDED,
    EVENT_APPROVAL_DENIED,
    EVENT_PREPARE_CREATED,
    EVENT_ROLLBACK_EXECUTED,
)
from app.services.commit import EXECUTION_TOOL
from app.services.execution import (
    ExecutionOutcome,
    claim_run,
    mark_failed,
    mark_succeeded,
)
from app.services.outbox import EVENT_RUN_REQUESTED, enqueue, execution_run_key
from app.services.rollback import rollback_run

ARTIFACT = "a" * 64

# PRD 第 5 章点名的关键事件。**这张清单就是验收口径。**
CRITICAL_EVENT_TYPES = frozenset(
    {
        "prepare.created",
        "prepare.reissued",
        "approval.decided",
        "commit.accepted",
        "execution.claimed",
        "execution.submitted",
        "execution.succeeded",
        "execution.failed",
        "rollback.executed",
    }
)


@pytest.fixture(autouse=True)
def _isolated_signing_key(monkeypatch):
    """账本签名不碰凭据后端（否则会在工作区留下 .secrets.enc）。"""
    key = SigningKey("coverage-key", Ed25519PrivateKey.generate())
    monkeypatch.setattr(audit, "get_signing_key", lambda: key)
    return key


@pytest.fixture()
def world(session_factory):
    """可走 Prepare → 双维审批 → 执行 → 回滚 的一组最小对象（只交出 ID）。"""
    with session_factory() as db:
        maker = User(email="maker@example.com", hashed_password="x")
        data_checker = User(email="data@example.com", hashed_password="x")
        security_checker = User(email="security@example.com", hashed_password="x")
        operator = User(email="operator@example.com", hashed_password="x")
        project = Project(name="覆盖率", tenant_id="default")
        db.add_all([maker, data_checker, security_checker, operator, project])
        db.flush()
        db.add_all(
            [
                Membership(
                    user_id=maker.id, project_id=project.id, role=UserRole.MAKER
                ),
                Membership(
                    user_id=data_checker.id,
                    project_id=project.id,
                    role=UserRole.CHECKER_DATA,
                ),
                Membership(
                    user_id=security_checker.id,
                    project_id=project.id,
                    role=UserRole.CHECKER_SECURITY,
                ),
                Membership(
                    user_id=operator.id, project_id=project.id, role=UserRole.OPERATOR
                ),
            ]
        )
        version = PipelineVersion(
            project_id=project.id,
            created_by=maker.id,
            version_no=1,
            status=VersionStatus.FROZEN,
            sha256=ARTIFACT,
            etl_plan={
                "target_table": "orders_wide",
                "source_table": "orders",
                "steps": [],
                "target_datasource_id": str(uuid.uuid4()),
            },
            config="",
            quality_rules=[],
            frozen_at=datetime.now(UTC),
        )
        db.add(version)
        db.commit()
        return SimpleNamespace(
            project_id=project.id,
            version_id=version.id,
            maker_id=maker.id,
            data_checker_id=data_checker.id,
            operator_id=operator.id,
        )


def _membership(db, world, who: str) -> Membership:
    user_id = {
        "maker": world.maker_id,
        "data": world.data_checker_id,
        "operator": world.operator_id,
    }[who]
    return db.scalar(
        select(Membership).where(
            Membership.user_id == user_id, Membership.project_id == world.project_id
        )
    )


def _types(db) -> list[str]:
    return [
        str(item)
        for item in db.scalars(select(AuditRecord.event_type).order_by(AuditRecord.seq))
    ]


def _make_run(db, world) -> uuid.UUID:
    """造一个「已 Commit 待执行」的运行，连同它的 Outbox 命令。

    回滚要经 ``load_command`` 取回当初那份已批准的执行命令——这也是「回滚删哪张表
    不由调用方决定」的实现方式，所以测试必须把它一并造出来。
    """
    version = db.get(PipelineVersion, world.version_id)
    preparation = create_preparation(db, version=version, user_id=world.maker_id)
    run = ExecutionRun(
        project_id=world.project_id,
        version_id=version.id,
        preparation_id=preparation.id,
        created_by=world.operator_id,
        status=ExecutionRunStatus.PENDING,
        environment="dev",
        artifact=ARTIFACT,
    )
    db.add(run)
    db.flush()
    command = ExecutionCommand(
        run_id=run.id,
        version_id=version.id,
        preparation_id=preparation.id,
        capability_token="header.payload.signature",
        capability_id=uuid.uuid4(),
        tool=EXECUTION_TOOL,
        subject=str(world.operator_id),
        environment="dev",
        artifact=ARTIFACT,
        target_table="orders_wide",
        shadow_table="orders_wide__shadow_v1",
        budget={"max_rows": 1000},
    )
    enqueue(
        db,
        idempotency_key=execution_run_key(run.id),
        event_type=EVENT_RUN_REQUESTED,
        payload=command.model_dump(mode="json"),
    )
    db.commit()
    return run.id


def test_prepare_and_both_approvals_reach_the_ledger(session_factory, world):
    with session_factory() as db:
        version = db.get(PipelineVersion, world.version_id)
        preparation = create_preparation(db, version=version, user_id=world.maker_id)

        # 两条槽位里只签数据维度：安全维度必须由安全审批人签，而覆盖率关心的是
        # 「这个接入点被走到没有」，不是审批能否集齐。
        data_slot = next(
            item
            for item in preparation.approvals
            if item.dimension is ApprovalDimension.DATA
        )
        decide(
            db,
            approval=data_slot,
            membership=_membership(db, world, "data"),
            user_id=world.data_checker_id,
            decision=ApprovalDecision.APPROVED,
            comment="覆盖率测试",
        )

        types = _types(db)

    assert types.count(EVENT_PREPARE_CREATED) == 1
    assert types.count(EVENT_APPROVAL_DECIDED) == 1


def test_a_denied_approval_reaches_the_ledger(session_factory, world):
    """**被拒的尝试也必须留下证据。**

    它对应的业务动作没有发生（审批状态不变），因此这条记录不会随任何业务变更
    一起提交——``_record_denial`` 于是自己提交。否则「谁试过自批」在事后完全
    查不到，而那恰恰是最该被看见的一类动作。
    """
    with session_factory() as db:
        version = db.get(PipelineVersion, world.version_id)
        preparation = create_preparation(db, version=version, user_id=world.maker_id)
        data_slot = next(
            item
            for item in preparation.approvals
            if item.dimension is ApprovalDimension.DATA
        )

        # 申请人拿数据审批人的成员身份去审自己提交的版本 —— 被职责分离拦下
        with pytest.raises(SelfApprovalDenied):
            decide(
                db,
                approval=data_slot,
                membership=_membership(db, world, "data"),
                user_id=world.maker_id,
                decision=ApprovalDecision.APPROVED,
            )

    with session_factory() as db:
        denials = [
            record
            for record in db.scalars(select(AuditRecord).order_by(AuditRecord.seq))
            if record.event_type == EVENT_APPROVAL_DENIED
        ]

    assert len(denials) == 1
    assert denials[0].payload["reason"] == "self_approval"


def test_execution_terminal_state_reaches_the_ledger(session_factory, world):
    worker = uuid.uuid4()
    with session_factory() as db:
        run_id = _make_run(db, world)

    with session_factory() as db:
        claim_run(db, run_id=run_id, worker_id=worker)
        mark_succeeded(
            db,
            db.get(ExecutionRun, run_id),
            ExecutionOutcome(
                read_rows=8, written_bytes=64, rejected_rows=0, published=True
            ),
            worker_id=worker,
        )

    with session_factory() as db:
        records = list(db.scalars(select(AuditRecord).order_by(AuditRecord.seq)))
        terminal = [
            record
            for record in records
            if record.event_type.startswith("execution.")
            and record.event_type.split(".")[1] in {"succeeded", "failed", "cancelled"}
        ]

    assert [record.event_type for record in terminal] == ["execution.succeeded"]
    assert terminal[0].actor == f"worker:{worker}"
    assert terminal[0].payload["published"] is True
    assert terminal[0].payload["read_rows"] == 8
    # 认领（execution.claimed）与提交（execution.submitted）的接入点在
    # ``run_execution`` 的编排路径上，本用例直接调 ``claim_run`` 因此不经过它们——
    # 这里不假装覆盖到了。它们的**事件类型存在**由最后一个用例的清单自检保证。
    assert EVENT_PREPARE_CREATED in [record.event_type for record in records]


def test_rollback_reaches_the_ledger(session_factory, world):
    class _Publisher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def discard_shadow(self) -> list[str]:
            return ["orders_wide__shadow_v1"]

        def close(self) -> None:
            pass

    worker = uuid.uuid4()
    with session_factory() as db:
        run_id = _make_run(db, world)

    # 造出「已收敛但未发布」的残留：回滚清理的正是这种
    with session_factory() as db:
        claim_run(db, run_id=run_id, worker_id=worker)
        mark_failed(db, db.get(ExecutionRun, run_id), "数据面不可用", worker_id=worker)

    with session_factory() as db:
        outcome = rollback_run(
            db,
            run=db.get(ExecutionRun, run_id),
            actor=f"user:{world.operator_id}",
            publisher_factory=lambda *_a, **_k: _Publisher(),
        )
        assert outcome.executed is True

    with session_factory() as db:
        executed = [
            record
            for record in db.scalars(select(AuditRecord).order_by(AuditRecord.seq))
            if record.event_type == EVENT_ROLLBACK_EXECUTED
        ]

    assert len(executed) == 1
    assert executed[0].payload["dropped"] == ["orders_wide__shadow_v1"]
    assert executed[0].run_id == run_id


def test_every_critical_event_has_an_instrumented_event_type(world):
    """清单自检：PRD 点名的关键事件都在代码里定义过。

    这一条防的是「清单更新了、接入点没跟上」——它是覆盖率 100% 这个说法的下限
    保证。真正跑出来多少条由上面几个用例分别断言。
    """
    defined = {
        value
        for name, value in vars(audit).items()
        if name.startswith("EVENT_") and isinstance(value, str)
    }
    # 执行终态按状态拼出，不在上面的常量集合里
    defined |= {"execution.succeeded", "execution.failed", "execution.cancelled"}

    assert CRITICAL_EVENT_TYPES <= defined
