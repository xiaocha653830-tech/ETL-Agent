import uuid

from sqlalchemy.orm import Session

from app.models.benchmark import BenchmarkRun
from app.services.benchmark import BenchmarkReport


def save_report(
    db: Session,
    report: BenchmarkReport,
    *,
    created_by: uuid.UUID | None = None,
) -> BenchmarkRun:
    row = BenchmarkRun(
        created_by=created_by,
        dataset_version=report.dataset_version,
        dataset_sha256=report.dataset_sha256,
        backend=report.backend,
        cases_total=report.cases_total,
        cases_passed=report.cases_passed,
        operator_accuracy=report.operator_accuracy,
        attribute_accuracy=report.attribute_accuracy,
        guardrail_rate=report.guardrail_rate,
        passed=report.passed,
        detail=report.to_dict(),
        duration_ms=report.duration_ms,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
