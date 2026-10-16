"""控制面 API 应用入口（SPEC §3 M1）。

启动：
    uvicorn app.main:app --reload --port 8000
接口文档：
    http://127.0.0.1:8000/docs
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import (
    approvals,
    audit,
    auth,
    commits,
    datasources,
    health,
    pipelines,
    projects,
    runs,
    versions,
)
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.db.session import SessionLocal
from app.registry import router as registry_router
from app.services.audit_verifier import record_verification, verify_chain
from app.services.commit import EXECUTION_TOOL
from app.services.pdp import HighRiskToolPolicy, get_pdp

logger = logging.getLogger(__name__)

API_V1 = "/api/v1"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动期的一次性动作。

    SPEC §8 要求校验器「启动时与定期巡检」，这里是前者（后者是 Celery 的
    ``audit.verify`` 任务）。**校验失败不阻断启动**：把「检测到篡改」变成一次
    可用性故障，等于给攻击者一个 DoS 杠杆——他只要改一条记录，整个系统就起不来，
    而运维连通过接口查看证据的路径都没了。阻断落在授权类动作的入口上
    （``commit_preparation`` / ``rollback_run``），那里的粒度才是有意义的。
    """
    if settings.audit_verify_on_startup:
        try:
            with SessionLocal() as db:
                result = verify_chain(db, trigger="startup")
                record_verification(db, result, trigger="startup")
        except Exception:  # noqa: BLE001 —— 启动校验绝不能反过来挡住启动
            logger.exception("启动时证据链校验失败；不阻断启动，请检查数据库可达性")
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="生产级 ETL-Agent 数据集成平台 — 控制面 API",
    lifespan=lifespan,
)

# 全局异常处理器：所有错误出口统一为 {code, message, detail}
register_exception_handlers(app)

# 安全内核策略装配（T-12）：执行工具属高风险，PDP 要求令牌必须绑定具体制品。
# 策略在装配期一次性注册，运行期只做判定——判定路径上不再有可变的策略集合。
get_pdp().register_policy(
    HighRiskToolPolicy(high_risk_tools=frozenset({EXECUTION_TOOL}))
)

# 健康检查挂在根路径，供容器探针使用
app.include_router(health.router)

# 业务接口统一 /api/v1 前缀
app.include_router(auth.router, prefix=API_V1)
app.include_router(projects.router, prefix=API_V1)
app.include_router(datasources.router, prefix=API_V1)
app.include_router(pipelines.router, prefix=API_V1)
app.include_router(versions.router, prefix=API_V1)
# M6 审批与授权：Prepare 准备单与双维审批（API-06 / API-07，FR-21 ~ FR-24）
app.include_router(approvals.preparation_router, prefix=API_V1)
app.include_router(approvals.approval_router, prefix=API_V1)
# M6 Commit 收口与 M8 异步执行：提交授权、查询运行状态（API-08 / API-09）
app.include_router(commits.router, prefix=API_V1)
app.include_router(runs.router, prefix=API_V1)
# M4 算子能力注册表：只读清单，供 M3 生成时约束算子空间（FR-11 / FR-14）
app.include_router(registry_router, prefix=API_V1)
# M11 审计与证据账本：检索事件链、校验链完整性、导出审计报告（API-11 ~ API-13）
app.include_router(audit.router, prefix=API_V1)
