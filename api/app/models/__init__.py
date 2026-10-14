"""ORM 模型包。

集中 re-export，保证任意入口导入本包时 ``Base.metadata`` 都是完整的——
Alembic autogenerate 与测试建表都依赖这一点。
"""

from app.models.capability import Capability
from app.models.datasource import (
    Datasource,
    DatasourceStatus,
    DatasourceType,
    MetadataProfile,
)
from app.models.identity import Membership, Project, User, UserRole
from app.models.pipeline import PipelineRun, PipelineRunStatus
from app.models.version import PipelineVersion, VersionStatus

__all__ = [
    "Capability",
    "Datasource",
    "DatasourceStatus",
    "DatasourceType",
    "Membership",
    "MetadataProfile",
    "PipelineRun",
    "PipelineRunStatus",
    "PipelineVersion",
    "Project",
    "User",
    "UserRole",
    "VersionStatus",
]
