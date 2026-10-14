"""Commit 收口测试（FR-25 ~ FR-28）。

重点验证三件事：**审批未齐不放行**、**指纹对不上不放行**、
**受理时业务事实与投递同事务落库**。
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.errors import ErrorCode
from app.models.approval import ApprovalDecision, PreparationStatus
from app.models.execution import ExecutionRunStatus, OutboxStatus
from app.services.llm import FakeLLMClient
from app.services.outbox import backoff_seconds

PARSE_OK = json.dumps(
    {
        "understood": "把 orders 同步到 Doris",
        "source_table": "orders",
        "target_table": "orders_wide",
        "fields": ["order_id"],
        "filters": None,
        "schedule": None,
        "need_clarification": [],
    },
    ensure_ascii=False,
)

MATCH_OK = json.dumps(
    {
        "supported": True,
        "steps": [
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.Jdbc",
                "role": "source",
                "attributes": {
                    "driver": "com.mysql.cj.jdbc.Driver",
                    "query": "SELECT order_id FROM orders",
                },
            },
            {
                "step_id": "snk",
                "operator_id": "seatunnel.sink.Doris",
                "role": "sink",
                "attributes": {"database": "ods", "table": "orders_wide"},
            },
        ],
    },
    ensure_ascii=False,
)

PLAN = json.dumps(
    {
        "name": "订单同步",
        "description": "订单表同步",
        "source_table": "orders",
        "target_table": "orders_wide",
        "field_mappings": [
            {
                "source_field": "order_id",
                "target_field": "order_id",
                "transform": None,
                "sensitivity_level": "L2",
            }
        ],
        "unmapped_source_fields": [],
        "warnings": [],
    },
    ensure_ascii=False,
)

MAKER = "maker@example.com"
CHECKER_DATA = "checker.data@example.com"
CHECKER_SECURITY = "checker.security@example.com"
OPERATOR = "operator@example.com"


@pytest.fixture()
def stubs(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    from app.api.routes import pipelines as routes

    monkeypatch.setattr(routes, "get_checkpointer", lambda: InMemorySaver())

    def _install(responses: list[str]) -> None:
        monkeypatch.setattr(routes, "get_llm_client", lambda: FakeLLMClient(responses))

    return _install


@pytest.fixture()
def ctx(client: TestClient, auth_headers):
    """一个项目 + 四个角色账号（发起人 / 两个审批人 / 操作员）。"""
    maker = auth_headers(MAKER)
    project = client.post("/api/v1/projects", json={"name": "集成"}, headers=maker).json()

    headers = {"maker": maker}
    for email, role, key in (
        (CHECKER_DATA, "checker_data", "data"),
        (CHECKER_SECURITY, "checker_security", "security"),
        (OPERATOR, "operator", "operator"),
    ):
        checker = auth_headers(email)
        resp = client.post(
            f"/api/v1/projects/{project['id']}/members",
            json={"email": email, "role": role},
            headers=maker,
        )
        assert resp.status_code == 201, resp.text
        headers[key] = checker

    def _datasource(name: str, ds_type: str) -> str:
        return client.post(
            "/api/v1/datasources",
            json={
                "project_id": project["id"],
                "name": name,
                "type": ds_type,
                "config": {"host": "127.0.0.1"},
            },
            headers=maker,
        ).json()["id"]

    return {
        "headers": headers,
        "project_id": project["id"],
        "maker_headers": maker,
        "source_id": _datasource("源库", "mysql"),
        "target_id": _datasource("数仓", "doris"),
    }


# --------------------------------------------------------------------------
# 推进到「可 Commit」的辅助
# --------------------------------------------------------------------------


def _frozen_version(client: TestClient, ctx: dict, stubs) -> str:
    stubs([PARSE_OK, MATCH_OK, PLAN])
    resp = client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": "把 orders 同步到 Doris",
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
        headers=ctx["maker_headers"],
    )
    assert resp.status_code == 201, resp.text

    versions = client.get(
        f"/api/v1/versions?project_id={ctx['project_id']}", headers=ctx["maker_headers"]
    ).json()
    version_id = versions[0]["id"]
    frozen = client.post(
        f"/api/v1/versions/{version_id}/freeze", headers=ctx["maker_headers"]
    )
    assert frozen.status_code == 200, frozen.text
    return version_id


def _prepared(client: TestClient, ctx: dict, version_id: str) -> dict:
    resp = client.post(
        "/api/v1/preparations",
        json={"version_id": version_id},
        headers=ctx["maker_headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _slot(preparation: dict, dimension: str) -> dict:
    return next(a for a in preparation["approvals"] if a["dimension"] == dimension)


def _approve(client: TestClient, ctx: dict, preparation: dict, dimensions=("data", "security")):
    for dimension in dimensions:
        resp = client.post(
            f"/api/v1/approvals/{_slot(preparation, dimension)['id']}/decide",
            json={"decision": ApprovalDecision.APPROVED.value},
            headers=ctx["headers"][dimension],
        )
        assert resp.status_code == 200, resp.text
    return client.get(
        f"/api/v1/preparations/{preparation['id']}", headers=ctx["maker_headers"]
    ).json()


def _ready(client: TestClient, ctx: dict, stubs) -> dict:
    """走完 生成 → 冻结 → Prepare → 双维审批，返回已批准的准备单。"""
    version_id = _frozen_version(client, ctx, stubs)
    preparation = _prepared(client, ctx, version_id)
    return _approve(client, ctx, preparation)


def _commit(client: TestClient, ctx: dict, preparation: dict, fingerprint: str | None = None):
    return client.post(
        "/api/v1/commits",
        json={
            "preparation_id": preparation["id"],
            "expected_fingerprint": fingerprint or preparation["fingerprint"],
        },
        headers=ctx["headers"]["operator"],
    )


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------


def test_commit_accepted_after_dual_approval(client: TestClient, ctx, stubs):
    preparation = _ready(client, ctx, stubs)
    resp = _commit(client, ctx, preparation)

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == ExecutionRunStatus.PENDING.value
    assert body["artifact"] == preparation["fingerprint"]
    assert body["preparation_id"] == preparation["id"]
    # 不返回令牌：它随 Outbox 交给执行层，不进入面向用户的响应
    assert "capability_token" not in body


def test_commit_links_run_capability_and_outbox(
    client: TestClient, ctx, stubs, session_factory
):
    """一次受理同时落三样东西，且互相指得对。"""
    from sqlalchemy import select

    from app.models.capability import Capability
    from app.models.execution import ExecutionRun, OutboxEvent

    preparation = _ready(client, ctx, stubs)
    run_id = _commit(client, ctx, preparation).json()["run_id"]

    with session_factory() as db:
        run = db.get(ExecutionRun, uuid.UUID(run_id))
        capability = db.scalar(select(Capability))
        event = db.scalar(select(OutboxEvent))

        assert run.capability_id == capability.id
        assert capability.preparation_id == uuid.UUID(preparation["id"])
        assert capability.bound_artifact == preparation["fingerprint"]
        assert capability.consumed is False

        # 幂等键由 run_id 构成：同一件事只应投递一次
        assert event.idempotency_key == f"execution-run:{run.id}"
        assert event.status is OutboxStatus.PENDING
        assert event.attempts == 0
        assert event.payload["run_id"] == str(run.id)
        assert event.payload["capability_id"] == str(capability.id)


def test_outbox_payload_binds_the_execution_tool(client: TestClient, ctx, stubs, session_factory):
    """载荷要能让执行层独立回答「执行什么、在哪执行、凭什么执行」。"""
    from sqlalchemy import select

    from app.models.execution import OutboxEvent
    from app.services.commit import EXECUTION_TOOL

    preparation = _ready(client, ctx, stubs)
    _commit(client, ctx, preparation)

    with session_factory() as db:
        payload = db.scalar(select(OutboxEvent)).payload

    assert payload["tool"] == EXECUTION_TOOL
    assert payload["environment"] == "dev"
    assert payload["artifact"] == preparation["fingerprint"]
    assert payload["shadow_table"] == preparation["impact_scope"]["shadow_table"]
    assert payload["capability_token"]


def test_issued_capability_is_single_use(client: TestClient, ctx, stubs, session_factory):
    """签发出来的令牌必须真的只能用一次（FR-27 与 M6 的接缝）。"""
    from sqlalchemy import select

    from app.core.errors import CapabilityConsumed
    from app.models.execution import OutboxEvent
    from app.schemas.capability import BindingContext
    from app.services.capability import verify_capability

    preparation = _ready(client, ctx, stubs)
    _commit(client, ctx, preparation)

    with session_factory() as db:
        payload = db.scalar(select(OutboxEvent)).payload
        binding = BindingContext(
            tool=payload["tool"],
            subject=payload["subject"],
            environment=payload["environment"],
            artifact=payload["artifact"],
        )

        verify_capability(db, payload["capability_token"], binding, consume=True)
        with pytest.raises(CapabilityConsumed):
            verify_capability(db, payload["capability_token"], binding, consume=True)


def test_preparation_becomes_committed(client: TestClient, ctx, stubs):
    preparation = _ready(client, ctx, stubs)
    _commit(client, ctx, preparation)

    after = client.get(
        f"/api/v1/preparations/{preparation['id']}", headers=ctx["maker_headers"]
    ).json()
    assert after["status"] == PreparationStatus.COMMITTED.value
    assert after["committed_at"] is not None


def test_run_is_queryable(client: TestClient, ctx, stubs):
    preparation = _ready(client, ctx, stubs)
    run_id = _commit(client, ctx, preparation).json()["run_id"]

    resp = client.get(f"/api/v1/runs/{run_id}", headers=ctx["headers"]["operator"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == ExecutionRunStatus.PENDING.value

    listed = client.get(
        f"/api/v1/runs?project_id={ctx['project_id']}", headers=ctx["headers"]["operator"]
    ).json()
    assert [item["id"] for item in listed] == [run_id]


# --------------------------------------------------------------------------
# 复核：审批（FR-25）
# --------------------------------------------------------------------------


def test_commit_rejected_when_one_dimension_missing(client: TestClient, ctx, stubs):
    version_id = _frozen_version(client, ctx, stubs)
    preparation = _prepared(client, ctx, version_id)
    _approve(client, ctx, preparation, dimensions=("data",))

    resp = _commit(client, ctx, preparation)
    assert resp.status_code == 403
    assert resp.json()["code"] == ErrorCode.POL_APPROVAL_INCOMPLETE.value
    assert resp.json()["detail"]["missing_dimensions"] == ["安全维度"]


def test_commit_rejected_when_dimension_rejected(client: TestClient, ctx, stubs):
    version_id = _frozen_version(client, ctx, stubs)
    preparation = _prepared(client, ctx, version_id)
    _approve(client, ctx, preparation, dimensions=("data",))
    client.post(
        f"/api/v1/approvals/{_slot(preparation, 'security')['id']}/decide",
        json={"decision": "rejected", "comment": "影子表命名需确认"},
        headers=ctx["headers"]["security"],
    )

    resp = _commit(client, ctx, preparation)
    assert resp.status_code == 403
    assert resp.json()["code"] == ErrorCode.POL_APPROVAL_INCOMPLETE.value


def test_commit_rejected_when_approval_expired(
    client: TestClient, ctx, stubs, session_factory
):
    """审批通过时在时限内，Commit 时已过期——授权不再成立。"""
    from sqlalchemy import select

    from app.models.approval import Approval, Preparation

    preparation = _ready(client, ctx, stubs)

    with session_factory() as db:
        record = db.scalar(
            select(Preparation).where(Preparation.id == uuid.UUID(preparation["id"]))
        )
        record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        for approval in db.scalars(
            select(Approval).where(Approval.preparation_id == record.id)
        ):
            approval.expires_at = record.expires_at
        db.commit()

    resp = _commit(client, ctx, preparation)
    assert resp.status_code == 403
    assert resp.json()["code"] == ErrorCode.POL_APPROVAL_INCOMPLETE.value


def test_rejected_commit_leaves_no_trace(client: TestClient, ctx, stubs, session_factory):
    """复核不过不得留下任何半成品：没有运行、没有令牌、没有待投递事件。"""
    from sqlalchemy import func, select

    from app.models.capability import Capability
    from app.models.execution import ExecutionRun, OutboxEvent

    version_id = _frozen_version(client, ctx, stubs)
    preparation = _prepared(client, ctx, version_id)
    _approve(client, ctx, preparation, dimensions=("data",))

    assert _commit(client, ctx, preparation).status_code == 403

    with session_factory() as db:
        for model in (ExecutionRun, Capability, OutboxEvent):
            assert db.scalar(select(func.count()).select_from(model)) == 0


# --------------------------------------------------------------------------
# 复核：指纹（FR-25）
# --------------------------------------------------------------------------


def test_commit_rejected_when_client_fingerprint_is_stale(client: TestClient, ctx, stubs):
    preparation = _ready(client, ctx, stubs)
    resp = _commit(client, ctx, preparation, fingerprint="0" * 64)

    assert resp.status_code == 409
    assert resp.json()["code"] == ErrorCode.BIZ_FINGERPRINT_MISMATCH.value


def test_commit_rejected_when_version_content_tampered(
    client: TestClient, ctx, stubs, session_factory
):
    """指纹是复算出来的，不是比字符串——版本内容被改写必须被发现。"""
    from sqlalchemy import select

    from app.models.version import PipelineVersion

    preparation = _ready(client, ctx, stubs)

    with session_factory() as db:
        version = db.scalar(
            select(PipelineVersion).where(
                PipelineVersion.id == uuid.UUID(preparation["version_id"])
            )
        )
        plan = dict(version.etl_plan)
        plan["target_table"] = "orders_wide_backdoor"
        version.etl_plan = plan
        db.commit()

    resp = _commit(client, ctx, preparation)
    assert resp.status_code == 409
    assert resp.json()["code"] == ErrorCode.BIZ_FINGERPRINT_MISMATCH.value
    assert "被改写" in resp.json()["message"]


def test_commit_rejected_when_preparation_fingerprint_altered(
    client: TestClient, ctx, stubs, session_factory
):
    """准备单背书的是另一份制品——审批就不再指向任何东西。"""
    from sqlalchemy import select

    from app.models.approval import Preparation

    preparation = _ready(client, ctx, stubs)

    with session_factory() as db:
        record = db.scalar(
            select(Preparation).where(Preparation.id == uuid.UUID(preparation["id"]))
        )
        record.fingerprint = "f" * 64
        db.commit()

    resp = _commit(client, ctx, preparation, fingerprint="f" * 64)
    assert resp.status_code == 409
    assert resp.json()["code"] == ErrorCode.BIZ_FINGERPRINT_MISMATCH.value


# --------------------------------------------------------------------------
# 幂等与准入
# --------------------------------------------------------------------------


def test_second_commit_is_rejected(client: TestClient, ctx, stubs):
    """重复提交必须报错而不是「看起来又成功了一次」——令牌是单次的。"""
    preparation = _ready(client, ctx, stubs)
    assert _commit(client, ctx, preparation).status_code == 202

    again = _commit(client, ctx, preparation)
    assert again.status_code == 409
    assert again.json()["code"] == ErrorCode.BIZ_RESOURCE_CONFLICT.value


def test_commit_requires_operator_role(client: TestClient, ctx, stubs):
    """操作员是被授予「放行」的那个人；Maker 与审批人都不是。"""
    preparation = _ready(client, ctx, stubs)

    resp = client.post(
        "/api/v1/commits",
        json={
            "preparation_id": preparation["id"],
            "expected_fingerprint": preparation["fingerprint"],
        },
        headers=ctx["headers"]["data"],
    )
    assert resp.status_code == 403


def test_commit_requires_authentication(client: TestClient, ctx, stubs):
    preparation = _ready(client, ctx, stubs)
    resp = client.post(
        "/api/v1/commits",
        json={
            "preparation_id": preparation["id"],
            "expected_fingerprint": preparation["fingerprint"],
        },
    )
    assert resp.status_code == 401


def test_commit_is_project_scoped(client: TestClient, ctx, auth_headers, stubs):
    preparation = _ready(client, ctx, stubs)
    outsider = auth_headers("outsider@example.com")

    resp = client.post(
        "/api/v1/commits",
        json={
            "preparation_id": preparation["id"],
            "expected_fingerprint": preparation["fingerprint"],
        },
        headers=outsider,
    )
    assert resp.status_code == 403


def test_pdp_denial_blocks_commit(client: TestClient, ctx, stubs, monkeypatch, session_factory):
    """策略许可 ≠ 可跳过审批，反过来也成立：策略拒绝就是拒绝。"""
    from dataclasses import dataclass

    from sqlalchemy import func, select

    from app.models.execution import ExecutionRun
    from app.services import commit as commit_service
    from app.services.pdp import Decision, PolicyDecisionPoint

    @dataclass
    class _AlwaysDeny:
        name = "test-deny"

        def evaluate(self, request):
            return Decision.deny("测试策略：一律拒绝", ErrorCode.POL_RISK_BLOCKED)

    monkeypatch.setattr(
        commit_service, "get_pdp", lambda: PolicyDecisionPoint([_AlwaysDeny()])
    )

    preparation = _ready(client, ctx, stubs)
    resp = _commit(client, ctx, preparation)

    assert resp.status_code == 403
    assert resp.json()["code"] == ErrorCode.POL_RISK_BLOCKED.value

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(ExecutionRun)) == 0


def test_execution_tool_is_registered_as_high_risk():
    """执行工具在装配期就被登记为高风险：必须绑定具体制品才放行。"""
    from app.main import app  # noqa: F401 —— 导入即完成策略装配
    from app.services.commit import EXECUTION_TOOL
    from app.services.pdp import get_pdp

    policies = get_pdp().policies
    high_risk = [
        policy for policy in policies if getattr(policy, "high_risk_tools", set())
    ]
    assert any(EXECUTION_TOOL in policy.high_risk_tools for policy in high_risk)


# --------------------------------------------------------------------------
# Outbox 退避
# --------------------------------------------------------------------------


def test_backoff_grows_then_caps():
    from app.core.config import settings

    assert backoff_seconds(1) == settings.outbox_backoff_base_seconds
    assert backoff_seconds(2) == settings.outbox_backoff_base_seconds * 2
    assert backoff_seconds(20) == settings.outbox_backoff_max_seconds


# --------------------------------------------------------------------------
# 运行控制（API-09）
# --------------------------------------------------------------------------


def test_operator_can_cancel_a_pending_run(client: TestClient, ctx, stubs):
    """受理但尚未开跑的运行没有副作用需要收尾，取消即刻生效。"""
    preparation = _ready(client, ctx, stubs)
    run_id = _commit(client, ctx, preparation).json()["run_id"]

    resp = client.post(
        f"/api/v1/runs/{run_id}/cancel", headers=ctx["headers"]["operator"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == ExecutionRunStatus.CANCELLED.value
    assert body["finished_at"] is not None


def test_cancel_requires_operator_role(client: TestClient, ctx, stubs):
    preparation = _ready(client, ctx, stubs)
    run_id = _commit(client, ctx, preparation).json()["run_id"]

    resp = client.post(f"/api/v1/runs/{run_id}/cancel", headers=ctx["maker_headers"])
    assert resp.status_code == 403


def test_runs_are_project_scoped(client: TestClient, ctx, auth_headers, stubs):
    preparation = _ready(client, ctx, stubs)
    run_id = _commit(client, ctx, preparation).json()["run_id"]

    outsider = auth_headers("outsider@example.com")
    assert client.get(f"/api/v1/runs/{run_id}", headers=outsider).status_code == 403
    assert (
        client.post(f"/api/v1/runs/{run_id}/cancel", headers=outsider).status_code == 403
    )
