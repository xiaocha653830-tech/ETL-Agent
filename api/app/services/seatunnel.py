"""SeaTunnel 2.3.13 Zeta REST V2 数据面适配器（T-15）。

只从已冻结版本取得配置；运行时仅注入受管连接参数、凭据和影子表名。
提交副作用必须经过 Tool Broker 消费单次能力令牌，之后轮询终态与度量。
"""

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from pyhocon import ConfigFactory
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import DataplaneJobFailed, DataplaneProtocolError, DataplaneUnavailable
from app.models.datasource import Datasource, DatasourceType
from app.models.version import PipelineVersion, VersionStatus
from app.schemas.capability import BindingContext
from app.schemas.execution import ExecutionCommand
from app.services.budget import BudgetUsage, BudgetVerdict, evaluate
from app.services.commit import EXECUTION_TOOL
from app.services.credentials import CredentialService, get_credential_service
from app.services.execution import (
    BudgetExceeded,
    DataplaneCancelled,
    ExecutionOutcome,
    PollAction,
    PollCallback,
    PollDecision,
)
from app.services.quality import DorisPublisher, QualityReport, compile_quality_config
from app.services.tool_broker import InMemoryInvocationSink, ToolBroker
from app.services.versioning import compute_fingerprint

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r'"\$\{([A-Z][A-Z0-9_]*)\}"')
_ACTIVE = frozenset(
    {"CREATED", "PENDING", "SCHEDULED", "RUNNING", "RESTARTING", "FAILING", "CANCELLING"}
)
_FAILED = frozenset({"FAILED", "CANCELED", "UNKNOWABLE", "SAVEPOINT_DONE"})


