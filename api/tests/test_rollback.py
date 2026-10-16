"""T-18 受管回滚（FR-38 / API-10）。

回滚是本系统里唯一会 ``DROP TABLE`` 的动作，因此这里测的重点不是「删成功了吗」，
而是**什么时候必须拒绝删**：影子表名在换表之后指向的是被换下来的旧正式表，
把它当残留清掉就是删掉上一版生产数据。
"""

import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.errors import ResourceConflict, RollbackUnavailable
from app.models.datasource import DatasourceType
from app.models.execution import ExecutionRun, ExecutionRunStatus, RunEvent
from app.models.identity import Membership, UserRole
from app.models.version import PipelineVersion
from app.services.quality import DorisPublisher
from app.services.rollback import OPERATION_DISCARD_SHADOW, rollback_run

# --------------------------------------------------------------------------
# 执行体：discard_shadow 真的会发 DROP TABLE
# --------------------------------------------------------------------------


class _DroppingConnection:
    def __init__(self, tables):
        self.tables = set(tables)
        self.statements: list[str] = []

    def cursor(self):
        return _DroppingCursor(self)

    def close(self):
        pass


class _DroppingCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=()):
        self.connection.statements.append(sql)
        if "information_schema.tables" in sql:
            self.result = [(int(params[1] in self.connection.tables),)]
        elif sql.startswith("DROP TABLE"):
            self.connection.tables.discard(re.search(r"`([^`]+)`\Z", sql).group(1))
            self.result = []
        else:
            raise AssertionError(f"未预期 SQL: {sql}")

    def fetchall(self):
        return self.result


def _publisher(monkeypatch, tables):
    connection = _DroppingConnection(tables)
    monkeypatch.setattr("app.services.quality.pymysql.connect", lambda **_kw: connection)
    project_id, target_id, version_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    version = SimpleNamespace(
        project_id=project_id,
        etl_plan={"target_datasource_id": str(target_id), "target_table": "orders_wide"},
    )
    target = SimpleNamespace(
        project_id=project_id,
        type=DatasourceType.DORIS,
        config={"host": "127.0.0.1", "port": 8050, "database": "ods"},
        credential_ref="target",
    )

    class _Db:
        def get(self, model, key):
            return version if model is PipelineVersion else target

    class _Credentials:
        def resolve(self, ref):
            return {"username": "root", "password": ""}

    command = SimpleNamespace(
        version_id=version_id,
        target_table="orders_wide",
        shadow_table="orders_wide__shadow_v2",
    )
    return DorisPublisher(_Db(), command, _Credentials()), connection


def test_discard_shadow_drops_the_error_table_before_the_shadow_table(monkeypatch):
    publisher, connection = _publisher(
        monkeypatch, ["orders_wide", "orders_wide__shadow_v2", "orders_wide__shadow_v2__errors"]
    )
    dropped = publisher.discard_shadow()
    assert dropped == ["orders_wide__shadow_v2__errors", "orders_wide__shadow_v2"]
    # 正式表必须原样留着——这正是「受管」二字的全部含义
    assert connection.tables == {"orders_wide"}


def test_discard_shadow_is_a_no_op_when_the_run_never_created_anything(monkeypatch):
    publisher, connection = _publisher(monkeypatch, ["orders_wide"])
    assert publisher.discard_shadow() == []
    assert not any(sql.startswith("DROP TABLE") for sql in connection.statements)


# --------------------------------------------------------------------------
# 服务层：什么时候必须拒绝
# --------------------------------------------------------------------------


class _PublisherSpy:
    """记录构造次数的发布器替身——用来证明「没有第二次删表」。"""

    def __init__(self, dropped=("orders_wide__shadow_v1",), error=None):
        self._dropped = list(dropped)
        self._error = error
        self.calls = 0
        self.closed = 0
        self.command = None

    def __call__(self, db, command, credentials):
        self.calls += 1
        self.command = command
        return self

    def discard_shadow(self):
        if self._error is not None:
            raise self._error
        return list(self._dropped)

    def close(self):
        self.closed += 1


def _fail(db, run_id, *, published=False, status=ExecutionRunStatus.FAILED):
    run = db.get(ExecutionRun, run_id)
    run.status = status
    run.published_at = datetime.now(UTC) if published else None
    db.commit()
    return run


def test_rollback_is_refused_while_the_run_is_still_running(
    session_factory, execution_world
):
    """还在写影子的运行不能回滚：删表会把数据写到一张正在消失的表上。"""
    with session_factory() as db:
        run = db.get(ExecutionRun, execution_world.run_id)
        assert run.status is ExecutionRunStatus.PENDING
        with pytest.raises(ResourceConflict, match="尚未结束"):
            rollback_run(db, run=run, actor="op", publisher_factory=_PublisherSpy())


def test_rollback_is_refused_once_a_publish_swapped_the_table(
    session_factory, execution_world
):
    """核心安全边界：换过表之后，影子表名下是旧正式表，不是残留。"""
    spy = _PublisherSpy()
    with session_factory() as db:
        run = _fail(db, execution_world.run_id, published=True)
        with pytest.raises(RollbackUnavailable, match="旧正式表"):
            rollback_run(db, run=run, actor="op", publisher_factory=spy)
    assert spy.calls == 0


