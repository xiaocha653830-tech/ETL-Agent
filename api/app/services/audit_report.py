"""审计报告装配（API-13，FR-40）。

报告的**全部价值**在于它自带验证配方：字段序、序列化参数、时间格式、创世哈希、
签名信封版本，以及验签用的公钥。第三方拿到这份文件就能独立复算整条链并验签，
**不需要访问本系统**。

一份要回查数据库才能验证的报告不算审计报告——它把证据的可信度重新押在了一张
可被改写的表上，而这张表恰恰是审计要防的东西。

``subjects`` 把审批链、准备单、执行记录与质量报告一并带上（FR-40 要求审计人员
看到的是「完整审批链」而不只是哈希）。它们是从业务表现查的**副本**，因此报告里
同时留着账本中对应事件的 ``payload``——两者不一致本身就是一条线索。
"""

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.keys import get_signing_key
from app.db.types import as_utc
from app.models.approval import Approval, Preparation
from app.models.audit import (
    AUDIT_CHAIN_ID,
    GENESIS_HASH,
    AuditChainHead,
    AuditRecord,
    IntegrityStatus,
)
from app.models.execution import ExecutionRun, RunEvent
from app.models.quality import QualityReport
from app.services.audit import (
    HASH_FIELD_ORDER,
    SIGNATURE_VERSION,
    TIMESTAMP_FORMAT,
)
from app.services.audit_verifier import latest_verification

logger = logging.getLogger(__name__)

REPORT_VERSION = "audit-report-v1"


@dataclass(frozen=True)
class AuditScope:
    """报告覆盖的范围。"""

    project_id: uuid.UUID
    run_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = None


@dataclass(frozen=True)
class AuditReport:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.payload

    def iter_ndjson(self) -> Iterator[str]:
        """逐行流式输出。

        **最后一行是 summary**：流式导出被截断时，消费方能立刻发现报告不完整。
        否则一个被截断的 NDJSON 看起来就像一份「到此为止」的完整报告——那比
        没有报告更危险。
        """
        import json

        for item in self.payload["timeline"]:
            yield json.dumps({"kind": "record", **item}, ensure_ascii=False) + "\n"
        for item in self.payload["checkpoints"]:
            yield json.dumps({"kind": "checkpoint", **item}, ensure_ascii=False) + "\n"
        summary = {
            "kind": "summary",
            "report_version": self.payload["report_version"],
            "scope": self.payload["scope"],
            "chain": self.payload["chain"],
        }
        yield json.dumps(summary, ensure_ascii=False) + "\n"


def _record_view(record: AuditRecord) -> dict[str, Any]:
    return {
        "seq": record.seq,
        "event_type": record.event_type,
        "actor": record.actor,
        "occurred_at": as_utc(record.occurred_at).isoformat(),
        "project_id": str(record.project_id) if record.project_id else None,
        "run_id": str(record.run_id) if record.run_id else None,
        "approval_id": str(record.approval_id) if record.approval_id else None,
        "version_id": str(record.version_id) if record.version_id else None,
        "preparation_id": str(record.preparation_id) if record.preparation_id else None,
        "payload": record.payload,
        "prev_hash": record.prev_hash,
        "entry_hash": record.entry_hash,
        "signature": record.signature,
        "anchored": record.signature is not None,
    }


def _subjects(db: Session, scope: AuditScope) -> dict[str, Any]:
    """按范围把审批链、准备单、执行记录与质量报告带进报告。

    只带**摘要**：报告要能自证其证据链，而不是变成一份业务库的全量导出。
    """
    subjects: dict[str, Any] = {
        "preparations": [],
        "approvals": [],
        "execution_runs": [],
        "run_events": [],
        "quality_reports": [],
    }
    if scope.run_id is None and scope.version_id is None:
        return subjects

    runs = list(
        db.scalars(
            select(ExecutionRun).where(
                ExecutionRun.project_id == scope.project_id,
                *(
                    [ExecutionRun.id == scope.run_id]
                    if scope.run_id
                    else [ExecutionRun.version_id == scope.version_id]
                ),
            )
        )
    )
    run_ids = [run.id for run in runs]
    subjects["execution_runs"] = [
        {
            "id": str(run.id),
            "version_id": str(run.version_id),
            "status": run.status.value,
            "artifact": run.artifact,
            "read_rows": run.read_rows,
            "written_bytes": run.written_bytes,
            "rejected_rows": run.rejected_rows,
            "published_at": run.published_at.isoformat() if run.published_at else None,
            "rolled_back_at": (
                run.rolled_back_at.isoformat() if run.rolled_back_at else None
            ),
        }
        for run in runs
    ]

    if run_ids:
        preparation_ids = {run.preparation_id for run in runs}
        preparations = list(
            db.scalars(select(Preparation).where(Preparation.id.in_(preparation_ids)))
        )
        subjects["preparations"] = [
            {
                "id": str(item.id),
                "version_id": str(item.version_id),
                "status": item.status.value,
                "risk_level": item.risk_level.value,
                "risk_reasons": item.risk_reasons,
                "fingerprint": item.fingerprint,
                "impact_scope": item.impact_scope,
                "budget": item.budget,
                "expires_at": item.expires_at.isoformat(),
                "committed_at": (
                    item.committed_at.isoformat() if item.committed_at else None
                ),
            }
            for item in preparations
        ]
        approvals = list(
            db.scalars(
                select(Approval).where(Approval.preparation_id.in_(preparation_ids))
            )
        )
        subjects["approvals"] = [
            {
                "id": str(item.id),
                "preparation_id": str(item.preparation_id),
                "dimension": item.dimension.value,
                "decision": item.decision.value if item.decision else None,
                "approver_id": str(item.approver_id) if item.approver_id else None,
                "comment": item.comment,
                "decided_at": item.decided_at.isoformat() if item.decided_at else None,
            }
            for item in approvals
        ]
        events = list(
            db.scalars(
                select(RunEvent)
                .where(RunEvent.run_id.in_(run_ids))
                .order_by(RunEvent.created_at)
            )
        )
        subjects["run_events"] = [
            {
                "run_id": str(item.run_id),
                "event_type": item.event_type,
                "message": item.message,
                "created_at": as_utc(item.created_at).isoformat(),
            }
            for item in events
        ]
        reports = list(
            db.scalars(select(QualityReport).where(QualityReport.run_id.in_(run_ids)))
        )
        subjects["quality_reports"] = [
            {
                "run_id": str(item.run_id),
                "source_rows": item.source_rows,
                "valid_rows": item.valid_rows,
                "rejected_rows": item.rejected_rows,
                "rejected_ratio": item.rejected_ratio,
                "error_dist": item.error_dist,
                "error_table": item.error_table,
            }
            for item in reports
        ]

    return subjects


