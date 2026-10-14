"""需求生成接口测试（API-03 / API-04，FR-08 ~ FR-14）。

checkpointer 换成 ``InMemorySaver``、模型换成 ``FakeLLMClient`` ——
接口测试不该依赖 PostgreSQL 的 checkpoint 表，也不该调真实模型。
"""

import json

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

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

PARSE_NEEDS_CLARIFY = json.dumps(
    {
        "understood": "同步订单数据",
        "source_table": "orders",
        "target_table": None,
        "fields": [],
        "filters": None,
        "schedule": None,
        "need_clarification": [
            {"key": "target_table", "question": "目标表名？", "hint": None, "options": []}
        ],
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
                    "url": "jdbc:mysql://db:3306/demo",
                    "query": "SELECT order_id FROM orders",
                },
            },
            {
                "step_id": "snk",
                "operator_id": "seatunnel.sink.Doris",
                "role": "sink",
                "attributes": {
                    "fenodes": "127.0.0.1:8030",
                    "database": "ods",
                    "table": "orders_wide",
                },
            },
        ],
    },
    ensure_ascii=False,
)

MATCH_UNSUPPORTED = json.dumps(
    {"supported": False, "reason": "受管清单中没有消息队列相关算子"}, ensure_ascii=False
)

PLAN_OK = json.dumps(
    {
        "name": "订单同步",
        "description": "订单表同步到 Doris",
        "source_table": "orders",
        "target_table": "orders_wide",
        "field_mappings": [
            {"source_field": "order_id", "target_field": "order_id", "transform": None,
             "sensitivity_level": "L2"}
        ],
        "unmapped_source_fields": [],
        "warnings": [],
    },
    ensure_ascii=False,
)


@pytest.fixture()
def stubs(monkeypatch):
    """替换模型客户端与断点存储；返回装配函数。"""
    from app.api.routes import pipelines as routes

    saver = InMemorySaver()
    monkeypatch.setattr(routes, "get_checkpointer", lambda: saver)

    def _install(responses: list[str]) -> None:
        monkeypatch.setattr(routes, "get_llm_client", lambda: FakeLLMClient(responses))

    return _install


@pytest.fixture()
def ctx(client: TestClient, auth_headers):
    """建好项目与两端数据源，返回请求所需的一揽子上下文。"""
    headers = auth_headers("maker@example.com")
    project = client.post(
        "/api/v1/projects", json={"name": "订单集成"}, headers=headers
    ).json()

    def _datasource(name: str, ds_type: str, config: dict) -> str:
        resp = client.post(
            "/api/v1/datasources",
            json={"project_id": project["id"], "name": name, "type": ds_type, "config": config},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return {
        "headers": headers,
        "project_id": project["id"],
        "source_id": _datasource("订单源库", "mysql", {"host": "127.0.0.1", "database": "demo"}),
        "target_id": _datasource("数仓", "doris", {"host": "127.0.0.1", "database": "ods"}),
    }


def _generate(client: TestClient, ctx: dict, requirement: str = "把 orders 同步到 Doris"):
    return client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": requirement,
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
        headers=ctx["headers"],
    )


# --------------------------------------------------------------------------
# 正常生成
# --------------------------------------------------------------------------


def test_generate_success(client: TestClient, ctx, stubs):
    stubs([PARSE_OK, MATCH_OK, PLAN_OK])
    resp = _generate(client, ctx)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["plan"]["target_table"] == "orders_wide"
    assert body["config"]["format"] == "hocon"
    assert len(body["dag"]["nodes"]) == 2
    assert body["dag"]["edges"][0]["source"] == "src"


def test_generated_config_has_no_credentials(client: TestClient, ctx, stubs):
    stubs([PARSE_OK, MATCH_OK, PLAN_OK])
    body = _generate(client, ctx).json()

    assert "jdbc:mysql://db:3306/demo" not in body["config"]["content"]
    assert "${SOURCE_URL}" in body["config"]["content"]


