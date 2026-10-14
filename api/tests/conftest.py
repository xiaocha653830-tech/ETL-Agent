"""pytest 公共夹具。

按 SPEC §4 环境差异约定：**开发环境单元测试使用内存 SQLite**，
不依赖 PostgreSQL / Redis / Vault，测试不消费真实资源。
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 —— 导入以注册全部 ORM 模型到 Base.metadata
from app.db.base import Base
from app.db.session import get_db
from app.main import app

DEFAULT_PASSWORD = "Passw0rd!"


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """每个测试函数一个全新的内存库。"""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        # 内存库必须共享同一连接，否则每次连接看到的都是空库
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """覆盖数据库依赖后的测试客户端。"""

    def _override_get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    # raise_server_exceptions=False：让 500 也走全局处理器，便于断言统一错误体
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def register_user(client: TestClient):
    """返回一个「注册用户」的辅助函数。"""

    def _register(
        email: str,
        password: str = DEFAULT_PASSWORD,
        display_name: str | None = None,
    ) -> dict:
        resp = client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": display_name},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    return _register


@pytest.fixture()
def auth_headers(client: TestClient, register_user):
    """注册并登录，返回可直接用于请求的鉴权头。"""

    def _headers(email: str, password: str = DEFAULT_PASSWORD) -> dict[str, str]:
        register_user(email)
        resp = client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    return _headers