class SeatunnelTransport(Protocol):
    def request(
        self, method: str, path: str, *, body: str | dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


class Publisher(Protocol):
    def prepare(self, *, with_quality_rules: bool) -> None: ...
    def report(
        self, *, source_rows: int, budget: dict[str, Any], with_quality_rules: bool
    ) -> QualityReport: ...
    def rejected_rows(self) -> int | None: ...
    def publish(self) -> None: ...
    def close(self) -> None: ...


class UrllibSeatunnelTransport:
    """同步 HTTP 传输；Worker 是同步任务，无需引入额外生产依赖。"""

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("SEATUNNEL_BASE_URL 必须是无用户信息的 HTTP(S) 地址")
        if settings.app_env not in ("dev", "demo") and parsed.scheme != "https":
            # HOCON 请求体会在内存中包含运行时凭据，生产网络不得以 HTTP 传输。
            raise ValueError("非开发/演示环境的 SeaTunnel REST 地址必须使用 HTTPS")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def request(
        self, method: str, path: str, *, body: str | dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if isinstance(body, str):
            data = body.encode("utf-8")
            content_type = "text/plain; charset=utf-8"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            content_type = "application/json"
        else:
            data = None
            content_type = None
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(
            self._base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read(1_000_001)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            # HTTP 错误体可能回显提交的连接参数或口令，绝不向日志和运行记录透传。
            logger.warning(
                "SeaTunnel REST 调用失败 method=%s path=%s kind=%s",
                method,
                path.split("?")[0],
                type(exc).__name__,
            )
            raise DataplaneUnavailable("SeaTunnel REST 请求失败") from None
        if len(raw) > 1_000_000:
            raise DataplaneProtocolError("SeaTunnel 响应超过大小限制")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DataplaneProtocolError("SeaTunnel 返回了非 JSON 响应") from None
        if not isinstance(payload, dict):
            raise DataplaneProtocolError("SeaTunnel 响应结构错误")
        return payload


def _job_id(payload: dict[str, Any]) -> int:
    value = payload.get("jobId")
    if isinstance(value, bool) or not str(value or "").isdigit() or int(value) <= 0:
        raise DataplaneProtocolError("SeaTunnel 响应缺少有效 jobId")
    return int(value)


def _metric(metrics: dict[str, Any], name: str) -> int:
    try:
        return max(0, int(metrics.get(name) or 0))
    except (TypeError, ValueError):
        raise DataplaneProtocolError(f"SeaTunnel 度量 {name} 无效") from None


def _optional_metric(metrics: dict[str, Any], name: str) -> int | None:
    """缺失即 ``None``（不可判定），而不是 ``0``（判定为没有）——两者语义不同。

    引擎版本差异会让某个度量消失，把「没这个数」读成「这个数是零」，
    依赖它的比值维度就会得到一个人为的、看起来完全正常的结论。
    """
    if metrics.get(name) is None:
        return None
    return _metric(metrics, name)


def _stop_exception(decision: PollDecision) -> Exception:
    """把非 CONTINUE 的轮询结论翻译成对应的停止语义。

    只有确认 ``decision`` 不是 CONTINUE 的调用方才该调用它。
    """
    if decision.action is PollAction.BUDGET_EXCEEDED:
        return BudgetExceeded(
            reason=decision.reason or "运行超出批准预算",
            snapshot=dict(decision.snapshot or {}),
        )
    return DataplaneCancelled()


class _BudgetWatch:
    """把 SeaTunnel 度量翻译成预算观测值，并逐次判定（FR-35）。

    **度量名到维度的映射只在这里出现一次**：预算模块不认识 SeaTunnel，它只
    接收已经翻译好的数字。引擎换度量名时，改动被限制在这一处，判据本身不动。
    """

    def __init__(
        self,
        *,
        budget: dict[str, Any],
        clock: Callable[[], float],
        rejected_rows: Callable[[], int | None] | None = None,
    ) -> None:
        self._budget = budget
        self._clock = clock
        self._rejected_rows = rejected_rows
        # 以首次轮询为计时起点：它紧跟在作业提交之后，因此时长不含控制面
        # 渲染配置、建影子表这些准备耗时——那部分不该记在数据面作业头上。
        self._started_at: float | None = None

    def observe(self, metrics: dict[str, Any]) -> BudgetVerdict:
        now = self._clock()
        if self._started_at is None:
            self._started_at = now
        rejected = self._rejected_rows() if self._rejected_rows is not None else None
        usage = BudgetUsage(
            read_rows=_metric(metrics, "SourceReceivedCount"),
            written_bytes=_metric(metrics, "SinkCommittedBytes")
            or _metric(metrics, "SinkWriteBytes"),
            read_bytes=_optional_metric(metrics, "SourceReceivedBytes"),
            rejected_rows=rejected,
            elapsed_seconds=max(0.0, now - self._started_at),
        )
        return evaluate(self._budget, usage)


def _jdbc_url(datasource: Datasource) -> str:
    configured = str((datasource.config or {}).get("url") or "")
    if configured:
        return configured
    host = str((datasource.config or {}).get("host") or "")
    database = str((datasource.config or {}).get("database") or "")
    port = (datasource.config or {}).get("port")
    dialect = {
        DatasourceType.MYSQL: "mysql",
        DatasourceType.DORIS: "mysql",
        DatasourceType.POSTGRESQL: "postgresql",
    }.get(datasource.type)
    if not dialect or not host or not database:
        return ""
    return f"jdbc:{dialect}://{host}{f':{port}' if port else ''}/{database}"


def _endpoint_values(
    datasource: Datasource, *, credentials: CredentialService
) -> dict[str, str]:
    config = datasource.config or {}
    secret = credentials.resolve(datasource.credential_ref) if datasource.credential_ref else {}
    host = str(config.get("host") or "")
    port = str(config.get("port") or "")
    values = {
        "URL": _jdbc_url(datasource),
        "HOST": host,
        "PORT": port,
        "FENODES": str(config.get("fenodes") or (f"{host}:{port or '8030'}" if host else "")),
        "USER": str(secret.get("username") or secret.get("user") or ""),
        "USERNAME": str(secret.get("username") or secret.get("user") or ""),
        "PASSWORD": str(secret.get("password") or ""),
        "TOKEN": str(secret.get("token") or ""),
        "ACCESS_KEY": str(secret.get("access_key") or ""),
        "SECRET_KEY": str(secret.get("secret_key") or ""),
    }
    return values


def render_runtime_config(
    db: Session, command: ExecutionCommand, *, credentials: CredentialService
) -> str:
    """校验冻结制品并只替换受管占位符，绝不采用模型填写的目标表。"""
    version = db.get(PipelineVersion, command.version_id)
    if version is None or version.status is not VersionStatus.FROZEN:
        raise DataplaneProtocolError("执行版本不存在或尚未冻结")
    actual = compute_fingerprint(version.etl_plan, version.config, version.quality_rules)
    if actual != command.artifact or version.sha256 != actual:
        raise DataplaneProtocolError("执行制品指纹与冻结版本不一致")
    plan = version.etl_plan or {}
    try:
        source_id = uuid.UUID(str(plan["source_datasource_id"]))
        target_id = uuid.UUID(str(plan["target_datasource_id"]))
    except (KeyError, ValueError):
        raise DataplaneProtocolError("冻结方案缺少有效数据源标识") from None
    source, target = db.get(Datasource, source_id), db.get(Datasource, target_id)
    if (
        source is None or target is None
        or source.project_id != version.project_id
        or target.project_id != version.project_id
        or target.type is not DatasourceType.DORIS
    ):
        raise DataplaneProtocolError("数据源不存在、跨项目或目标端不是 Doris")
    if not command.shadow_table or command.shadow_table == command.target_table:
        raise DataplaneProtocolError("准备单未指定独立影子表")
    source_values = _endpoint_values(source, credentials=credentials)
    target_values = _endpoint_values(target, credentials=credentials)
    values = {
        **{f"SOURCE_{key}": value for key, value in source_values.items()},
        **{f"TARGET_{key}": value for key, value in target_values.items()},
        "TARGET_TABLE": command.shadow_table,
        "RUN_ID": str(command.run_id),
    }

    def inject(match: re.Match[str]) -> str:
        key = match.group(1)
        value = values.get(key)
        # Doris 演示环境的 root 账号允许空口令；缺少端点或用户名仍须拒绝。
        if value is None or (not value and not key.endswith("_PASSWORD")):
            raise DataplaneProtocolError(f"运行时缺少 {key} 的受管参数")
        return json.dumps(value, ensure_ascii=False)

    # SeaTunnel 会在前缀后再拼库名、表名、jobId；完整 UUID 会让长影子表超过
    # Doris 的 128 字符标签上限。jobId 仍保证全局唯一，前缀保留 12 位供排障。
    short_label = f"etl-{command.run_id.hex[:12]}"
    rendered = _PLACEHOLDER.sub(inject, version.config).replace(
        "etl-${RUN_ID}", short_label
    ).replace(
        "etl-${RUN_LABEL}", short_label
    )
    unresolved = [
        line for line in rendered.splitlines()
        if not line.lstrip().startswith("#") and "${" in line
    ]
    if unresolved or "${TARGET_TABLE}" not in version.config:
        raise DataplaneProtocolError("冻结配置存在未解析参数或未绑定影子表")
    try:
        parsed = ConfigFactory.parse_string(rendered)
        actual_table = parsed["sink"]["Doris"]["table"]
    except Exception:  # noqa: BLE001 —— 解析异常可能包含含凭据的原文
        raise DataplaneProtocolError("运行时配置不是有效的 Doris HOCON") from None
    if actual_table != command.shadow_table:
        raise DataplaneProtocolError("Doris Sink 未指向批准的影子表")
    return rendered


class SeaTunnelDataPlane:
    """受管提交、轮询、度量回传与协作式停止。"""

    def __init__(
        self,
        transport: SeatunnelTransport | None = None,
        *,
        credentials: CredentialService | None = None,
        poll_interval: float | None = None,
        job_timeout: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        publisher_factory: Callable[[Session, ExecutionCommand, CredentialService], Publisher]
        | None = None,
    ) -> None:
        self._transport = transport or UrllibSeatunnelTransport(
            settings.seatunnel_base_url, settings.seatunnel_http_timeout_seconds
        )
        self._credentials = credentials or get_credential_service()
        self._poll_interval = poll_interval or settings.seatunnel_poll_interval_seconds
        self._job_timeout = job_timeout or settings.seatunnel_job_timeout_seconds
        if self._poll_interval <= 0 or self._job_timeout <= 0:
            raise ValueError("SeaTunnel 轮询间隔和作业等待时限必须大于零")
        self._clock = clock
        self._sleep = sleep
        self._publisher_factory = publisher_factory or DorisPublisher
        self._invocations = InMemoryInvocationSink()

    def _submit_http(self, *, config: str, job_name: str) -> dict[str, Any]:
        path = "/submit-job?" + urlencode({"format": "hocon", "jobName": job_name})
        return self._transport.request("POST", path, body=config)

    def _stop(self, job_id: int) -> None:
        reply = self._transport.request(
            "POST", "/stop-job", body={"jobId": job_id, "isStopWithSavePoint": False}
        )
        if _job_id(reply) != job_id:
            raise DataplaneProtocolError("SeaTunnel 停止作业回执不匹配")

    def _stop_after_query_error(self, job_id: int) -> None:
        """状态已不可确认时尽力停止，失败则保留 jobId 事件供人工排查。"""
        try:
            self._stop(job_id)
        except (DataplaneUnavailable, DataplaneProtocolError):
            logger.warning("SeaTunnel 状态查询失败后无法确认停止 job_id=%s", job_id)

    def submit(
        self,
        command: ExecutionCommand,
        *,
        db: Session,
        on_poll: PollCallback,
    ) -> ExecutionOutcome:
        if command.tool != EXECUTION_TOOL:
            raise DataplaneProtocolError("执行命令未绑定 SeaTunnel 提交工具")
        config = render_runtime_config(db, command, credentials=self._credentials)
        version = db.get(PipelineVersion, command.version_id)
        rules = list(version.quality_rules or [])
        config = compile_quality_config(
            config,
            source_table=str((version.etl_plan or {}).get("source_table") or ""),
            shadow_table=command.shadow_table,
            rules=rules,
        )

        # 每次运行独立注册闭包，避免并发 Worker 复用同一工具处理器时串作业。
        broker = ToolBroker(sink=self._invocations)

        def authorized_run(**_arguments: Any) -> ExecutionOutcome:
            publisher = self._publisher_factory(db, command, self._credentials)
            try:
                publisher.prepare(with_quality_rules=bool(rules))
                watch = _BudgetWatch(
                    budget=command.budget,
                    clock=self._clock,
                    # 没有质量规则就没有错误表，拒绝率维度自然不可判定
                    rejected_rows=publisher.rejected_rows if rules else None,
                )

                def governed_poll(status: str, metrics: dict[str, Any]) -> PollDecision:
                    # 先让执行层续租并记录进度：这些是「实际发生了什么」的账，
                    # 与预算判定无关，越界的那一轮同样要记——否则运行行上会永远
                    # 停在越界之前的那个数字，看不出作业究竟跑到了哪一步。
                    requested = on_poll(status, metrics)
                    # 预算结论压过取消结论：超预算是必须留痕的事实，而取消请求
                    # 已经由 request_cancel 记在运行行与 run.cancel_requested 事件上，
                    # 不会被覆盖；反过来把超预算记成「取消」，这条事实就没有载体了。
                    verdict = watch.observe(metrics)
                    if verdict.exceeded:
                        return PollDecision.budget_exceeded(
                            reason=f"超预算硬中断：{verdict.headline}",
                            snapshot=verdict.as_snapshot(),
                        )
                    return requested

                outcome = self._execute_job(config, command, governed_poll)
                report = publisher.report(
                    source_rows=outcome.read_rows,
                    budget=command.budget,
                    with_quality_rules=bool(rules),
                )
                publisher.publish()
                return ExecutionOutcome(
                    read_rows=outcome.read_rows,
                    written_bytes=outcome.written_bytes,
                    rejected_rows=report.rejected_rows,
                    detail={
                        **(outcome.detail or {}),
                        "quality_report": report.as_dict(),
                        "published": True,
                    },
                )
            finally:
                publisher.close()

        broker.register_tool(EXECUTION_TOOL, authorized_run)
        return broker.execute(
            EXECUTION_TOOL,
            {
                "run_id": str(command.run_id),
                "shadow_table": command.shadow_table,
                "config_sha256": hashlib.sha256(config.encode("utf-8")).hexdigest(),
            },
            command.capability_token,
            db=db,
            expected_binding=BindingContext(
                tool=command.tool,
                subject=command.subject,
                environment=command.environment,
                artifact=command.artifact,
            ),
        )

    def _execute_job(
        self,
        config: str,
        command: ExecutionCommand,
        on_poll: PollCallback,
    ) -> ExecutionOutcome:
        reply = self._submit_http(config=config, job_name=f"etl-{command.run_id}")
        job_id = _job_id(reply)
        # 首次查询失败也必须留住 jobId，供运维定位可能仍在运行的作业。
        decision = on_poll("SUBMITTED", {"jobId": job_id})
        if decision.action is not PollAction.CONTINUE:
            self._stop(job_id)
            raise _stop_exception(decision)
        deadline = self._clock() + self._job_timeout
        while True:
            try:
                info = self._transport.request("GET", f"/job-info/{job_id}")
            except (DataplaneUnavailable, DataplaneProtocolError):
                self._stop_after_query_error(job_id)
                raise
            if _job_id(info) != job_id:
                raise DataplaneProtocolError("SeaTunnel 查询回执 jobId 不匹配")
            status = str(info.get("jobStatus") or "").upper()
            metrics = info.get("metrics") or {}
            if not isinstance(metrics, dict):
                raise DataplaneProtocolError("SeaTunnel 度量结构错误")
            decision = on_poll(status, metrics)
            if status == "FINISHED":
                # 作业已经结束，无需（也无法）停止；但结论不能就此丢掉——
                # 放行意味着换表，而换表之后就没有回退点了。
                if decision.action is not PollAction.CONTINUE:
                    raise _stop_exception(decision)
                return ExecutionOutcome(
                    read_rows=_metric(metrics, "SourceReceivedCount"),
                    written_bytes=_metric(metrics, "SinkCommittedBytes")
                    or _metric(metrics, "SinkWriteBytes"),
                    # 引擎不统计质量分流结果；拒绝行数由 DorisPublisher.report
                    # 在发布前从错误表实测，此处只是满足运行表非空约束的占位。
                    rejected_rows=0,
                    detail={"job_id": job_id, "job_status": status},
                )
            if status in _FAILED:
                raise DataplaneJobFailed(f"SeaTunnel 作业进入 {status} 终态")
            if status not in _ACTIVE:
                raise DataplaneProtocolError("SeaTunnel 返回未知作业状态")
            if decision.action is not PollAction.CONTINUE or self._clock() >= deadline:
                self._stop(job_id)
                if decision.action is not PollAction.CONTINUE:
                    raise _stop_exception(decision)
                raise DataplaneJobFailed("SeaTunnel 作业超过等待时限，已请求停止")
            self._sleep(self._poll_interval)
