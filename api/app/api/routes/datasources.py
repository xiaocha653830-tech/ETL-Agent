"""数据源登记、连通性测试与元数据探查（FR-01 ~ FR-07）。

对应 SPEC §6 的 API-01（登记）与 API-02（探查）。

**凭据纪律**：明文凭据只在 ``POST /datasources`` 的请求体里出现一次，
随即被转入凭据服务（FR-03）；此后所有响应只回 ``has_credential`` 与不可逆指纹。
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, DbSession, ensure_role, require_project_member
from app.core.errors import AppError, DatasourceUnreachable, ResourceNotFound
from app.models.datasource import Datasource, DatasourceStatus, MetadataProfile
from app.models.identity import Membership, User, UserRole
from app.schemas.datasource import (
    ConnectionTestOut,
    DatasourceCreate,
    DatasourceOut,
    MetadataProfileOut,
)
from app.services.credentials import CredentialService, get_credential_service
from app.services.datasource import (
    ConnectionSpec,
    SqlAlchemyMetadataSource,
    build_engine,
    profile_datasource,
    test_connection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/datasources", tags=["datasources"])

Credentials = Annotated[CredentialService, Depends(get_credential_service)]


# --------------------------------------------------------------------------
# 内部辅助
# --------------------------------------------------------------------------


def authorized_datasource(
    db: Session, datasource_id: uuid.UUID, user: User
) -> tuple[Datasource, Membership]:
    """取数据源并校验租户边界；一次性返回成员关系，避免重复查询。

    公开为 ``authorized_datasource`` 供其他路由（如 M3 生成接口）复用。
    """
    datasource = db.get(Datasource, datasource_id)
    if datasource is None:
        raise ResourceNotFound("数据源不存在")
    membership = require_project_member(datasource.project_id, user, db)
    return datasource, membership


def _connection_spec(datasource: Datasource, credentials: CredentialService) -> ConnectionSpec:
    """组装连接信息 —— 凭据在此刻解析，用完即弃。"""
    secret = credentials.resolve(datasource.credential_ref) if datasource.credential_ref else {}
    return ConnectionSpec(type=datasource.type, config=datasource.config, secret=secret)


# --------------------------------------------------------------------------
# 登记与查询
# --------------------------------------------------------------------------


@router.post("", response_model=DatasourceOut, status_code=status.HTTP_201_CREATED)
def create_datasource(
    payload: DatasourceCreate,
    user: CurrentUser,
    db: DbSession,
    credentials: Credentials,
) -> Datasource:
    """登记数据源（API-01）。仅 MAKER 可登记。"""
    membership = require_project_member(payload.project_id, user, db)
    ensure_role(membership, UserRole.MAKER)

    credential_ref: str | None = None
    fingerprint: str | None = None
    if payload.credential:
        fingerprint = CredentialService.fingerprint(payload.credential)
        credential_ref = credentials.issue_ref(
            payload.credential, name=f"ds-{uuid.uuid4()}"
        )

    datasource = Datasource(
        project_id=payload.project_id,
        name=payload.name,
        type=payload.type,
        config=payload.config,
        credential_ref=credential_ref,
        credential_fingerprint=fingerprint,
    )
    db.add(datasource)
    db.commit()
    db.refresh(datasource)
    return datasource


@router.get("", response_model=list[DatasourceOut])
def list_datasources(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> list[Datasource]:
    require_project_member(project_id, user, db)
    stmt = (
        select(Datasource)
        .where(Datasource.project_id == project_id)
        .order_by(Datasource.created_at)
    )
    return list(db.scalars(stmt))


@router.get("/{datasource_id}", response_model=DatasourceOut)
def get_datasource(
    datasource_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> Datasource:
    datasource, _ = authorized_datasource(db, datasource_id, user)
    return datasource


# --------------------------------------------------------------------------
# 连通性测试（FR-04）
# --------------------------------------------------------------------------


@router.post("/{datasource_id}/test", response_model=ConnectionTestOut)
def test_datasource(
    datasource_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    credentials: Credentials,
) -> ConnectionTestOut:
    """安全连通性测试：只执行 ``SELECT 1``，不读取业务数据。"""
    datasource, _ = authorized_datasource(db, datasource_id, user)

    try:
        latency_ms = test_connection(_connection_spec(datasource, credentials))
    except AppError:
        datasource.status = DatasourceStatus.UNREACHABLE
        db.commit()
        raise

    datasource.status = DatasourceStatus.CONNECTED
    db.commit()
    return ConnectionTestOut(ok=True, latency_ms=latency_ms, message="连接成功")


# --------------------------------------------------------------------------
# 元数据探查（API-02 / FR-05、FR-06、FR-07）
# --------------------------------------------------------------------------


@router.post("/{datasource_id}/profile", response_model=MetadataProfileOut)
def profile(
    datasource_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    credentials: Credentials,
    tables_limit: Annotated[int, Query(ge=1, le=200)] = 20,
    sample_size: Annotated[int, Query(ge=1, le=50)] = 5,
) -> MetadataProfile:
    """只读元数据探查 + 敏感分级自动打标。

    ``tables_limit`` / ``sample_size`` 是探查预算 —— 默认值刻意保守，
    避免在生产库上做全量扫描（PRD 第 5 章非功能要求）。
    """
    datasource, membership = authorized_datasource(db, datasource_id, user)
    ensure_role(membership, UserRole.MAKER)

    engine = build_engine(_connection_spec(datasource, credentials))
    try:
        source = SqlAlchemyMetadataSource(engine, schema=datasource.config.get("schema"))
        result = profile_datasource(
            source, tables_limit=tables_limit, sample_size=sample_size
        )
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 探查期异常统一收敛为「数据源不可达」
        logger.warning("元数据探查失败 [datasource=%s]：%s", datasource_id, exc)
        raise DatasourceUnreachable(f"元数据探查失败（{exc.__class__.__name__}）") from exc
    finally:
        engine.dispose()

    profile_record = MetadataProfile(
        datasource_id=datasource.id,
        schema_json=result.to_schema_json(),
        sensitivity_tags=result.to_sensitivity_tags(),
        max_level=result.max_level.value,
        tables_scanned=result.table_count,
    )
    db.add(profile_record)
    datasource.status = DatasourceStatus.PROFILED
    db.commit()
    db.refresh(profile_record)
    return profile_record


@router.get("/{datasource_id}/profiles", response_model=list[MetadataProfileOut])
def list_profiles(
    datasource_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> list[MetadataProfile]:
    """历史探查留档（FR-07）—— 供审批环节与当前版本对照。"""
    datasource, _ = authorized_datasource(db, datasource_id, user)
    stmt = (
        select(MetadataProfile)
        .where(MetadataProfile.datasource_id == datasource.id)
        .order_by(MetadataProfile.sampled_at.desc())
    )
    return list(db.scalars(stmt))
