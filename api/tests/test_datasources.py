"""数据源登记、连通性测试与元数据探查测试（FR-01 ~ FR-07）。

探查用手写的替身数据源驱动，不依赖真实数据库；
连通性测试同理（真实驱动需要 pymysql 等，属部署期依赖）。
"""

import pytest
from fastapi.testclient import TestClient

PLAINTEXT_PASSWORD = "super-secret-pwd"


# --------------------------------------------------------------------------
# 替身：不连数据库，返回预置 Schema 与样本值
# --------------------------------------------------------------------------


class FakeEngine:
    def dispose(self) -> None:
        pass


class FakeMetadataSource:
    TABLES = {
        "customers": [
            {"name": "id_card", "type": "VARCHAR(18)", "nullable": False},
            {"name": "full_name", "type": "VARCHAR(64)", "nullable": True},
        ],
        "orders": [
            {"name": "order_id", "type": "BIGINT", "nullable": False},
            {"name": "user_phone", "type": "VARCHAR(20)", "nullable": True},
            {"name": "amount", "type": "DECIMAL(10,2)", "nullable": True},
        ],
    }
    SAMPLES = {
        ("customers", "id_card"): ["110101199003078888"],
        ("customers", "full_name"): ["张三丰"],
        ("orders", "order_id"): ["1001", "1002"],
        ("orders", "user_phone"): ["13812345678"],
        ("orders", "amount"): ["99.50"],
    }

    def __init__(self, engine, schema=None) -> None:
        pass

    def list_tables(self) -> list[str]:
        return sorted(self.TABLES)

    def list_columns(self, table: str) -> list[dict]:
        return self.TABLES[table]

    def sample_values(self, table: str, column: str, limit: int) -> list[str]:
        return self.SAMPLES.get((table, column), [])


@pytest.fixture()
def stub_probe(monkeypatch):
    """把探查路由里的引擎与读取器替换成替身。"""
    from app.api.routes import datasources as routes

    monkeypatch.setattr(routes, "build_engine", lambda spec: FakeEngine())
    monkeypatch.setattr(routes, "SqlAlchemyMetadataSource", FakeMetadataSource)
    return routes


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


