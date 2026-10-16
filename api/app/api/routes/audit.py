"""审计查询接口（API-11 ~ API-13，FR-39 / FR-40）。

SPEC §6 的冻结契约（API-01~10）与平台支撑接口里都没有审计条目，这一组是按
PRD 第 7 章「审计台」的页面要素补的：按时间排序的事件链、哈希链可视化、
签名检查点状态、导出入口。

**全部只读**——审计面没有写方法（测试断言 POST/PUT/DELETE 一律 405）。唯一会
落库的动作是「跑一次校验」，它写的是 ``audit_verifications`` 校验历史，而不是
被校验的那条链。

角色：一律 ``require_project_member``。**不额外细分角色**——分页读得到的数据却
禁止导出只是安全剧场；真正的控制是 SQL 层的 ``project_id`` 强制过滤与落库前脱敏。
把「谁能批量取走证据」变成可追责事实的做法是让**导出行为本身入链**
（``audit.exported``），而不是加一道能被绕过的角色门槛。
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, require_project_member
from app.core.config import settings
from app.core.keys import get_signing_key
from app.db.types import as_utc
from app.models.audit import (
    AUDIT_CHAIN_ID,
    AuditChainHead,
    AuditRecord,
    IntegrityStatus,
)
from app.schemas.audit import (
    AuditChainHeadOut,
    AuditFailureOut,
    AuditRecordOut,
    AuditRecordPage,
    AuditVerificationOut,
)
from app.services.audit import record_audit
from app.services.audit_report import AuditScope, build_audit_report
from app.services.audit_verifier import (
    Failure,
    FailureCode,
    VerificationResult,
    latest_verification,
    record_verification,
    verify_chain,
)

router = APIRouter(prefix="/audit", tags=["audit"])


def _head_out(db: Session) -> AuditChainHeadOut:
    head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
    verification = latest_verification(db)
    return AuditChainHeadOut(
        seq=head.last_seq if head else 0,
        entry_hash=head.last_hash if head else "",
        integrity_status=head.integrity_status if head else "unverified",
        checked_at=(
            as_utc(verification.created_at) if verification is not None else None
        ),
    )


@router.get("/records", response_model=AuditRecordPage)
def list_records(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    event_type: Annotated[str | None, Query(max_length=64)] = None,
    actor: Annotated[str | None, Query(max_length=255)] = None,
    run_id: Annotated[uuid.UUID | None, Query()] = None,
    approval_id: Annotated[uuid.UUID | None, Query()] = None,
    version_id: Annotated[uuid.UUID | None, Query()] = None,
    preparation_id: Annotated[uuid.UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "asc",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> AuditRecordPage:
    """API-11：按条件检索证据链。

    **keyset 分页而不是 offset**：账本是一条持续在尾部追加的流，offset 分页在
    追加期间会漏读或重读；``after_seq`` 始终指向一个确定的位置。
    """
    require_project_member(project_id, user, db)

    # 租户隔离在 SQL 层强制——不是先查全量再在 Python 里过滤
    stmt = select(AuditRecord).where(AuditRecord.project_id == project_id)
    if event_type:
        stmt = stmt.where(AuditRecord.event_type == event_type)
    if actor:
        stmt = stmt.where(AuditRecord.actor == actor)
    if run_id:
        stmt = stmt.where(AuditRecord.run_id == run_id)
    if approval_id:
        stmt = stmt.where(AuditRecord.approval_id == approval_id)
    if version_id:
        stmt = stmt.where(AuditRecord.version_id == version_id)
    if preparation_id:
        stmt = stmt.where(AuditRecord.preparation_id == preparation_id)
    if since:
        stmt = stmt.where(AuditRecord.occurred_at >= since)
    if until:
        stmt = stmt.where(AuditRecord.occurred_at <= until)

    if order == "desc":
        if after_seq is not None:
            stmt = stmt.where(AuditRecord.seq <= after_seq)
        stmt = stmt.order_by(AuditRecord.seq.desc())
    else:
        if after_seq is not None:
            stmt = stmt.where(AuditRecord.seq > after_seq)
        stmt = stmt.order_by(AuditRecord.seq.asc())

    # 多取一条判断还有没有下一页——比再查一次 count 便宜，且不受并发追加影响
    rows = list(db.scalars(stmt.limit(limit + 1)))
    has_more = len(rows) > limit
    rows = rows[:limit]

    return AuditRecordPage(
        items=[AuditRecordOut.model_validate(row) for row in rows],
        next_after_seq=rows[-1].seq if has_more and rows else None,
        head=_head_out(db),
    )


def _verification_out(
    result: VerificationResult, *, trigger: str, stale: bool
) -> AuditVerificationOut:
    key = get_signing_key()
    return AuditVerificationOut(
        chain_id=AUDIT_CHAIN_ID,
        status=result.status.value,
        trigger=trigger,
        checked_at=datetime.now(UTC),
        checked=result.checked,
        from_seq=result.from_seq,
        to_seq=result.to_seq,
        head_seq=result.head_seq,
        head_hash=result.head_hash,
        anchors_checked=result.anchors_checked,
        anchors_verified=result.anchors_verified,
        first_failure_seq=result.first_failure_seq,
        failures=[
            AuditFailureOut(seq=item.seq, code=item.code.value, detail=item.detail)
            for item in result.failures
        ],
        unanchored_tail_seconds=result.unanchored_tail_seconds,
        duration_ms=result.duration_ms,
        public_key={
            "kid": key.kid,
            "algorithm": key.algorithm,
            "pem": key.public_key_pem().decode("ascii"),
        },
        stale=stale,
    )


@router.get("/verification", response_model=AuditVerificationOut)
def get_verification(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    refresh: Annotated[bool, Query()] = False,
) -> AuditVerificationOut:
    """API-12：链的完整性校验结论。

    ``refresh=true`` 现场跑一次完整校验并落一条校验历史；否则返回最近一次的持久化
    结论，并置 ``stale`` 标志——**审计台必须能区分「此刻没问题」与「三天前没问题」**。
    """
    require_project_member(project_id, user, db)

    if refresh:
        result = verify_chain(db, trigger="api")
        record_verification(db, result, trigger="api")
        return _verification_out(result, trigger="api", stale=False)

    head = db.get(AuditChainHead, AUDIT_CHAIN_ID)
    row = latest_verification(db)
    if row is None:
        result = verify_chain(db, trigger="api")
        record_verification(db, result, trigger="api")
        return _verification_out(result, trigger="api", stale=False)

    stale = head is not None and as_utc(head.updated_at) > as_utc(row.created_at)
    # 从校验历史还原结论。**失败明细必须一并带回**：只回一个状态的话，
    # 「链在第 N 条断了」这个排查起点就丢了，而那正是最该被看见的信息。
    failures = tuple(
        Failure(
            seq=int(item.get("seq") or 0),
            code=FailureCode(str(item.get("code"))),
            detail=str(item.get("detail") or ""),
        )
        for item in (row.failures or [])
    )
    result = VerificationResult(
        status=IntegrityStatus(row.status),
        checked=row.checked,
        from_seq=row.from_seq,
        to_seq=row.to_seq,
        head_seq=row.head_seq,
        head_hash=row.head_hash,
        anchors_checked=row.anchors_checked,
        anchors_verified=row.anchors_verified,
        first_failure_seq=row.first_failure_seq,
        failures=failures,
        unanchored_tail_seconds=row.unanchored_tail_seconds,
        duration_ms=row.duration_ms,
    )
    return _verification_out(result, trigger=row.trigger, stale=stale)


@router.get("/report")
def export_report(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    run_id: Annotated[uuid.UUID | None, Query()] = None,
    version_id: Annotated[uuid.UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    fmt: Annotated[str, Query(alias="format", pattern="^(json|ndjson)$")] = "json",
    include_payload: Annotated[bool, Query()] = True,
) -> Any:
    """API-13：导出自包含的审计报告（FR-40）。

    报告内嵌复算配方与公钥，持有它的第三方**无需访问本系统**即可独立重算整条链
    并验签。这是「审计证据」与「一份查询结果」的区别。
    """
    require_project_member(project_id, user, db)

    report = build_audit_report(
        db,
        scope=AuditScope(
            project_id=project_id,
            run_id=run_id,
            version_id=version_id,
            since=since,
            until=until,
        ),
    )
    payload = report.to_dict()
    if not include_payload:
        for item in payload["timeline"]:
            item["payload"] = None

    # 「谁批量取走了证据」本身就是审计事实。这是「查询与导出不限角色」的补偿控制：
    # 把导出变成可追责的动作，而不是加一道能被绕过的角色门槛。
    if settings.audit_export_records_event:
        record_audit(
            db,
            event_type="audit.exported",
            actor=str(user.id),
            project_id=project_id,
            run_id=run_id,
            payload={
                "format": fmt,
                "record_count": payload["scope"]["record_count"],
                "seq_range": payload["scope"]["seq_range"],
                "truncated": payload["scope"]["truncated"],
            },
        )
        db.commit()

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    filename = f"audit-{project_id}-{stamp}.{'ndjson' if fmt == 'ndjson' else 'json'}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if fmt == "ndjson":
        return StreamingResponse(
            report.iter_ndjson(), media_type="application/x-ndjson", headers=headers
        )
    return JSONResponse(payload, headers=headers)
