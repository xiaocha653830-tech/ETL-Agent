"""平台级 Benchmark 结果，不绑定租户业务数据或项目。"""

import uuid

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.types import JsonColumn


class BenchmarkRun(Base, TimestampMixin):
    __tablename__ = "benchmark_runs"
    __table_args__ = (
        CheckConstraint("cases_total > 0", name="ck_benchmark_nonempty"),
        CheckConstraint(
            "cases_passed >= 0 AND cases_passed <= cases_total", name="ck_benchmark_case_counts"
        ),
        CheckConstraint(
            "operator_accuracy >= 0 AND operator_accuracy <= 1", name="ck_benchmark_operator_ratio"
        ),
        CheckConstraint(
            "attribute_accuracy >= 0 AND attribute_accuracy <= 1",
            name="ck_benchmark_attribute_ratio",
        ),
        CheckConstraint(
            "guardrail_rate >= 0 AND guardrail_rate <= 1", name="ck_benchmark_guardrail_ratio"
        ),
        Index("ix_benchmark_dataset_backend_created", "dataset_version", "backend", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    dataset_version: Mapped[str] = mapped_column(String(32), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    backend: Mapped[str] = mapped_column(String(16), nullable=False)
    cases_total: Mapped[int] = mapped_column(Integer, nullable=False)
    cases_passed: Mapped[int] = mapped_column(Integer, nullable=False)
    operator_accuracy: Mapped[float] = mapped_column(Float, nullable=False)
    attribute_accuracy: Mapped[float] = mapped_column(Float, nullable=False)
    guardrail_rate: Mapped[float] = mapped_column(Float, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict] = mapped_column(JsonColumn, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
