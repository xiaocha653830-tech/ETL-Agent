"""审计查询契约（API-11 ~ API-13，FR-39 / FR-40）。

SPEC §6 的 API-01~10 与 API-P* 里都没有审计条目——这是规格空白，接口按 PRD
第 7 章「审计台」页面的要素设计：按时间排序的事件链、哈希链可视化、签名检查点
状态、导出入口。三个端点**全部只读**，没有写方法。
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, computed_field


class AuditRecordOut(BaseModel):
    """账本里的一条证据。

    ``prev_hash`` / ``entry_hash`` / ``signature`` 都返回：前两者是链可视化与
    离线复算的输入，后者是签名检查点状态的来源。**审计接口不该把「能不能验」
    这件事留给调用方去猜**——它把验证所需的材料原样交出去。
    """

    model_config = ConfigDict(from_attributes=True)

    seq: int
    event_type: str
    actor: str
    occurred_at: datetime
    project_id: uuid.UUID | None
    run_id: uuid.UUID | None
    approval_id: uuid.UUID | None
    version_id: uuid.UUID | None
    preparation_id: uuid.UUID | None
    payload: dict[str, Any] | None
    prev_hash: str
    entry_hash: str
    signature: str | None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def anchored(self) -> bool:
        """本条是否为签名检查点。非锚点的 ``signature`` 是 NULL。"""
        return self.signature is not None


class AuditChainHeadOut(BaseModel):
    """链头与完整性状态。**与列表同一次响应返回**。

    分两次请求会出现「列表已经更新、链头还是旧的」的错位，而这两者在审计台上
    必须同源——「这些记录属于一条经过校验的链」本身就是一个整体判断。
    """

    seq: int
    entry_hash: str
    integrity_status: str
    checked_at: datetime | None


class AuditRecordPage(BaseModel):
    items: list[AuditRecordOut]
    # keyset 分页游标。账本是**持续在尾部追加**的流，offset 分页会在追加期间
    # 漏读或重读；游标则始终指向一个确定的位置。
    next_after_seq: int | None
    head: AuditChainHeadOut


class AuditFailureOut(BaseModel):
    seq: int
    code: str
    detail: str


class AuditVerificationOut(BaseModel):
    """一次校验的结论（API-12）。"""

    chain_id: str
    status: str
    trigger: str
    checked_at: datetime
    checked: int
    from_seq: int | None
    to_seq: int | None
    head_seq: int
    head_hash: str
    anchors_checked: int
    anchors_verified: int
    first_failure_seq: int | None
    failures: list[AuditFailureOut]
    unanchored_tail_seconds: float | None
    duration_ms: int
    # 公钥随结论一起给：调用方与离线复算方必须用同一把公钥，口径才可比
    public_key: dict[str, str]
    # 这条结论是不是「上次跑的」——审计台必须能区分「此刻没问题」与「三天前没问题」
    stale: bool
