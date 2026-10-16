"""平台级评测 API。默认 L0；backend=deepseek 是明确的付费模型调用请求。"""

import uuid

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.errors import PermissionDenied, ResourceConflict, ResourceNotFound
from app.models.benchmark import BenchmarkRun
from app.models.identity import Membership, UserRole
from app.schemas.benchmark import BenchmarkCreate, BenchmarkOut, BenchmarkSummaryOut
from app.services.benchmark import DatasetError, load_dataset, run_benchmark
from app.services.benchmark_store import save_report

router = APIRouter(prefix="/benchmark/runs", tags=["benchmark"])


@router.post("", response_model=BenchmarkOut, status_code=status.HTTP_201_CREATED)
def create(payload: BenchmarkCreate, user: CurrentUser, db: DbSession) -> BenchmarkRun:
    maker = db.scalar(
        select(Membership.id)
        .where(
            Membership.user_id == user.id,
            Membership.role == UserRole.MAKER,
        )
        .limit(1)
    )
    if maker is None:
        raise PermissionDenied("触发平台评测需要至少一个项目的工程师角色")
    try:
        dataset = load_dataset(payload.dataset_version)
    except DatasetError as exc:
        raise ResourceConflict(str(exc)) from None
    report = run_benchmark(dataset, backend=payload.backend)
    return save_report(db, report, created_by=user.id)


@router.get("", response_model=list[BenchmarkSummaryOut])
def list_runs(
    user: CurrentUser,
    db: DbSession,
    dataset_version: str | None = Query(default=None, pattern=r"^v[1-9][0-9]{0,5}$"),
    backend: str | None = Query(default=None, pattern=r"^(injected|deepseek)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[BenchmarkRun]:
    stmt = select(BenchmarkRun)
    if dataset_version:
        stmt = stmt.where(BenchmarkRun.dataset_version == dataset_version)
    if backend:
        stmt = stmt.where(BenchmarkRun.backend == backend)
    return list(
        db.scalars(
            stmt.order_by(BenchmarkRun.created_at.desc(), BenchmarkRun.id.desc()).limit(limit)
        )
    )


@router.get("/{run_id}", response_model=BenchmarkOut)
def get_run(run_id: uuid.UUID, user: CurrentUser, db: DbSession) -> BenchmarkRun:
    row = db.get(BenchmarkRun, run_id)
    if row is None:
        raise ResourceNotFound("评测结果不存在")
    return row
