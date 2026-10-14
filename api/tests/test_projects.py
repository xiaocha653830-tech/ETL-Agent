"""项目与成员管理测试（FR-44 多租户隔离 / FR-45 成员角色管理）。"""

import uuid

from fastapi.testclient import TestClient


def _create_project(client: TestClient, headers: dict, name: str = "数据集成项目") -> dict:
    resp = client.post("/api/v1/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_member(
    client: TestClient, headers: dict, project_id: str, email: str, role: str
):
    return client.post(
        f"/api/v1/projects/{project_id}/members",
        json={"email": email, "role": role},
        headers=headers,
    )


# --------------------------------------------------------------------------
# 项目
# --------------------------------------------------------------------------


def test_create_project_makes_creator_a_maker(client: TestClient, auth_headers):
    headers = auth_headers("maker@example.com")
    project = _create_project(client, headers)

    assert project["tenant_id"] == "default"

    members = client.get(f"/api/v1/projects/{project['id']}/members", headers=headers)
    assert members.status_code == 200
    rows = members.json()
    assert len(rows) == 1
    assert rows[0]["email"] == "maker@example.com"
    assert rows[0]["role"] == "maker"


def test_list_projects_only_returns_own_memberships(client: TestClient, auth_headers):
    alice = auth_headers("alice@example.com")
    bob = auth_headers("bob@example.com")
    _create_project(client, alice, name="A 的项目")

    assert len(client.get("/api/v1/projects", headers=alice).json()) == 1
    assert client.get("/api/v1/projects", headers=bob).json() == []


def test_cross_tenant_access_is_denied(client: TestClient, auth_headers):
    alice = auth_headers("alice@example.com")
    bob = auth_headers("bob@example.com")
    project = _create_project(client, alice)

    resp = client.get(f"/api/v1/projects/{project['id']}", headers=bob)
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_CROSS_TENANT"


def test_unknown_project_id_does_not_leak_existence(client: TestClient, auth_headers):
    """项目不存在与无权访问返回同一错误码 —— 不泄露资源是否存在。"""
    headers = auth_headers("prober@example.com")
    resp = client.get(f"/api/v1/projects/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_CROSS_TENANT"


def test_project_endpoints_require_authentication(client: TestClient):
    assert client.get("/api/v1/projects").status_code == 401
    assert client.post("/api/v1/projects", json={"name": "x"}).status_code == 401


# --------------------------------------------------------------------------
# 成员与角色
# --------------------------------------------------------------------------


def test_maker_can_add_member(client: TestClient, auth_headers):
    maker = auth_headers("maker@example.com")
    auth_headers("checker@example.com")  # 先注册目标用户
    project = _create_project(client, maker)

    resp = _add_member(client, maker, project["id"], "checker@example.com", "checker_data")
    assert resp.status_code == 201, resp.text
    assert resp.json()["role"] == "checker_data"
    assert resp.json()["email"] == "checker@example.com"

    assert len(client.get(f"/api/v1/projects/{project['id']}/members", headers=maker).json()) == 2


def test_add_member_by_non_maker_is_denied(client: TestClient, auth_headers):
    maker = auth_headers("maker@example.com")
    checker = auth_headers("checker@example.com")
    project = _create_project(client, maker)
    _add_member(client, maker, project["id"], "checker@example.com", "checker_data")

    resp = _add_member(client, checker, project["id"], "maker@example.com", "auditor")
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_PERMISSION_DENIED"


def test_add_unregistered_email_returns_404(client: TestClient, auth_headers):
    maker = auth_headers("maker@example.com")
    project = _create_project(client, maker)

    resp = _add_member(client, maker, project["id"], "ghost@example.com", "auditor")
    assert resp.status_code == 404
    assert resp.json()["code"] == "BIZ_RESOURCE_NOT_FOUND"


def test_add_duplicate_member_returns_409(client: TestClient, auth_headers):
    maker = auth_headers("maker@example.com")
    auth_headers("checker@example.com")
    project = _create_project(client, maker)

    assert (
        _add_member(client, maker, project["id"], "checker@example.com", "auditor").status_code
        == 201
    )
    again = _add_member(client, maker, project["id"], "checker@example.com", "checker_data")
    assert again.status_code == 409
    assert again.json()["code"] == "BIZ_RESOURCE_CONFLICT"


def test_update_member_role(client: TestClient, auth_headers):
    maker = auth_headers("maker@example.com")
    auth_headers("checker@example.com")
    project = _create_project(client, maker)
    membership = _add_member(
        client, maker, project["id"], "checker@example.com", "auditor"
    ).json()

    resp = client.patch(
        f"/api/v1/projects/{project['id']}/members/{membership['id']}",
        json={"role": "checker_security"},
        headers=maker,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "checker_security"


def test_cannot_remove_self(client: TestClient, auth_headers):
    maker = auth_headers("maker@example.com")
    project = _create_project(client, maker)
    membership = client.get(
        f"/api/v1/projects/{project['id']}/members", headers=maker
    ).json()[0]

    resp = client.delete(
        f"/api/v1/projects/{project['id']}/members/{membership['id']}", headers=maker
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "BIZ_RESOURCE_CONFLICT"


def test_remove_member(client: TestClient, auth_headers):
    maker = auth_headers("maker@example.com")
    auth_headers("checker@example.com")
    project = _create_project(client, maker)
    membership = _add_member(
        client, maker, project["id"], "checker@example.com", "auditor"
    ).json()

    resp = client.delete(
        f"/api/v1/projects/{project['id']}/members/{membership['id']}", headers=maker
    )
    assert resp.status_code == 204
    assert len(client.get(f"/api/v1/projects/{project['id']}/members", headers=maker).json()) == 1


def test_membership_operations_are_project_scoped(client: TestClient, auth_headers):
    """拿甲项目的 membership_id 去乙项目操作必须失败（防越权）。"""
    maker = auth_headers("maker@example.com")
    auth_headers("checker@example.com")

    project_a = _create_project(client, maker, name="甲项目")
    project_b = _create_project(client, maker, name="乙项目")
    membership_a = _add_member(
        client, maker, project_a["id"], "checker@example.com", "auditor"
    ).json()

    resp = client.delete(
        f"/api/v1/projects/{project_b['id']}/members/{membership_a['id']}", headers=maker
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "BIZ_RESOURCE_NOT_FOUND"
