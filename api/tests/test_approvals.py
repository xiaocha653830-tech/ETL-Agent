"""Prepare 准备单与双维审批测试（FR-21 ~ FR-24）。"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.models.approval import ApprovalDimension, PreparationStatus, RiskLevel
from app.models.version import PipelineVersion
from app.services.approval import rate_risk
from app.services.llm import FakeLLMClient

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


def _plan(*, level: str = "L2", sink: str = "seatunnel.sink.Doris") -> str:
    return json.dumps(
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
                    "sensitivity_level": level,
                }
            ],
            "unmapped_source_fields": [],
            "warnings": [],
        },
        ensure_ascii=False,
    )


PLAN = _plan()


@pytest.fixture()
def stubs(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver

    from app.api.routes import pipelines as routes

    monkeypatch.setattr(routes, "get_checkpointer", lambda: InMemorySaver())

    def _install(responses: list[str]) -> None:
        monkeypatch.setattr(routes, "get_llm_client", lambda: FakeLLMClient(responses))

    return _install


MAKER = "maker@example.com"
CHECKER_DATA = "checker.data@example.com"
CHECKER_SECURITY = "checker.security@example.com"


@pytest.fixture()
def ctx(client: TestClient, auth_headers, stubs):
    """一个项目 + 三角色账号（发起人 / 数据审批人 / 安全审批人）。"""
    maker = auth_headers(MAKER)
    project = client.post(
        "/api/v1/projects", json={"name": "集成"}, headers=maker
    ).json()

    headers = {"maker": maker}
    for email, role, key in (
        (CHECKER_DATA, "checker_data", "data"),
        (CHECKER_SECURITY, "checker_security", "security"),
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


def _freeze(client: TestClient, ctx: dict, stubs, plan: str = PLAN) -> str:
    """生成 → 冻结，返回可提交审批的版本 id。"""
    stubs([PARSE_OK, MATCH_OK, plan])
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
        f"/api/v1/versions?project_id={ctx['project_id']}",
        headers=ctx["maker_headers"],
    ).json()
    version_id = versions[0]["id"]

    frozen = client.post(
        f"/api/v1/versions/{version_id}/freeze", headers=ctx["maker_headers"]
    )
    assert frozen.status_code == 200, frozen.text
    return version_id


def _prepare(client: TestClient, ctx: dict, version_id: str) -> dict:
    resp = client.post(
        "/api/v1/preparations",
        json={"version_id": version_id},
        headers=ctx["maker_headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _slot(preparation: dict, dimension: str) -> dict:
    return next(a for a in preparation["approvals"] if a["dimension"] == dimension)


def _decide(client: TestClient, ctx: dict, approval_id: str, headers, decision: str) -> dict:
    return client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": decision},
        headers=headers,
    )


# --------------------------------------------------------------------------
# 风险评级（FR-21）—— 纯函数，确定性
# --------------------------------------------------------------------------


def _version(*, level: str, sink: str, source: str = "seatunnel.source.Jdbc"):
    steps = [
        {"step_id": "src", "operator_id": source, "role": "source", "attributes": {}},
        {"step_id": "snk", "operator_id": sink, "role": "sink", "attributes": {}},
    ]
    return PipelineVersion(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        version_no=1,
        etl_plan={
            "steps": steps,
            "field_mappings": [
                {"source_field": "a", "target_field": "b", "sensitivity_level": level}
            ],
        },
        config="",
        quality_rules=[],
    )


@pytest.mark.parametrize(
    ("level", "sink", "expected"),
    [
        ("L1", "seatunnel.sink.Jdbc", RiskLevel.P3),
        ("L1", "seatunnel.sink.Doris", RiskLevel.P2),
        ("L2", "seatunnel.sink.Jdbc", RiskLevel.P2),
        ("L2", "seatunnel.sink.Doris", RiskLevel.P1),
        ("L3", "seatunnel.sink.Jdbc", RiskLevel.P1),
        ("L3", "seatunnel.sink.Doris", RiskLevel.P0),
        ("L4", "seatunnel.sink.Jdbc", RiskLevel.P0),
        ("L4", "seatunnel.sink.Doris", RiskLevel.P0),
    ],
)
def test_risk_ladder(level: str, sink: str, expected: RiskLevel):
    assert rate_risk(_version(level=level, sink=sink)).level is expected


def test_risk_streaming_source_is_at_least_p2():
    """CDC 增量任务持续作用在生产链路上，不因单批结束而降低风险。"""
    assessment = rate_risk(
        _version(level="L1", sink="seatunnel.sink.Jdbc", source="seatunnel.source.MySQL-CDC")
    )
    assert assessment.level is RiskLevel.P2
    assert any("CDC" in reason for reason in assessment.reasons)


def test_risk_reasons_explain_the_level():
    """审批人唯一需要被回答的是「为什么是这个等级」。"""
    assessment = rate_risk(_version(level="L3", sink="seatunnel.sink.Doris"))
    assert assessment.reasons
    assert any("L3" in reason for reason in assessment.reasons)
    assert any("覆盖写" in reason for reason in assessment.reasons)


def test_risk_is_deterministic():
    version = _version(level="L2", sink="seatunnel.sink.Doris")
    assert rate_risk(version) == rate_risk(version)


# --------------------------------------------------------------------------
# 准备单生成（FR-22）
# --------------------------------------------------------------------------


def test_prepare_creates_two_pending_slots(client: TestClient, ctx, stubs):
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    assert preparation["status"] == PreparationStatus.PENDING.value
    assert {a["dimension"] for a in preparation["approvals"]} == {"data", "security"}
    assert all(a["status"] == "pending" for a in preparation["approvals"])
    assert preparation["missing_dimensions"] == ["数据维度", "安全维度"]


def test_prepare_records_the_four_items(client: TestClient, ctx, stubs):
    """准备单四项是审批台判断风险的唯一依据，必须齐备。"""
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    assert preparation["impact_scope"]["target_table"] == "orders_wide"
    assert preparation["impact_scope"]["publish_strategy"] == "shadow-swap"
    assert preparation["data_grading"]["max_level"] == "L2"
    assert preparation["budget"]["max_rows"] > 0
    assert preparation["rollback_plan"]["strategy"] == "discard-shadow"


def test_prepare_binds_version_fingerprint(client: TestClient, ctx, stubs):
    version_id = _freeze(client, ctx, stubs)
    detail = client.get(
        f"/api/v1/versions/{version_id}", headers=ctx["maker_headers"]
    ).json()
    preparation = _prepare(client, ctx, version_id)

    assert preparation["fingerprint"] == detail["sha256"]


def test_prepare_has_no_side_effects(client: TestClient, ctx, stubs, session_factory):
    """FR-22：Prepare 零外部副作用 —— 不签发令牌、不落任何执行事实。"""
    from sqlalchemy import func, select

    from app.models.capability import Capability

    version_id = _freeze(client, ctx, stubs)
    _prepare(client, ctx, version_id)

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Capability)) == 0


def test_prepare_is_idempotent(client: TestClient, ctx, stubs):
    """审批台反复打开不应产生多份准备单。"""
    version_id = _freeze(client, ctx, stubs)
    first = _prepare(client, ctx, version_id)
    second = _prepare(client, ctx, version_id)

    assert first["id"] == second["id"]

    listed = client.get(
        f"/api/v1/preparations?project_id={ctx['project_id']}", headers=ctx["maker_headers"]
    ).json()
    assert len(listed) == 1


def test_prepare_requires_frozen_version(client: TestClient, ctx, stubs):
    """草稿版本还没过门禁，谈不上「这次执行要做什么」。"""
    stubs([PARSE_OK, MATCH_OK, PLAN])
    client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": "把 orders 同步到 Doris",
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
        headers=ctx["maker_headers"],
    )
    versions = client.get(
        f"/api/v1/versions?project_id={ctx['project_id']}", headers=ctx["maker_headers"]
    ).json()

    resp = client.post(
        "/api/v1/preparations",
        json={"version_id": versions[0]["id"]},
        headers=ctx["maker_headers"],
    )
    assert resp.status_code == 409


def test_prepare_requires_maker_role(client: TestClient, ctx, stubs):
    """审批人自己生成准备单等于把「申请」与「审批」并到一处。"""
    version_id = _freeze(client, ctx, stubs)

    resp = client.post(
        "/api/v1/preparations",
        json={"version_id": version_id},
        headers=ctx["headers"]["data"],
    )
    assert resp.status_code == 403


def test_preparation_is_project_scoped(client: TestClient, ctx, auth_headers, stubs):
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    outsider = auth_headers("outsider@example.com")
    resp = client.get(f"/api/v1/preparations/{preparation['id']}", headers=outsider)
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# 双维审批（FR-23）
# --------------------------------------------------------------------------


def test_one_dimension_is_not_enough(client: TestClient, ctx, stubs):
    """只审一维不得使准备单变为可执行。"""
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    resp = _decide(
        client, ctx, _slot(preparation, "data")["id"], ctx["headers"]["data"], "approved"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["status"] == PreparationStatus.PENDING.value
    assert body["missing_dimensions"] == ["安全维度"]
    assert _slot(body, "data")["status"] == "approved"


def test_both_dimensions_approve_makes_it_ready(client: TestClient, ctx, stubs):
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    _decide(client, ctx, _slot(preparation, "data")["id"], ctx["headers"]["data"], "approved")
    resp = _decide(
        client, ctx, _slot(preparation, "security")["id"], ctx["headers"]["security"], "approved"
    )
    body = resp.json()

    assert body["status"] == PreparationStatus.APPROVED.value
    assert body["missing_dimensions"] == []


def test_rejection_marks_preparation_rejected(client: TestClient, ctx, stubs):
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    resp = _decide(
        client, ctx, _slot(preparation, "security")["id"], ctx["headers"]["security"], "rejected"
    )
    assert resp.json()["status"] == PreparationStatus.REJECTED.value


def test_rejected_can_be_reapproved(client: TestClient, ctx, stubs):
    """审批人拿到补充说明后改判是正常流程，槽位仍是同一个。"""
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)
    approval_id = _slot(preparation, "data")["id"]

    _decide(client, ctx, approval_id, ctx["headers"]["data"], "rejected")
    resp = _decide(client, ctx, approval_id, ctx["headers"]["data"], "approved")
    assert resp.json()["status"] == PreparationStatus.PENDING.value

    _decide(
        client, ctx, _slot(preparation, "security")["id"], ctx["headers"]["security"], "approved"
    )
    final = client.get(
        f"/api/v1/preparations/{preparation['id']}", headers=ctx["maker_headers"]
    ).json()
    assert final["status"] == PreparationStatus.APPROVED.value


def test_decision_records_approver_and_comment(client: TestClient, ctx, stubs):
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)
    approval_id = _slot(preparation, "data")["id"]

    client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approved", "comment": "字段映射已核对"},
        headers=ctx["headers"]["data"],
    )
    body = client.get(
        f"/api/v1/preparations/{preparation['id']}", headers=ctx["maker_headers"]
    ).json()

    slot = _slot(body, "data")
    assert slot["approver_id"] is not None
    assert slot["comment"] == "字段映射已核对"
    assert slot["decided_at"] is not None


# --------------------------------------------------------------------------
# 职责分离（FR-24）
# --------------------------------------------------------------------------


def test_wrong_dimension_role_is_denied(client: TestClient, ctx, stubs):
    """安全审批人不得替数据维度签字。"""
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    resp = _decide(
        client, ctx, _slot(preparation, "data")["id"], ctx["headers"]["security"], "approved"
    )
    assert resp.status_code == 403


def test_maker_cannot_approve(client: TestClient, ctx, stubs):
    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    resp = _decide(
        client, ctx, _slot(preparation, "data")["id"], ctx["maker_headers"], "approved"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_PERMISSION_DENIED"


def test_self_approval_is_denied(client: TestClient, ctx, stubs, session_factory):
    """自批拦截：申请人即使拿到审批角色也不得审批自己的版本。

    角色矩阵下「申请人 = 审批人」本该不可能；本测试刻意改掉申请人的角色来制造
    这个状态——正是为了证明这条判定不是靠角色矩阵顺带挡住的。
    """
    from sqlalchemy import select

    from app.models.identity import Membership, User, UserRole

    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == MAKER))
        membership = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.project_id == uuid.UUID(ctx["project_id"]),
            )
        )
        membership.role = UserRole.CHECKER_DATA
        db.commit()

    resp = _decide(
        client, ctx, _slot(preparation, "data")["id"], ctx["maker_headers"], "approved"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "POL_SELF_APPROVAL_DENIED"


def test_one_person_cannot_hold_both_slots(client: TestClient, ctx, stubs, session_factory):
    """一人分饰两角：先审数据维度，再被改成安全审批人接着审——必须拦住。"""
    from sqlalchemy import select

    from app.models.identity import Membership, User, UserRole

    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

    _decide(client, ctx, _slot(preparation, "data")["id"], ctx["headers"]["data"], "approved")

    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == CHECKER_DATA))
        membership = db.scalar(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.project_id == uuid.UUID(ctx["project_id"]),
            )
        )
        membership.role = UserRole.CHECKER_SECURITY
        db.commit()

    resp = _decide(
        client, ctx, _slot(preparation, "security")["id"], ctx["headers"]["data"], "approved"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "POL_SELF_APPROVAL_DENIED"


# --------------------------------------------------------------------------
# 时限（SPEC §5：expires_at 非空）
# --------------------------------------------------------------------------


def test_expired_preparation_cannot_be_approved(
    client: TestClient, ctx, stubs, session_factory
):
    from sqlalchemy import select

    from app.models.approval import Approval, Preparation

    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)

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

    resp = _decide(
        client, ctx, _slot(preparation, "data")["id"], ctx["headers"]["data"], "approved"
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "POL_APPROVAL_INCOMPLETE"

    after = client.get(
        f"/api/v1/preparations/{preparation['id']}", headers=ctx["maker_headers"]
    ).json()
    assert after["status"] == PreparationStatus.EXPIRED.value


def test_reprepare_after_expiry_resets_slots(
    client: TestClient, ctx, stubs, session_factory
):
    """超时后重新 Prepare 相当于重新发起一轮审批：换新时限、清空决定。"""
    from sqlalchemy import select

    from app.models.approval import Approval, Preparation

    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)
    _decide(client, ctx, _slot(preparation, "data")["id"], ctx["headers"]["data"], "approved")

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

    again = _prepare(client, ctx, version_id)
    assert again["id"] == preparation["id"]
    assert again["status"] == PreparationStatus.PENDING.value
    assert all(a["status"] == "pending" for a in again["approvals"])


def test_expired_approval_does_not_count(
    client: TestClient, ctx, stubs, session_factory
):
    """审批通过时在时限内，但时限过后该授权不再成立。"""
    from sqlalchemy import select

    from app.models.approval import Approval, Preparation

    version_id = _freeze(client, ctx, stubs)
    preparation = _prepare(client, ctx, version_id)
    for dimension in ("data", "security"):
        _decide(
            client, ctx, _slot(preparation, dimension)["id"], ctx["headers"][dimension], "approved"
        )

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

    body = client.get(
        f"/api/v1/preparations/{preparation['id']}", headers=ctx["maker_headers"]
    ).json()
    assert body["missing_dimensions"] == ["数据维度（审批已超时）", "安全维度（审批已超时）"]


# --------------------------------------------------------------------------
# 数据分级的呈现口径
# --------------------------------------------------------------------------


def test_data_grading_counts_without_field_names(client: TestClient, ctx, stubs):
    """准备单会进审批台与审计报告，不能顺手把敏感字段名抄一份出来。"""
    version_id = _freeze(client, ctx, stubs, plan=_plan(level="L3"))
    preparation = _prepare(client, ctx, version_id)

    grading = preparation["data_grading"]
    assert grading["max_level"] == "L3"
    assert grading["level_counts"] == {"L3": 1}
    assert grading["sensitive_field_count"] == 1
    assert "order_id" not in json.dumps(grading, ensure_ascii=False)


def test_approval_dimension_enum_values_are_stable():
    """前端按维度字符串分支，枚举值属于已发布契约。"""
    assert ApprovalDimension.DATA.value == "data"
    assert ApprovalDimension.SECURITY.value == "security"
