"""质量报告（M10，FR-33 / FR-40）。

对应 SPEC §5 的 ``QUALITY_REPORT`` 实体。在此之前，质量校验的结论只活在
``RunEvent`` 的事件快照里——「这次运行分流了多少行」这个问题的答案被埋在一条
进度事件的 JSON 载荷中，查询要按事件类型过滤再解析快照。

一条报告是一次运行的**质量结论**，它有独立的生命周期（被审计、被报告接口查询、
与历史运行比对），因此它是一张表，而不是某条事件的一个字段。这与
``ExecutionRun.published_at`` 的取舍是同一条原则：**关键事实应当可以被查询**。
"""

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.types import JsonColumn

if TYPE_CHECKING:
    from app.models.execution import ExecutionRun


class QualityReport(Base, TimestampMixin):
    """一次运行的质量校验结论。"""

    __tablename__ = "quality_reports"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 报告是运行的产出物：运行没了它也就没有意义（与 ``RunEvent`` 同样是 CASCADE）。
    # UNIQUE 把「一次运行至多一份报告」钉在库层——发布路径已被幂等台账约束为至多
    # 一次，这里是把它再钉一遍。
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("execution_runs.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # 源端读到的总行数。行数守恒（``source == valid + rejected``）是发布前的硬判据，
    # 报告只在守恒时才会产出，所以这三个数字是一组可相互复核的事实。
    source_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    valid_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rejected_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # 错误码到行数的分布，如 ``{"E001": 1}``，回答「被拒的行都是什么原因」——
    # 只知道总数的话，看到「拒绝了 300 行」也无从下手。取值来自错误表的
    # ``validation_errors`` 列，而那些值正是规则声明里的 ``error_code``。
    error_dist: Mapped[dict] = mapped_column(JsonColumn, nullable=False)

    # 错误明细所在的表名；没有质量规则时为 ``None``（此时不存在错误表）。
    # 报告只带指针而不复制数据行：原始行留在错误表里，那才是它们该待的地方
    # （与 ``BudgetVerdict.as_snapshot`` 的取舍一致）。
    error_table: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped["ExecutionRun"] = relationship(back_populates="quality_report")

    @property
    def rejected_ratio(self) -> float:
        """拒绝率。**派生而不落列**：它必须与 ``rejected_rows / source_rows``
        是同一个数字，存两份就会有对不上的那一天。"""
        return self.rejected_rows / self.source_rows if self.source_rows else 0.0
