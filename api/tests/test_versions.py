"""版本冻结与 Diff 测试（API-05，FR-18 ~ FR-20）。"""

import json
import uuid

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

MATCH_OK = json.dumps(
    {
        "supported": True,
        "steps": [
            {
                "step_id": "src",
                "operator_id": "seatunnel.source.Jdbc",
                "role": "source",
                "attributes": {
                    # driver 与 query 是 Jdbc 源的非注入必填项，门禁会据此校验
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


def _plan(name: str, target_field: str) -> str:
    return json.dumps(
        {
            "name": name,
            "description": "订单表同步",
            "source_table": "orders",
            "target_table": "orders_wide",
            "field_mappings": [
                {"source_field": "order_id", "target_field": target_field,
                 "transform": None, "sensitivity_level": "L2"}
            ],
            "unmapped_source_fields": [],
            "warnings": [],
        },
        ensure_ascii=False,
    )


PLAN_V1 = _plan("订单同步", "order_id")
PLAN_V2 = _plan("订单同步（改字段）", "order_no")


@pytest.fixture()
def stubs(monkeypatch):
    from app.api.routes import pipelines as routes

    saver = InMemorySaver()
    monkeypatch.setattr(routes, "get_checkpointer", lambda: saver)

    def _install(responses: list[str]) -> None:
        monkeypatch.setattr(routes, "get_llm_client", lambda: FakeLLMClient(responses))

    return _install


@pytest.fixture()
def ctx(client: TestClient, auth_headers):
    headers = auth_headers("maker@example.com")
    project = client.post("/api/v1/projects", json={"name": "集成"}, headers=headers).json()

    def _datasource(name: str, ds_type: str) -> str:
        return client.post(
            "/api/v1/datasources",
            json={
                "project_id": project["id"],
                "name": name,
                "type": ds_type,
                "config": {"host": "127.0.0.1"},
            },
            headers=headers,
        ).json()["id"]

    return {
        "headers": headers,
        "project_id": project["id"],
        "source_id": _datasource("源库", "mysql"),
        "target_id": _datasource("数仓", "doris"),
    }


def _generate(client: TestClient, ctx: dict, responses: list[str], stubs) -> str:
    stubs(responses)
    resp = client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": "把 orders 同步到 Doris",
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
        headers=ctx["headers"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["run_id"]


def _versions(client: TestClient, ctx: dict) -> list[dict]:
    resp = client.get(
        f"/api/v1/versions?project_id={ctx['project_id']}", headers=ctx["headers"]
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# 草稿自动落库
# --------------------------------------------------------------------------


def test_successful_generation_creates_draft_version(client: TestClient, ctx, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)

    versions = _versions(client, ctx)
    assert len(versions) == 1
    assert versions[0]["status"] == "draft"
    # 未过门禁就没有指纹
    assert versions[0]["sha256"] is None
    assert versions[0]["frozen_at"] is None


def test_failed_generation_creates_no_version(client: TestClient, ctx, stubs):
    stubs([PARSE_OK, json.dumps({"supported": False, "reason": "不支持"})])
    client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": "x",
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
        headers=ctx["headers"],
    )
    assert _versions(client, ctx) == []


# --------------------------------------------------------------------------
# 冻结
# --------------------------------------------------------------------------


def test_freeze_produces_fingerprint(client: TestClient, ctx, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    resp = client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["status"] == "frozen"
    assert len(body["sha256"]) == 64
    assert body["frozen_at"] is not None
    assert body["gate_report"]["passed"] is True
    assert body["gate_report"]["summary"]["failed"] == 0


def test_freeze_is_idempotent_protected(client: TestClient, ctx, stubs):
    """冻结版本不可重复冻结，更不可修改（FR-19）。"""
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    assert (
        client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"]).status_code
        == 200
    )
    again = client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"])
    assert again.status_code == 409
    assert again.json()["code"] == "BIZ_RESOURCE_CONFLICT"


def test_fingerprint_is_stable_for_same_content(client: TestClient, ctx, stubs):
    from app.models.version import PipelineVersion
    from app.services.versioning import compute_fingerprint

    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]
    client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"])

    frozen = _versions(client, ctx)[0]
    detail = client.get(f"/api/v1/versions/{version_id}", headers=ctx["headers"]).json()

    expected = compute_fingerprint(
        detail["etl_plan"], detail["config"], detail["quality_rules"]
    )
    assert frozen["sha256"] == expected
    assert isinstance(PipelineVersion, type)


def test_gate_failure_marks_version_rejected(
    client: TestClient, ctx, stubs, session_factory
):
    """门禁不通过时不得产生半冻结版本。"""
    from sqlalchemy import select

    from app.models.version import PipelineVersion

    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    # 直接在库里把配置改坏，模拟一份语法无效的产物
    with session_factory() as db:
        version = db.scalar(
            select(PipelineVersion).where(PipelineVersion.id == uuid.UUID(version_id))
        )
        version.config = "source { Jdbc { url = "
        db.commit()

    resp = client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"])
    assert resp.status_code == 409
    assert "门禁未通过" in resp.json()["message"]

    after = client.get(f"/api/v1/versions/{version_id}", headers=ctx["headers"]).json()
    assert after["status"] == "rejected"
    assert after["sha256"] is None
    assert after["gate_report"]["passed"] is False


# --------------------------------------------------------------------------
# Diff（FR-20）
# --------------------------------------------------------------------------


def test_diff_between_two_versions(client: TestClient, ctx, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V2], stubs)

    versions = _versions(client, ctx)
    assert [item["version_no"] for item in versions] == [2, 1]
    older, newer = versions[1]["id"], versions[0]["id"]

    resp = client.get(
        f"/api/v1/versions/{older}/diff?against={newer}", headers=ctx["headers"]
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["left"]["version_no"] == 1
    assert body["right"]["version_no"] == 2
    assert body["plan_changed"] is True
    assert len(body["mappings_changed"]) == 1
    assert body["mappings_changed"][0]["after"]["target_field"] == "order_no"


def test_diff_of_identical_plans_has_no_mapping_change(client: TestClient, ctx, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)

    versions = _versions(client, ctx)
    resp = client.get(
        f"/api/v1/versions/{versions[1]['id']}/diff?against={versions[0]['id']}",
        headers=ctx["headers"],
    )
    body = resp.json()

    assert body["plan_changed"] is False
    assert body["mappings_added"] == []
    assert body["mappings_removed"] == []
    assert body["mappings_changed"] == []

    # 配置**会**不同：Doris 的 sink.label-prefix 按 run_id 生成（SPEC §11.1 V2 要求
    # 全局唯一），两次运行必然得到不同的标签前缀。这不是缺陷，而是幂等设计的结果。
    assert body["config_changed"] is True
    assert body["fingerprint_equal"] is False


def test_diff_across_projects_is_rejected(client: TestClient, ctx, auth_headers, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    mine = _versions(client, ctx)[0]["id"]

    other = auth_headers("other@example.com")
    other_project = client.post(
        "/api/v1/projects", json={"name": "别人的项目"}, headers=other
    ).json()

    resp = client.get(
        f"/api/v1/versions/{mine}/diff?against={other_project['id']}", headers=ctx["headers"]
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 权限
# --------------------------------------------------------------------------


def test_freeze_requires_authentication(client: TestClient, ctx, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    assert client.post(f"/api/v1/versions/{version_id}/freeze").status_code == 401


def test_versions_are_project_scoped(client: TestClient, ctx, auth_headers, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    outsider = auth_headers("outsider@example.com")

    assert (
        client.get(
            f"/api/v1/versions?project_id={ctx['project_id']}", headers=outsider
        ).status_code
        == 403
    )