def test_result_can_be_read_back(client: TestClient, ctx, stubs):
    stubs([PARSE_OK, MATCH_OK, PLAN_OK])
    run_id = _generate(client, ctx).json()["run_id"]

    resp = client.get(f"/api/v1/pipelines/{run_id}", headers=ctx["headers"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "succeeded"
    assert resp.json()["plan"]["target_table"] == "orders_wide"


def test_list_generations_scoped_to_project(client: TestClient, ctx, stubs):
    stubs([PARSE_OK, MATCH_OK, PLAN_OK, PARSE_OK, MATCH_OK, PLAN_OK])
    _generate(client, ctx)
    _generate(client, ctx)

    resp = client.get(
        f"/api/v1/pipelines?project_id={ctx['project_id']}", headers=ctx["headers"]
    )
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# --------------------------------------------------------------------------
# 澄清中断（FR-09 / FR-10）
# --------------------------------------------------------------------------


def test_generate_returns_clarification_interrupt(client: TestClient, ctx, stubs):
    stubs([PARSE_NEEDS_CLARIFY])
    resp = _generate(client, ctx)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "interrupted"
    assert body["questions"][0]["key"] == "target_table"
    assert body["plan"] is None


def test_resume_completes_the_run(client: TestClient, ctx, stubs):
    stubs([PARSE_NEEDS_CLARIFY])
    run_id = _generate(client, ctx).json()["run_id"]

    stubs([PARSE_OK, MATCH_OK, PLAN_OK])
    resp = client.post(
        f"/api/v1/pipelines/{run_id}/resume",
        json={"answers": {"target_table": "orders_wide"}},
        headers=ctx["headers"],
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "succeeded"
    assert resp.json()["plan"]["target_table"] == "orders_wide"


def test_resume_requires_pending_interrupt(client: TestClient, ctx, stubs):
    stubs([PARSE_OK, MATCH_OK, PLAN_OK])
    run_id = _generate(client, ctx).json()["run_id"]

    resp = client.post(
        f"/api/v1/pipelines/{run_id}/resume",
        json={"answers": {"target_table": "x"}},
        headers=ctx["headers"],
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "BIZ_RESOURCE_CONFLICT"


def test_resume_rejects_empty_answers(client: TestClient, ctx, stubs):
    stubs([PARSE_NEEDS_CLARIFY])
    run_id = _generate(client, ctx).json()["run_id"]

    resp = client.post(
        f"/api/v1/pipelines/{run_id}/resume", json={"answers": {}}, headers=ctx["headers"]
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------
# 不支持（FR-14）
# --------------------------------------------------------------------------


def test_unsupported_returns_reason_not_error(client: TestClient, ctx, stubs):
    stubs([PARSE_OK, MATCH_UNSUPPORTED])
    resp = _generate(client, ctx)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "unsupported"
    assert "消息队列" in body["unsupported_reason"]
    assert body["plan"] is None


def test_failure_status_is_not_confused_with_unsupported(client: TestClient, ctx, stubs):
    """「不支持」是业务结论，「失败」是系统故障，前端据此走不同分支。"""
    stubs([PARSE_OK, MATCH_OK, PLAN_OK])
    assert _generate(client, ctx).json()["status"] == "succeeded"

    stubs([PARSE_OK, MATCH_UNSUPPORTED])
    assert _generate(client, ctx).json()["status"] == "unsupported"


# --------------------------------------------------------------------------
# 权限与租户
# --------------------------------------------------------------------------


def test_generate_requires_authentication(client: TestClient, ctx):
    resp = client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": "x",
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
    )
    assert resp.status_code == 401


def test_cross_tenant_cannot_generate(client: TestClient, ctx, auth_headers, stubs):
    stubs([PARSE_OK, MATCH_OK, PLAN_OK])
    outsider = auth_headers("outsider@example.com")

    resp = client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": "x",
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
        headers=outsider,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_CROSS_TENANT"


def test_read_only_role_cannot_generate(client: TestClient, ctx, stubs):
    """审计人员是全局只读角色，不能发起生成。"""
    stubs([PARSE_OK, MATCH_OK, PLAN_OK])
    maker = ctx["headers"]

    # 先把审计人员加进项目
    client.post(
        "/api/v1/auth/register",
        json={"email": "auditor@example.com", "password": "Passw0rd!"},
        headers=maker,
    )
    token = client.post(
        "/api/v1/auth/login",
        json={"email": "auditor@example.com", "password": "Passw0rd!"},
    ).json()["access_token"]
    auditor = {"Authorization": f"Bearer {token}"}

    client.post(
        f"/api/v1/projects/{ctx['project_id']}/members",
        json={"email": "auditor@example.com", "role": "auditor"},
        headers=maker,
    )

    resp = client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": "x",
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
        headers=auditor,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_PERMISSION_DENIED"
