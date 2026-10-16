"""版本冻结与 Diff 测试（API-05，FR-18 ~ FR-20）。"""

import json
import uuid
from datetime import UTC, datetime, timedelta

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


def test_maker_declares_quality_rules_before_freeze(client: TestClient, ctx, stubs):
    from app.services.versioning import compute_fingerprint

    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]
    rules = [{
        "field": "order_id",
        "rule": "not_null",
        "params": {},
        "error_code": "ORDER_ID_REQUIRED",
        "action": "reject",
    }]

    updated = client.put(
        f"/api/v1/versions/{version_id}/quality-rules",
        json={"rules": rules},
        headers=ctx["headers"],
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["quality_rules"] == rules
    assert updated.json()["etl_plan"]["quality_rules"] == rules

    frozen = client.post(
        f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"]
    )
    assert frozen.status_code == 200, frozen.text
    body = frozen.json()
    assert body["sha256"] == compute_fingerprint(
        body["etl_plan"], body["config"], body["quality_rules"]
    )


def test_quality_rules_cannot_change_frozen_version(client: TestClient, ctx, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]
    client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"])

    response = client.put(
        f"/api/v1/versions/{version_id}/quality-rules",
        json={"rules": []},
        headers=ctx["headers"],
    )
    assert response.status_code == 409
    assert response.json()["code"] == "BIZ_RESOURCE_CONFLICT"


def test_quality_rule_rejects_unknown_target_field(client: TestClient, ctx, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    response = client.put(
        f"/api/v1/versions/{version_id}/quality-rules",
        json={"rules": [{"field": "not_in_mapping", "rule": "not_null",
                         "error_code": "UNKNOWN_FIELD"}]},
        headers=ctx["headers"],
    )
    assert response.status_code == 409
    assert "目标字段映射" in response.json()["message"]


def test_quality_rule_error_codes_must_be_unique(client: TestClient, ctx, stubs):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    response = client.put(
        f"/api/v1/versions/{version_id}/quality-rules",
        json={"rules": [
            {"field": "order_id", "rule": "not_null", "error_code": "DUPLICATE"},
            {"field": "order_id", "rule": "regex", "error_code": "DUPLICATE"},
        ]},
        headers=ctx["headers"],
    )
    assert response.status_code == 409
    assert "错误码必须唯一" in response.json()["message"]


def test_other_maker_cannot_edit_someone_elses_quality_rules(
    client: TestClient, ctx, stubs, auth_headers
):
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]
    other = auth_headers("other.maker@example.com")
    added = client.post(
        f"/api/v1/projects/{ctx['project_id']}/members",
        json={"email": "other.maker@example.com", "role": "maker"},
        headers=ctx["headers"],
    )
    assert added.status_code == 201, added.text

    response = client.put(
        f"/api/v1/versions/{version_id}/quality-rules",
        json={"rules": []},
        headers=other,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "AUTH_PERMISSION_DENIED"


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


# --------------------------------------------------------------------------
# 探查记录绑定（FR-19 依据一致性）
# --------------------------------------------------------------------------


def _profile(
    session_factory,
    datasource_id: str,
    *,
    table: str,
    columns: list[str],
    sampled_at: datetime,
) -> str:
    """落一条探查记录。

    测试不真连数据库——门禁只读 ``schema_json["tables"]``，直接照这个形状造即可。
    """
    from app.models.datasource import MetadataProfile

    with session_factory() as db:
        record = MetadataProfile(
            datasource_id=uuid.UUID(datasource_id),
            schema_json={
                "tables": [
                    {
                        "name": table,
                        "max_level": "L2",
                        "columns": [
                            {"name": name, "type": "varchar", "level": "L2"}
                            for name in columns
                        ],
                    }
                ],
                "table_count": 1,
                "column_count": len(columns),
            },
            sensitivity_tags={},
            max_level="L2",
            sampled_at=sampled_at,
            tables_scanned=1,
        )
        db.add(record)
        db.commit()
        return str(record.id)


def _schema_check(gate_report: dict) -> dict:
    return next(c for c in gate_report["checks"] if c["name"] == "Schema 对齐")


def test_freeze_reuses_profile_bound_at_generation(
    client: TestClient, ctx, stubs, session_factory
):
    """冻结必须按**生成时绑定**的探查记录还原 Schema，而不是重新取「最新」。

    这是本项修复的核心断言：生成后又探查过一次的情况下，
    「取最新」会让一份本来合法的方案被判不对齐。
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    src_v1 = _profile(
        session_factory, ctx["source_id"], table="orders",
        columns=["order_id"], sampled_at=base,
    )
    tgt_v1 = _profile(
        session_factory, ctx["target_id"], table="orders_wide",
        columns=["order_id"], sampled_at=base,
    )

    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    detail = client.get(f"/api/v1/versions/{version_id}", headers=ctx["headers"]).json()
    assert detail["source_profile_id"] == src_v1
    assert detail["target_profile_id"] == tgt_v1

    # 生成之后重新探查：v2 里两端都不再有 order_id。
    # 若冻结退回「读最新」，这次冻结会因 Schema 不对齐而失败。
    later = base + timedelta(days=1)
    _profile(
        session_factory, ctx["source_id"], table="orders",
        columns=["another_col"], sampled_at=later,
    )
    _profile(
        session_factory, ctx["target_id"], table="orders_wide",
        columns=["another_col"], sampled_at=later,
    )

    resp = client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "frozen"


def test_freeze_fails_when_bound_profile_disappeared(
    client: TestClient, ctx, stubs, session_factory
):
    """绑定过探查记录、冻结时记录已不可用 → 「无从校验」，判 failed 而不是 skipped。"""
    from sqlalchemy import delete

    from app.models.datasource import MetadataProfile

    base = datetime(2026, 1, 1, tzinfo=UTC)
    _profile(
        session_factory, ctx["source_id"], table="orders",
        columns=["order_id"], sampled_at=base,
    )
    _profile(
        session_factory, ctx["target_id"], table="orders_wide",
        columns=["order_id"], sampled_at=base,
    )

    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    # 模拟数据源被删（探查记录随之级联清理）
    with session_factory() as db:
        db.execute(
            delete(MetadataProfile).where(
                MetadataProfile.datasource_id == uuid.UUID(ctx["source_id"])
            )
        )
        db.commit()

    resp = client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"])
    assert resp.status_code == 409

    after = client.get(f"/api/v1/versions/{version_id}", headers=ctx["headers"]).json()
    assert after["status"] == "rejected"
    assert after["sha256"] is None
    assert _schema_check(after["gate_report"])["status"] == "failed"


def test_freeze_without_any_profile_is_skipped_in_dev(client: TestClient, ctx, stubs):
    """从未探查过 → 开发环境记 ``skipped``（联调不受阻），与「记录被删」区别对待。"""
    _generate(client, ctx, [PARSE_OK, MATCH_OK, PLAN_V1], stubs)
    version_id = _versions(client, ctx)[0]["id"]

    resp = client.post(f"/api/v1/versions/{version_id}/freeze", headers=ctx["headers"])
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["status"] == "frozen"
    check = _schema_check(body["gate_report"])
    assert check["status"] == "skipped"
    assert body["source_profile_id"] is None


# --------------------------------------------------------------------------
# 生成异常收敛（不允许 run 悬挂在 running）
# --------------------------------------------------------------------------


def _post_generate(client: TestClient, ctx: dict):
    return client.post(
        "/api/v1/pipelines/generate",
        json={
            "project_id": ctx["project_id"],
            "requirement": "把 orders 同步到 Doris",
            "source_datasource_id": ctx["source_id"],
            "target_datasource_id": ctx["target_id"],
        },
        headers=ctx["headers"],
    )


def test_model_failure_marks_run_failed(client: TestClient, ctx, stubs, session_factory):
    """模型不可用时必须落 failed 终态——否则前端轮询永远等不到结果。"""
    from sqlalchemy import select

    from app.models.pipeline import PipelineRun, PipelineRunStatus

    stubs([])  # 脚本为空：第一步调用即 ModelUnavailable
    resp = _post_generate(client, ctx)
    assert resp.status_code == 503
    assert resp.json()["code"] == "SYS_MODEL_UNAVAILABLE"

    with session_factory() as db:
        run = db.scalar(select(PipelineRun))
        assert run is not None
        assert run.status is PipelineRunStatus.FAILED
        assert run.error


def test_unexpected_exception_marks_run_failed(
    client: TestClient, ctx, monkeypatch, session_factory
):
    """未预期的异常同样收敛为 failed，且**不外泄内部细节**。"""
    from langgraph.checkpoint.memory import InMemorySaver
    from sqlalchemy import select

    from app.api.routes import pipelines as routes
    from app.models.pipeline import PipelineRun, PipelineRunStatus

    monkeypatch.setattr(routes, "get_checkpointer", lambda: InMemorySaver())

    class _Boom:
        def complete(self, *_args, **_kwargs):
            raise RuntimeError("内部细节不该外泄")

        def complete_json(self, *_args, **_kwargs):
            raise RuntimeError("内部细节不该外泄")

    monkeypatch.setattr(routes, "get_llm_client", lambda: _Boom())

    resp = _post_generate(client, ctx)
    assert resp.status_code == 503
    assert "内部细节" not in resp.text

    with session_factory() as db:
        run = db.scalar(select(PipelineRun))
        assert run is not None
        assert run.status is PipelineRunStatus.FAILED
        assert "内部细节" not in (run.error or "")


def test_failed_generation_leaves_no_draft_version(
    client: TestClient, ctx, stubs, session_factory
):
    """异常路径不得留下半成品草稿。"""
    stubs([])
    _post_generate(client, ctx)
    assert _versions(client, ctx) == []
