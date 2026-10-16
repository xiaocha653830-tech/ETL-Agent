"""T-15：冻结制品、受管授权与 SeaTunnel REST V2 的边界测试。"""

import uuid

import pytest

from app.core.config import settings
from app.core.errors import DataplaneJobFailed, DataplaneProtocolError, DataplaneUnavailable
from app.models.capability import Capability
from app.models.datasource import Datasource, DatasourceType
from app.models.execution import ExecutionRun, ExecutionRunStatus, OutboxEvent, RunEvent
from app.models.version import PipelineVersion
from app.services.capability import issue_capability
from app.services.commit import EXECUTION_TOOL
from app.services.execution import (
    BudgetExceeded,
    DataplaneCancelled,
    PollDecision,
    run_execution,
)
from app.services.outbox import execution_run_key
from app.services.quality import QualityReport
from app.services.seatunnel import (
    SeaTunnelDataPlane,
    UrllibSeatunnelTransport,
    render_runtime_config,
)
from app.services.versioning import compute_fingerprint

CONFIG = '''
# 此行仅说明占位符 ${VAR}，不是需要注入的配置项。
env { job.mode = "BATCH" }
source {
  Jdbc {
    url = "${SOURCE_URL}"
    user = "${SOURCE_USER}"
    password = "${SOURCE_PASSWORD}"
    query = "SELECT id FROM orders"
  }
}
sink {
  Doris {
    fenodes = "${TARGET_FENODES}"
    username = "${TARGET_USERNAME}"
    password = "${TARGET_PASSWORD}"
    database = "ods"
    table = "${TARGET_TABLE}"
    sink.label-prefix = "etl-${RUN_ID}"
  }
}
'''


class _Credentials:
    def resolve(self, ref):
        return {
            "source": {"username": "reader", "password": "source-secret"},
            "target": {"username": "writer", "password": "target-secret"},
        }[ref]


