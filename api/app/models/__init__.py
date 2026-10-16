"""ORM 模型包。

集中 re-export，保证任意入口导入本包时 ``Base.metadata`` 都是完整的——
Alembic autogenerate 与测试建表都依赖这一点。
"""

from app.models.approval import (
    Approval,
    ApprovalDecision,
    ApprovalDimension,
    Preparation,
    PreparationStatus,
    RiskLevel,
)
from app.models.audit import (
    AuditChainHead,
    AuditRecord,
    AuditVerification,
    IntegrityStatus,
)
from app.models.capability import Capability
from app.models.datasource import (
    Datasource,
    DatasourceStatus,
    DatasourceType,
    MetadataProfile,
)
from app.models.execution import (
    ExecutionRun,
    ExecutionRunStatus,
    OutboxEvent,
    OutboxStatus,
    RunEvent,
    RunOperation,
    RunOperationStatus,
)
from app.models.identity import Membership, Project, User, UserRole
from app.models.pipeline import PipelineRun, PipelineRunStatus
from app.models.quality import QualityReport
from app.models.version import PipelineVersion, VersionStatus

__all__ = [
    "Approval",
    "ApprovalDecision",
    "ApprovalDimension",
    "AuditChainHead",
    "AuditRecord",
    "AuditVerification",
    "Capability",
    "Datasource",
    "DatasourceStatus",
    "DatasourceType",
    "ExecutionRun",
    "ExecutionRunStatus",
    "IntegrityStatus",
    "Membership",
    "MetadataProfile",
    "OutboxEvent",
    "OutboxStatus",
    "PipelineRun",
    "PipelineRunStatus",
    "PipelineVersion",
    "Preparation",
    "PreparationStatus",
    "Project",
    "QualityReport",
    "RiskLevel",
    "RunEvent",
    "RunOperation",
    "RunOperationStatus",
    "User",
    "UserRole",
    "VersionStatus",
]