def test_a_sibling_publish_also_blocks_the_rollback(session_factory, execution_world):
    """同一版本的另一次运行发布过，这个影子表名同样不能再当残留清掉。"""
    spy = _PublisherSpy()
    with session_factory() as db:
        run = _fail(db, execution_world.run_id)
        sibling = ExecutionRun(
            project_id=run.project_id,
            version_id=run.version_id,
            preparation_id=run.preparation_id,
            created_by=run.created_by,
            status=ExecutionRunStatus.SUCCEEDED,
            environment=run.environment,
            artifact=run.artifact,
            published_at=datetime.now(UTC),
        )
        db.add(sibling)
        db.commit()
        with pytest.raises(RollbackUnavailable):
            rollback_run(db, run=run, actor="op", publisher_factory=spy)
    assert spy.calls == 0


def test_rollback_drops_the_residue_and_records_both_facts(
    session_factory, execution_world
):
    spy = _PublisherSpy()
    with session_factory() as db:
        run = _fail(db, execution_world.run_id)
        outcome = rollback_run(db, run=run, actor="op-1", publisher_factory=spy)
        db.refresh(run)

        assert outcome.executed is True
        assert outcome.dropped == ("orders_wide__shadow_v1",)
        assert run.rolled_back_at is not None
        assert spy.closed == 1
        # 删的是已批准命令里的影子表，不是调用方临时给的任何名字
        assert spy.command.shadow_table == execution_world.command.shadow_table

        events = db.query(RunEvent).filter_by(run_id=run.id).all()
        assert any(event.event_type == "run.rolled_back" for event in events)


def test_repeating_the_rollback_never_drops_a_second_time(
    session_factory, execution_world
):
    """``DROP TABLE`` 没有撤回，因此重复点击只能拿回上一次的结论。"""
    spy = _PublisherSpy()
    with session_factory() as db:
        run = _fail(db, execution_world.run_id)
        first = rollback_run(db, run=run, actor="op", publisher_factory=spy)
        second = rollback_run(db, run=run, actor="op", publisher_factory=spy)

    assert first.executed is True
    assert second.executed is False
    assert second.dropped == first.dropped
    assert spy.calls == 1


def test_a_failed_rollback_is_never_replayed_automatically(
    session_factory, execution_world
):
    """上一轮已经动过手但结论不明时，自动化不猜——重放要人看一眼目标端。"""
    boom = _PublisherSpy(error=RuntimeError("连接中断"))
    with session_factory() as db:
        run = _fail(db, execution_world.run_id)
        with pytest.raises(RuntimeError):
            rollback_run(db, run=run, actor="op", publisher_factory=boom)

        calmer = _PublisherSpy()
        outcome = rollback_run(db, run=run, actor="op", publisher_factory=calmer)
        db.refresh(run)

    assert outcome.executed is False
    assert calmer.calls == 0
    assert run.rolled_back_at is None
    assert OPERATION_DISCARD_SHADOW  # 台账键是稳定契约，改名等于改语义


# --------------------------------------------------------------------------
# API-10：角色与错误码
# --------------------------------------------------------------------------


@pytest.fixture()
def operator_headers(client, session_factory, register_user, execution_world):
    """在 execution_world 的项目里注册一名系统操作员。"""

    def _make(role: UserRole = UserRole.OPERATOR, email: str = "op@example.com"):
        user = register_user(email)
        with session_factory() as db:
            db.add(
                Membership(
                    user_id=uuid.UUID(user["id"]),
                    project_id=execution_world.project_id,
                    role=role,
                )
            )
            db.commit()
        resp = client.post(
            "/api/v1/auth/login", json={"email": email, "password": "Passw0rd!"}
        )
        assert resp.status_code == 200, resp.text
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    return _make


@pytest.fixture()
def stub_publisher(monkeypatch):
    spy = _PublisherSpy(
        dropped=("orders_wide__shadow_v1__errors", "orders_wide__shadow_v1")
    )
    monkeypatch.setattr("app.services.rollback.DorisPublisher", spy)
    return spy


def test_rollback_endpoint_returns_the_cleanup_result(
    client, session_factory, execution_world, operator_headers, stub_publisher
):
    headers = operator_headers()
    with session_factory() as db:
        _fail(db, execution_world.run_id)

    resp = client.post(f"/api/v1/runs/{execution_world.run_id}/rollback", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executed"] is True
    assert body["dropped"] == ["orders_wide__shadow_v1__errors", "orders_wide__shadow_v1"]


def test_rollback_endpoint_refuses_a_maker(
    client, session_factory, execution_world, operator_headers
):
    """PRD 第 2 章：回滚属系统操作员，数据工程师没有这个动作。"""
    headers = operator_headers(role=UserRole.MAKER, email="maker@example.com")
    with session_factory() as db:
        _fail(db, execution_world.run_id)

    resp = client.post(f"/api/v1/runs/{execution_world.run_id}/rollback", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "AUTH_PERMISSION_DENIED"


def test_rollback_endpoint_reports_a_permanent_refusal(
    client, session_factory, execution_world, operator_headers, stub_publisher
):
    """已发布的运行回滚不了——这是永久拒绝，与「先去取消」必须可区分。"""
    headers = operator_headers()
    with session_factory() as db:
        _fail(db, execution_world.run_id, published=True)

    resp = client.post(f"/api/v1/runs/{execution_world.run_id}/rollback", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["code"] == "BIZ_ROLLBACK_UNAVAILABLE"