class _Transport:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def request(self, method, path, *, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/submit-job?"):
            return {"jobId": 42, "jobName": "etl-test"}
        if path == "/stop-job":
            return {"jobId": 42}
        assert path == "/job-info/42"
        status = self.states.pop(0)
        return {
            "jobId": 42,
            "jobStatus": status,
            "metrics": {"SourceReceivedCount": "12", "SinkCommittedBytes": "48"},
        }


class _Publisher:
    def prepare(self, *, with_quality_rules):
        self.with_quality_rules = with_quality_rules

    def report(self, *, source_rows, budget, with_quality_rules):
        return QualityReport(source_rows, source_rows, 0, 0.0, None)

    def rejected_rows(self):
        return None

    def publish(self):
        self.published = True

    def close(self):
        pass


def _publisher_factory(db, command, credentials):
    return _Publisher()


def _proceed(*_args: object) -> PollDecision:
    """默认轮询结论：不停。"""
    return PollDecision.proceed()


@pytest.fixture()
def seatunnel_world(session_factory, execution_world):
    with session_factory() as db:
        run = db.get(ExecutionRun, execution_world.run_id)
        version = db.get(PipelineVersion, execution_world.version_id)
        source = Datasource(
            project_id=run.project_id,
            name="MySQL",
            type=DatasourceType.MYSQL,
            config={"host": "mysql", "port": 3306, "database": "shop"},
            credential_ref="source",
        )
        target = Datasource(
            project_id=run.project_id,
            name="Doris",
            type=DatasourceType.DORIS,
            config={"host": "doris-fe", "port": 8030, "database": "ods"},
            credential_ref="target",
        )
        db.add_all([source, target])
        db.flush()
        version.etl_plan = {
            "source_datasource_id": str(source.id),
            "target_datasource_id": str(target.id),
            "source_table": "orders",
            "target_table": "orders_wide",
            "steps": [],
        }
        version.config = CONFIG
        version.sha256 = compute_fingerprint(version.etl_plan, CONFIG, [])
        run.artifact = version.sha256
        capability = issue_capability(
            db,
            tool=EXECUTION_TOOL,
            subject=str(run.created_by),
            environment=run.environment,
            artifact=version.sha256,
            preparation_id=run.preparation_id,
            commit=False,
        )
        run.capability_id = capability.capability_id
        command = execution_world.command.model_copy(
            update={
                "artifact": version.sha256,
                "capability_token": capability.token,
                "capability_id": capability.capability_id,
            }
        )
        event = db.query(OutboxEvent).filter_by(id=execution_world.event_id).one()
        event.payload = command.model_dump(mode="json")
        db.commit()
    return command


def _plane(states, *, transport=None):
    client = transport or _Transport(states)
    return (
        SeaTunnelDataPlane(
            transport=client,
            credentials=_Credentials(),
            publisher_factory=_publisher_factory,
            poll_interval=0.01,
            job_timeout=2,
            sleep=lambda _: None,
        ),
        client,
    )


def test_submit_polls_metrics_and_consumes_capability(session_factory, seatunnel_world):
    plane, transport = _plane(["RUNNING", "FINISHED"])
    with session_factory() as db:
        outcome = plane.submit(seatunnel_world, db=db, on_poll=_proceed)
        assert db.get(Capability, seatunnel_world.capability_id).consumed is True

    assert (outcome.read_rows, outcome.written_bytes) == (12, 48)
    assert outcome.detail["job_id"] == 42
    assert outcome.detail["job_status"] == "FINISHED"
    assert outcome.detail["published"] is True
    assert [call[1] for call in transport.calls[1:]] == ["/job-info/42", "/job-info/42"]
    method, path, config = transport.calls[0]
    assert method == "POST" and path.startswith("/submit-job?format=hocon")
    assert 'table = "orders_wide__shadow_v1"' in config
    assert f'"etl-{seatunnel_world.run_id.hex[:12]}"' in config
    assert 'password = "source-secret"' in config
    assert 'password = "target-secret"' in config
    assert not any(
        "${" in line for line in config.splitlines()
        if not line.lstrip().startswith("#")
    )


def test_cancel_stops_engine_job(session_factory, seatunnel_world):
    plane, transport = _plane(["RUNNING"])
    with session_factory() as db:
        with pytest.raises(DataplaneCancelled):
            plane.submit(
                seatunnel_world, db=db,
                on_poll=lambda status, _metrics: (
                    PollDecision.cancel() if status == "RUNNING" else _proceed()
                ),
            )
    assert transport.calls[-1] == (
        "POST", "/stop-job", {"jobId": 42, "isStopWithSavePoint": False}
    )


def test_failed_engine_status_is_not_success(session_factory, seatunnel_world):
    plane, _ = _plane(["FAILED"])
    with session_factory() as db:
        with pytest.raises(DataplaneJobFailed):
            plane.submit(seatunnel_world, db=db, on_poll=_proceed)


def test_timeout_requests_stop_before_failing(session_factory, seatunnel_world):
    transport = _Transport(["RUNNING"])
    # 预算监督也会读时钟，因此时钟须能无限取值：耗尽后固定返回 3.0。
    ticks = iter([0.0, 0.0, 3.0])
    plane = SeaTunnelDataPlane(
        transport=transport,
        credentials=_Credentials(),
        publisher_factory=_publisher_factory,
        poll_interval=0.01,
        job_timeout=2,
        clock=lambda: next(ticks, 3.0),
        sleep=lambda _: None,
    )
    with session_factory() as db:
        with pytest.raises(DataplaneJobFailed):
            plane.submit(seatunnel_world, db=db, on_poll=_proceed)
    assert transport.calls[-1][1] == "/stop-job"


def test_query_failure_attempts_to_stop_submitted_job(session_factory, seatunnel_world):
    class _BrokenQuery(_Transport):
        def request(self, method, path, *, body=None):
            if path == "/job-info/42":
                self.calls.append((method, path, body))
                raise DataplaneUnavailable("查询失败")
            return super().request(method, path, body=body)

    transport = _BrokenQuery([])
    plane, _ = _plane([], transport=transport)
    with session_factory() as db:
        with pytest.raises(DataplaneUnavailable):
            plane.submit(seatunnel_world, db=db, on_poll=_proceed)
    assert transport.calls[-1][1] == "/stop-job"


def test_production_rest_endpoint_requires_tls(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "prod")
    with pytest.raises(ValueError, match="HTTPS"):
        UrllibSeatunnelTransport("http://seatunnel:8080", 10)


def test_tampered_frozen_config_never_reaches_engine(session_factory, seatunnel_world):
    plane, transport = _plane([])
    with session_factory() as db:
        version = db.get(PipelineVersion, seatunnel_world.version_id)
        version.config = version.config.replace(
            'table = "${TARGET_TABLE}"', 'table = "orders_wide"'
        )
        db.commit()
        with pytest.raises(DataplaneProtocolError):
            plane.submit(seatunnel_world, db=db, on_poll=_proceed)
    assert transport.calls == []


def test_missing_shadow_binding_never_reaches_engine(session_factory, seatunnel_world):
    with session_factory() as db:
        command = seatunnel_world.model_copy(update={"shadow_table": "orders_wide"})
        with pytest.raises(DataplaneProtocolError):
            render_runtime_config(db, command, credentials=_Credentials())


def test_doris_empty_password_is_a_valid_managed_credential(session_factory, seatunnel_world):
    class _EmptyDorisPassword(_Credentials):
        def resolve(self, ref):
            result = super().resolve(ref)
            return {**result, "password": ""} if ref == "target" else result

    with session_factory() as db:
        config = render_runtime_config(
            db, seatunnel_world, credentials=_EmptyDorisPassword()
        )
    assert 'password = ""' in config


def test_unknown_engine_state_is_protocol_error(session_factory, seatunnel_world):
    plane, _ = _plane(["MYSTERY"])
    with session_factory() as db:
        with pytest.raises(DataplaneProtocolError):
            plane.submit(seatunnel_world, db=db, on_poll=_proceed)


def test_worker_records_real_engine_terminal_state(session_factory, seatunnel_world):
    plane, _ = _plane(["RUNNING", "FINISHED"])
    with session_factory() as db:
        run = run_execution(
            db, run_id=seatunnel_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )
        assert run.status is ExecutionRunStatus.SUCCEEDED
        assert (run.read_rows, run.written_bytes) == (12, 48)
        events = db.query(RunEvent).filter_by(run_id=run.id).all()
        assert any(event.event_type == "run.progress" for event in events)
        assert any(event.output_snapshot == {"job_id": 42} for event in events)
        assert all("source-secret" not in str(event.input_snapshot) for event in events)
        assert all("target-secret" not in str(event.output_snapshot) for event in events)


def test_running_metrics_are_visible_before_terminal_state(session_factory, seatunnel_world):
    class _InspectingTransport(_Transport):
        def request(self, method, path, *, body=None):
            if path == "/job-info/42" and len(self.states) == 1:
                with session_factory() as observer:
                    run = observer.get(ExecutionRun, seatunnel_world.run_id)
                    assert run.status is ExecutionRunStatus.RUNNING
                    assert (run.read_rows, run.written_bytes) == (12, 48)
            return super().request(method, path, body=body)

    plane, _ = _plane([], transport=_InspectingTransport(["RUNNING", "FINISHED"]))
    with session_factory() as db:
        run = run_execution(
            db, run_id=seatunnel_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )
        assert run.status is ExecutionRunStatus.SUCCEEDED


def test_worker_does_not_mark_failed_engine_job_as_success(session_factory, seatunnel_world):
    plane, _ = _plane(["FAILED"])
    with session_factory() as db:
        run = run_execution(
            db, run_id=seatunnel_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )
        assert run.status is ExecutionRunStatus.FAILED
        assert "SYS_DATAPLANE_JOB_FAILED" in run.error


# --------------------------------------------------------------------------
# T-17：运行时预算监督与超预算硬中断（FR-35 / FR-36）
# --------------------------------------------------------------------------


def _rebudget(db, command, budget):
    """替换运行声明的预算。

    命令与投递事件必须同时换：``load_command`` 读的是事件载荷，
    只改其中一处会让测试跑在一份根本没生效的预算上。
    """
    event = (
        db.query(OutboxEvent)
        .filter_by(idempotency_key=execution_run_key(command.run_id))
        .one()
    )
    event.payload = {**event.payload, "budget": budget}
    db.commit()
    return command.model_copy(update={"budget": budget})


class _MetricTransport(_Transport):
    """按轮次回传不同度量的替身，用于构造「跑着跑着越界」的场景。"""

    def __init__(self, states, metrics):
        super().__init__(states)
        self._metrics = list(metrics)

    def request(self, method, path, *, body=None):
        if path == "/job-info/42":
            self.calls.append((method, path, body))
            return {
                "jobId": 42,
                "jobStatus": self.states.pop(0),
                "metrics": self._metrics.pop(0),
            }
        return super().request(method, path, body=body)


def test_budget_breach_stops_the_engine_job(session_factory, seatunnel_world):
    plane, transport = _plane(["RUNNING", "RUNNING"])
    with session_factory() as db:
        command = _rebudget(db, seatunnel_world, {"max_rows": 5})
        with pytest.raises(BudgetExceeded) as caught:
            plane.submit(command, db=db, on_poll=_proceed)

    # 判定归判定，停止归停止：作业必须真的被请求停止，快照要带上越界细节
    assert transport.calls[-1] == (
        "POST", "/stop-job", {"jobId": 42, "isStopWithSavePoint": False}
    )
    snapshot = caught.value.snapshot
    assert [item["dimension"] for item in snapshot["breaches"]] == ["max_rows"]
    assert snapshot["observed"]["read_rows"] == 12


def test_budget_breach_on_the_final_poll_still_blocks_publication(
    session_factory, seatunnel_world
):
    """作业恰好在最后一次轮询才暴露真实体量时，也不能放行。

    这是最容易漏掉的一条：状态已是 FINISHED，没有任何东西可以再停，
    但放行就等于换表——而换表之后没有回退点。
    """
    transport = _MetricTransport(
        ["RUNNING", "FINISHED"],
        [
            {"SourceReceivedCount": "1", "SinkCommittedBytes": "8"},
            {"SourceReceivedCount": "900", "SinkCommittedBytes": "8"},
        ],
    )
    plane, _ = _plane([], transport=transport)
    with session_factory() as db:
        command = _rebudget(db, seatunnel_world, {"max_rows": 5})
        with pytest.raises(BudgetExceeded):
            plane.submit(command, db=db, on_poll=_proceed)
    assert all(call[1] != "/stop-job" for call in transport.calls)


def test_amplification_dimension_reads_source_and_sink_bytes(
    session_factory, seatunnel_world
):
    transport = _MetricTransport(
        ["RUNNING", "RUNNING"],
        [
            {
                "SourceReceivedCount": "10",
                "SourceReceivedBytes": "100",
                "SinkWriteBytes": "1000",
            }
        ],
    )
    plane, _ = _plane([], transport=transport)
    with session_factory() as db:
        command = _rebudget(db, seatunnel_world, {"max_amplification_ratio": 2.0})
        with pytest.raises(BudgetExceeded) as caught:
            plane.submit(command, db=db, on_poll=_proceed)
    breach = caught.value.snapshot["breaches"][0]
    assert breach["dimension"] == "max_amplification_ratio"
    assert breach["observed"] == 10.0


def test_undeclared_dimension_never_interrupts_a_real_job(
    session_factory, seatunnel_world
):
    """只声明行数上限时，作业正常跑完——未声明的维度不参与判定。"""
    plane, transport = _plane(["RUNNING", "FINISHED"])
    with session_factory() as db:
        command = _rebudget(db, seatunnel_world, {"max_rows": 1000})
        outcome = plane.submit(command, db=db, on_poll=_proceed)
        assert db.get(Capability, command.capability_id).consumed is True

    assert outcome.read_rows == 12
    assert outcome.detail["published"] is True
    assert all(call[1] != "/stop-job" for call in transport.calls)


def test_worker_records_budget_failure_with_failure_snapshot(
    session_factory, seatunnel_world
):
    """FR-36：运行落失败终态，并把中断那一刻的观测作为快照留痕。"""
    plane, transport = _plane(["RUNNING", "RUNNING"])
    with session_factory() as db:
        _rebudget(db, seatunnel_world, {"max_rows": 5})
        run = run_execution(
            db, run_id=seatunnel_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )

        assert run.status is ExecutionRunStatus.FAILED
        assert run.error.startswith("BIZ_BUDGET_EXCEEDED")
        # 中断前的最后一次进度要留在运行行上，列表页直接看得到「跑到哪一步被拦下」
        assert run.read_rows == 12
        snapshot = (
            db.query(RunEvent)
            .filter_by(run_id=run.id, event_type="run.budget_exceeded")
            .one()
        )
        assert snapshot.output_snapshot["breaches"][0]["limit"] == 5

    assert transport.calls[-1][1] == "/stop-job"


def test_worker_marks_a_budget_interrupted_job_as_failed_not_cancelled(
    session_factory, seatunnel_world
):
    """超预算是失败，不是取消——记成取消会把它和操作员的主动停止混为一谈。"""
    plane, _ = _plane(["RUNNING", "RUNNING"])
    with session_factory() as db:
        _rebudget(db, seatunnel_world, {"max_rows": 5})
        run = run_execution(
            db, run_id=seatunnel_world.run_id, worker_id=uuid.uuid4(), plane=plane
        )
        assert run.status is ExecutionRunStatus.FAILED
        assert run.status is not ExecutionRunStatus.CANCELLED


def test_cancel_arriving_before_the_final_poll_never_publishes(
    session_factory, seatunnel_world
):
    """FR-29：作业已结束才收到取消，仍然不得换表。

    换表发生在适配器内部，早于执行层对取消标志的事后复查。若在这里放行，
    运行会记成「已取消」而正式表其实已经被换掉——一条审计上的谎话。
    """
    spy = _Publisher()
    plane = SeaTunnelDataPlane(
        transport=_Transport(["RUNNING", "FINISHED"]),
        credentials=_Credentials(),
        publisher_factory=lambda db, command, credentials: spy,
        poll_interval=0.01,
        job_timeout=2,
        sleep=lambda _: None,
    )
    seen_running = {"value": False}

    def _cancel_at_the_end(status, _metrics):
        if status == "RUNNING":
            seen_running["value"] = True
            return _proceed()
        return PollDecision.cancel() if seen_running["value"] else _proceed()

    with session_factory() as db:
        with pytest.raises(DataplaneCancelled):
            plane.submit(seatunnel_world, db=db, on_poll=_cancel_at_the_end)

    assert getattr(spy, "published", False) is False
