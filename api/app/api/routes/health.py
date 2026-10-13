"""健康检查：存活（liveness）与就绪（readiness）。"""

from fastapi import APIRouter
from sqlalchemy import text

from app.api.deps import DbSession

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """存活探针：进程是否在跑。"""
    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: DbSession) -> dict:
    """就绪探针：依赖是否可用（当前校验数据库连通性）。"""
    db.execute(text("SELECT 1"))
    return {"status": "ready", "database": "up"}
