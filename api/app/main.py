"""控制面 API 应用入口（SPEC §3 M1）。

启动：
    uvicorn app.main:app --reload --port 8000
接口文档：
    http://127.0.0.1:8000/docs
"""

from fastapi import FastAPI

from app.api.routes import auth, health
from app.core.config import settings

API_V1 = "/api/v1"

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="生产级 ETL-Agent 数据集成平台 — 控制面 API",
)

# 健康检查挂在根路径，供容器探针使用
app.include_router(health.router)
# 业务接口统一 /api/v1 前缀
app.include_router(auth.router, prefix=API_V1)