@pytest.fixture()
def project(client: TestClient, auth_headers):
    """返回 (maker_headers, project_id)。"""
    headers = auth_headers("maker@example.com")
    resp = client.post("/api/v1/projects", json={"name": "集成项目"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return headers, resp.json()["id"]


def _register_datasource(
    client: TestClient, headers: dict, project_id: str, *, name: str = "订单源库"
):
    return client.post(
        "/api/v1/datasources",
        json={
            "project_id": project_id,
            "name": name,
            "type": "mysql",
            "config": {"host": "127.0.0.1", "port": 3306, "database": "demo"},
            "credential": {"username": "etl_reader", "password": PLAINTEXT_PASSWORD},
        },
        headers=headers,
    )


# --------------------------------------------------------------------------
# 登记
# --------------------------------------------------------------------------


def test_register_datasource(client: TestClient, project, stub_probe):
    headers, project_id = project
    resp = _register_datasource(client, headers, project_id)

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "订单源库"
    assert body["type"] == "mysql"
    assert body["status"] == "registered"
    assert body["has_credential"] is True
    assert body["credential_fingerprint"]


def test_response_never_leaks_credential(client: TestClient, project, stub_probe):
    """FR-03 的硬要求：明文口令不得出现在任何响应里。"""
    headers, project_id = project
    resp = _register_datasource(client, headers, project_id)

    raw = resp.text
    assert PLAINTEXT_PASSWORD not in raw
    assert "etl_reader" not in raw
    assert "credential_ref" not in resp.json()

    datasource_id = resp.json()["id"]
    detail = client.get(f"/api/v1/datasources/{datasource_id}", headers=headers)
    assert PLAINTEXT_PASSWORD not in detail.text
    assert detail.json()["has_credential"] is True


def test_register_without_credential_is_allowed(client: TestClient, project, stub_probe):
    headers, project_id = project
    resp = client.post(
        "/api/v1/datasources",
        json={
            "project_id": project_id,
            "name": "无凭据源",
            "type": "postgresql",
            "config": {"host": "127.0.0.1"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["has_credential"] is False
    assert resp.json()["credential_fingerprint"] is None


def test_list_and_filter_by_project(client: TestClient, project, stub_probe):
    headers, project_id = project
    _register_datasource(client, headers, project_id, name="源 A")
    _register_datasource(client, headers, project_id, name="源 B")

    resp = client.get(f"/api/v1/datasources?project_id={project_id}", headers=headers)
    assert resp.status_code == 200
    assert [item["name"] for item in resp.json()] == ["源 A", "源 B"]


def test_cross_tenant_datasource_access_is_denied(
    client: TestClient, project, auth_headers, stub_probe
):
    headers, project_id = project
    datasource_id = _register_datasource(client, headers, project_id).json()["id"]

    outsider = auth_headers("outsider@example.com")
    assert (
        client.get(f"/api/v1/datasources/{datasource_id}", headers=outsider).status_code == 403
    )
    denied = client.post(f"/api/v1/datasources/{datasource_id}/test", headers=outsider)
    assert denied.status_code == 403


def test_register_requires_authentication(client: TestClient, stub_probe):
    resp = client.post(
        "/api/v1/datasources", json={"project_id": None, "name": "x", "type": "mysql"}
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------
# 连通性测试（FR-04）
# --------------------------------------------------------------------------


def test_connection_success_updates_status(
    client: TestClient, project, stub_probe, monkeypatch
):
    headers, project_id = project
    datasource_id = _register_datasource(client, headers, project_id).json()["id"]

    monkeypatch.setattr(stub_probe, "test_connection", lambda spec: 42)
    resp = client.post(f"/api/v1/datasources/{datasource_id}/test", headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "latency_ms": 42, "message": "连接成功"}
    assert (
        client.get(f"/api/v1/datasources/{datasource_id}", headers=headers).json()["status"]
        == "connected"
    )


def test_connection_failure_marks_unreachable(
    client: TestClient, project, stub_probe, monkeypatch
):
    from app.core.errors import DatasourceUnreachable

    headers, project_id = project
    datasource_id = _register_datasource(client, headers, project_id).json()["id"]

    def _boom(spec):
        raise DatasourceUnreachable("连接失败（OperationalError）")

    monkeypatch.setattr(stub_probe, "test_connection", _boom)
    resp = client.post(f"/api/v1/datasources/{datasource_id}/test", headers=headers)

    assert resp.status_code == 502
    assert resp.json()["code"] == "SYS_DATASOURCE_UNREACHABLE"
    assert (
        client.get(f"/api/v1/datasources/{datasource_id}", headers=headers).json()["status"]
        == "unreachable"
    )


# --------------------------------------------------------------------------
# 元数据探查（API-02 / FR-05、FR-06、FR-07）
# --------------------------------------------------------------------------


def test_profile_generates_sensitivity_tags(client: TestClient, project, stub_probe):
    headers, project_id = project
    datasource_id = _register_datasource(client, headers, project_id).json()["id"]

    resp = client.post(f"/api/v1/datasources/{datasource_id}/profile", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # customers 表含 id_card → 越级为 L4
    assert body["max_level"] == "L4"
    assert body["tables_scanned"] == 2
    assert body["sensitivity_tags"]["customers"]["max_level"] == "L4"
    assert body["sensitivity_tags"]["customers"]["by_level"]["L4"] == ["id_card"]
    assert body["sensitivity_tags"]["orders"]["by_level"]["L3"] == ["user_phone"]


def test_profile_samples_are_masked(client: TestClient, project, stub_probe):
    """FR-06：探查结果里不得出现明文原始值。"""
    headers, project_id = project
    datasource_id = _register_datasource(client, headers, project_id).json()["id"]

    body = client.post(
        f"/api/v1/datasources/{datasource_id}/profile", headers=headers
    ).json()

    assert "110101199003078888" not in str(body)
    assert "13812345678" not in str(body)

    by_table = {table["name"]: table for table in body["schema_json"]["tables"]}
    phone_col = next(
        col for col in by_table["orders"]["columns"] if col["name"] == "user_phone"
    )
    assert phone_col["samples"] == ["138****5678"]
    assert phone_col["level"] == "L3"

    id_col = next(col for col in by_table["customers"]["columns"] if col["name"] == "id_card")
    assert id_col["samples"] == ["********"]
    assert id_col["level"] == "L4"


def test_profile_marks_datasource_as_profiled(client: TestClient, project, stub_probe):
    headers, project_id = project
    datasource_id = _register_datasource(client, headers, project_id).json()["id"]

    client.post(f"/api/v1/datasources/{datasource_id}/profile", headers=headers)

    assert (
        client.get(f"/api/v1/datasources/{datasource_id}", headers=headers).json()["status"]
        == "profiled"
    )


def test_profile_history_is_kept(client: TestClient, project, stub_probe):
    """FR-07：探查结果版本化留档。"""
    headers, project_id = project
    datasource_id = _register_datasource(client, headers, project_id).json()["id"]

    client.post(f"/api/v1/datasources/{datasource_id}/profile", headers=headers)
    client.post(f"/api/v1/datasources/{datasource_id}/profile", headers=headers)

    resp = client.get(f"/api/v1/datasources/{datasource_id}/profiles", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_profile_budget_parameters_are_validated(
    client: TestClient, project, stub_probe
):
    headers, project_id = project
    datasource_id = _register_datasource(client, headers, project_id).json()["id"]

    resp = client.post(
        f"/api/v1/datasources/{datasource_id}/profile?tables_limit=0", headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "BIZ_VALIDATION_FAILED"
