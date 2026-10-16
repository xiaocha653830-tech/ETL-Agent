"""pytest 公共夹具。

按 SPEC §4 环境差异约定：**开发环境单元测试使用内存 SQLite**，
不依赖 PostgreSQL / Redis / Vault，测试不消费真实资源。
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 —— 导入以注册全部 ORM 模型到 Base.metadata
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app

DEFAULT_PASSWORD = "Passw0rd!"

# 「已批准并受理」的制品指纹占位；执行链路测试不关心它从哪来，只关心它处处一致
ARTIFACT = "a" * 64


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
    # 关掉启动期的证据链校验：它走 SessionLocal（真实数据库），而单元测试用的是
    # 每次新建的内存库——在那里扫一条空链既无意义，又会把每一条用到 TestClient
    # 的用例都拖去连一次真库。生产默认值仍是 True（SPEC §8 要求校验器启动时跑）。
    original_verify = settings.audit_verify_on_startup
    settings.audit_verify_on_startup = False
    # raise_server_exceptions=False：让 500 也走全局处理器，便于断言统一错误体
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        settings.audit_verify_on_startup = original_verify
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


@pytest.fixture()
def execution_world(session_factory: sessionmaker[Session]) -> SimpleNamespace:
    """一条最小可执行链路：项目 / 版本 / 准备单 / 运行 + 一条待投递事件。

    直接用 ORM 造，不走 HTTP：执行层的测试关注的是认领的并发语义与投递的幂等，
    再跑一遍「生成 → 冻结 → 审批 → Commit」只会让失败原因更难定位。
    """
    from app.models.approval import Preparation, PreparationStatus, RiskLevel
    from app.models.execution import ExecutionRun, ExecutionRunStatus
    from app.models.identity import Membership, Project, User, UserRole
    from app.models.version import PipelineVersion, VersionStatus
    from app.schemas.execution import ExecutionCommand
    from app.services.commit import EXECUTION_TOOL
    from app.services.outbox import (
        EVENT_RUN_REQUESTED,
        enqueue,
        execution_run_key,
    )

    with session_factory() as db:
        user = User(email="operator@example.com", hashed_password="x")
        project = Project(name="集成", tenant_id="default")
        db.add_all([user, project])
        db.flush()
        db.add(
            Membership(user_id=user.id, project_id=project.id, role=UserRole.OPERATOR)
        )

        version = PipelineVersion(
            project_id=project.id,
            created_by=user.id,
            version_no=1,
            status=VersionStatus.FROZEN,
            sha256=ARTIFACT,
            etl_plan={"target_table": "orders_wide", "steps": []},
            config="",
            quality_rules=[],
            frozen_at=datetime.now(UTC),
        )
        db.add(version)
        db.flush()

        preparation = Preparation(
            project_id=project.id,
            version_id=version.id,
            created_by=user.id,
            fingerprint=ARTIFACT,
            risk_level=RiskLevel.P2,
            risk_reasons=[],
            impact_scope={"target_table": "orders_wide", "shadow_table": "orders_wide__shadow_v1"},
            data_grading={"max_level": "L2"},
            budget={"max_rows": 1000},
            rollback_plan={"strategy": "discard-shadow"},
            status=PreparationStatus.COMMITTED,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db.add(preparation)
        db.flush()

        run = ExecutionRun(
            project_id=project.id,
            version_id=version.id,
            preparation_id=preparation.id,
            created_by=user.id,
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
            subject=str(user.id),
            environment="dev",
            artifact=ARTIFACT,
            target_table="orders_wide",
            shadow_table="orders_wide__shadow_v1",
            budget={"max_rows": 1000},
        )
        event = enqueue(
            db,
            idempotency_key=execution_run_key(run.id),
            event_type=EVENT_RUN_REQUESTED,
            payload=command.model_dump(mode="json"),
        )
        db.commit()

        return SimpleNamespace(
            project_id=project.id,
            user_id=user.id,
            version_id=version.id,
            preparation_id=preparation.id,
            run_id=run.id,
            event_id=event.id,
            command=command,
        )
