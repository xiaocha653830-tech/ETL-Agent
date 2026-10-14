"""单次执行能力令牌模型（FR-26 / FR-27，SPEC §5 ``CAPABILITY``）。

**职责边界**：本表是安全内核 M7 的持久化事实，只承载「主体 + 工具 + 环境 + 制品」
四个通用绑定维度与消费状态，**不含任何业务语义**（不知道数据源、审批、版本是什么）。

与 SPEC §5 最小定义的差异（均为实现必需，已向上兼容扩展）：
  - 增列 ``tool`` / ``environment`` / ``kid`` / ``issued_at`` / ``consumed_at``。
    SPEC 原始实体只显式列出 ``bound_subject`` 与 ``bound_artifact``，但 FR-26 要求
    令牌绑定「工具 + 主体 + 环境 + 制品」四维，故四列齐备；``kid`` 为 Q3 预留的
    密钥轮转字段；``consumed_at`` 供审计留痕。
  - ``preparation_id`` 指向 M6 的准备单，M6（T-11）尚未落地，故可空。
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Uuid, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# SPEC §5 / FR-26：能力令牌有效期上限 5 分钟（签发时写 expires_at = now + 5min）
CAPABILITY_TTL_SECONDS = 300


class Capability(Base):
    """一次签发的能力令牌。

    ``consumed`` 是防重放 Guard 的唯一真相：消费通过
    ``UPDATE ... WHERE consumed = false`` 的条件更新原子完成（FR-27）。
    """

    __tablename__ = "capabilities"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 签发来源（M6 准备单）；M6 尚未落地，允许为空
    preparation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )

    # ---- 四维绑定（任一不匹配即拒绝，FR-26）----
    tool: Mapped[str] = mapped_column(String(128), nullable=False)
    bound_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    environment: Mapped[str] = mapped_column(String(128), nullable=False)
    bound_artifact: Mapped[str] = mapped_column(String(255), nullable=False)

    # 签名密钥标识（Q3：预留 kid，15 天内单密钥不轮转）
    kid: Mapped[str] = mapped_column(String(64), nullable=False)

    # ---- 单次消费（FR-27）----
    consumed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