def build_audit_report(db: Session, *, scope: AuditScope) -> AuditReport:
    """装配一份自包含的审计报告。"""
    conditions = [AuditRecord.project_id == scope.project_id]
    if scope.run_id is not None:
        conditions.append(AuditRecord.run_id == scope.run_id)
    if scope.version_id is not None:
        conditions.append(AuditRecord.version_id == scope.version_id)
    if scope.since is not None:
        conditions.append(AuditRecord.occurred_at >= scope.since)
    if scope.until is not None:
        conditions.append(AuditRecord.occurred_at <= scope.until)

    limit = min(
        scope.limit or settings.audit_report_max_records,
        settings.audit_report_max_records,
    )
    # 多取一条用来判断「是否被截断」——**绝不用沉默的截断冒充完整报告**
    records = list(
        db.scalars(
            select(AuditRecord)
            .where(*conditions)
            .order_by(AuditRecord.seq)
            .limit(limit + 1)
        )
    )
    truncated = len(records) > limit
    records = records[:limit]

    key = get_signing_key()
    head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
    verification = latest_verification(db)
    stale = True
    verification_block: dict[str, Any] = {"status": IntegrityStatus.UNVERIFIED.value}
    if verification is not None:
        if head is not None:
            stale = as_utc(head.updated_at) > as_utc(verification.created_at)
        verification_block = {
            "status": verification.status,
            "trigger": verification.trigger,
            "checked_at": as_utc(verification.created_at).isoformat(),
            "checked": verification.checked,
            "from_seq": verification.from_seq,
            "to_seq": verification.to_seq,
            "anchors_checked": verification.anchors_checked,
            "anchors_verified": verification.anchors_verified,
            "first_failure_seq": verification.first_failure_seq,
            "failures": verification.failures or [],
        }

    timeline = [_record_view(record) for record in records]
    checkpoints = [
        {
            "seq": item["seq"],
            "entry_hash": item["entry_hash"],
            "anchored_at": item["occurred_at"],
            "signature": item["signature"],
        }
        for item in timeline
        if item["anchored"]
    ]

    payload = {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "project_id": str(scope.project_id),
            "run_id": str(scope.run_id) if scope.run_id else None,
            "version_id": str(scope.version_id) if scope.version_id else None,
            "since": scope.since.isoformat() if scope.since else None,
            "until": scope.until.isoformat() if scope.until else None,
            "record_count": len(timeline),
            "seq_range": (
                [timeline[0]["seq"], timeline[-1]["seq"]] if timeline else [None, None]
            ),
            "truncated": truncated,
        },
        "chain": {
            # ---- 复算配方：没有这一段，报告就只是一份需要回查数据库的清单 ----
            "chain_id": AUDIT_CHAIN_ID,
            "algorithm": "sha256",
            "canonicalization": "json-ordered-fields-v1",
            "field_order": list(HASH_FIELD_ORDER),
            "timestamp_format": TIMESTAMP_FORMAT,
            "genesis_hash": GENESIS_HASH,
            "signature_version": SIGNATURE_VERSION,
            "signature_format": "v1:<kid>:<base64url-nopad>",
            # ---- 当前链头与校验结论 ----
            "head_seq": head.last_seq if head else 0,
            "head_hash": head.last_hash if head else GENESIS_HASH,
            "integrity_status": (
                head.integrity_status if head else IntegrityStatus.UNVERIFIED.value
            ),
            "verification": verification_block,
            "verification_stale": stale,
            "public_key": {
                "kid": key.kid,
                "algorithm": key.algorithm,
                "pem": key.public_key_pem().decode("ascii"),
            },
        },
        "checkpoints": checkpoints,
        "timeline": timeline,
        "subjects": _subjects(db, scope),
    }
    return AuditReport(payload=payload)
