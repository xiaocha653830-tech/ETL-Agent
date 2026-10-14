"""认证接口测试（FR-44 的一部分）。

重点验证「所有错误出口都是统一错误体」这一全局约定。
"""

from fastapi.testclient import TestClient

from tests.conftest import DEFAULT_PASSWORD


def test_register_success(client: TestClient):
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "email": "alice@example.com",
            "password": DEFAULT_PASSWORD,
            "display_name": "Alice",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "alice@example.com"
    assert body["display_name"] == "Alice"
    assert body["is_active"] is True
    # 绝不返回口令哈希等敏感字段
    assert "hashed_password" not in body
    assert "password" not in body


def test_register_duplicate_email(client: TestClient, register_user):
    register_user("dup@example.com")
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "dup@example.com", "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "AUTH_EMAIL_TAKEN"


def test_register_rejects_short_password(client: TestClient):
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "short@example.com", "password": "123"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "BIZ_VALIDATION_FAILED"


def test_login_success(client: TestClient, register_user):
    register_user("bob@example.com")
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "bob@example.com", "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0


def test_login_wrong_password_and_unknown_email_share_same_code(
    client: TestClient, register_user
):
    """防账号枚举：密码错误与账号不存在必须返回同一错误码与文案。"""
    register_user("carol@example.com")

    wrong_pwd = client.post(
        "/api/v1/auth/login",
        json={"email": "carol@example.com", "password": "WrongPass1!"},
    )
    unknown = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": DEFAULT_PASSWORD},
    )

    assert wrong_pwd.status_code == unknown.status_code == 401
    assert wrong_pwd.json() == unknown.json()
    assert wrong_pwd.json()["code"] == "AUTH_INVALID_CREDENTIALS"


def test_login_disabled_account(client: TestClient, register_user, session_factory):
    """停用账号登录被拒（AUTH_ACCOUNT_DISABLED）。"""
    from sqlalchemy import select

    from app.models.identity import User

    register_user("frozen@example.com")
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == "frozen@example.com"))
        user.is_active = False
        db.commit()

    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "frozen@example.com", "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_ACCOUNT_DISABLED"


def test_me_requires_token(client: TestClient):
    resp = client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_INVALID"


def test_me_rejects_garbage_token(client: TestClient):
    resp = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTH_TOKEN_INVALID"


def test_me_returns_current_user(client: TestClient, auth_headers):
    headers = auth_headers("dave@example.com")
    resp = client.get("/api/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["email"] == "dave@example.com"


def test_unknown_route_uses_unified_error_envelope(client: TestClient):
    """框架抛出的 404 也必须与业务错误结构一致。"""
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "BIZ_RESOURCE_NOT_FOUND"
    assert set(body) == {"code", "message", "detail"}


def test_health_and_readiness(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["database"] == "up"
