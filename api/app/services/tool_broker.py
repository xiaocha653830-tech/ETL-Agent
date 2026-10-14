"""Tool Broker —— 所有副作用的唯一出口（SPEC §3 M7，FR-26 / FR-27）。

PRD 第 10 章架构约束：「所有副作用执行必须经唯一出口、存在旁路执行路径 = 不可接受」。
本模块即该唯一出口：任何有副作用的操作都必须 ``execute(tool_name, arguments, token)``，
执行前依次完成

  1. 令牌有效性（验签 / 未过期）
  2. 工具已注册（未注册直接拒绝，且**不消费令牌**）
  3. 四维绑定匹配（请求的工具与令牌绑定一致）
  4. **原子消费**（防重放 Guard：同一令牌只能放行一次）

副作用调用会写入一条可核验的调用记录（谁、何时、用什么令牌、调了什么），
通过 ``InvocationSink`` 预留 T-20 证据账本的接口。

**职责边界**：Broker 不承载业务语义，只做「准入 + 放行 + 留痕」。
"""

import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.core.errors import CapabilityBindingMismatch, ToolNotRegistered
from app.core.keys import SigningKey
from app.schemas.capability import BindingContext, ToolInvocationOut
from app.services.capability import decode_claims, ensure_not_expired, verify_capability

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Any]


# --------------------------------------------------------------------------
# 调用记录（T-20 证据账本预留接口）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolInvocation:
    """一次工具调用的留痕。

    **只记录参数指纹而非原始参数**——避免把可能的敏感值写进证据账本。
    """

    capability_id: uuid.UUID
    tool: str
    subject: str
    environment: str
    artifact: str
    arguments_digest: str
    side_effect: bool
    succeeded: bool
    invoked_at: datetime

    def as_out(self) -> ToolInvocationOut:
        return ToolInvocationOut(
            capability_id=self.capability_id,
            tool=self.tool,
            subject=self.subject,
            environment=self.environment,
            artifact=self.artifact,
            arguments_digest=self.arguments_digest,
            side_effect=self.side_effect,
            succeeded=self.succeeded,
            invoked_at=self.invoked_at,
        )


class InvocationSink(Protocol):
    """调用记录落点协议 —— T-20 可实现为写库 + 哈希链。"""

    def record(self, invocation: ToolInvocation) -> None: ...


class InMemoryInvocationSink:
    """默认落点：进程内保存（测试与开发用）。"""

    def __init__(self) -> None:
        self._records: list[ToolInvocation] = []
        self._lock = threading.Lock()

    def record(self, invocation: ToolInvocation) -> None:
        with self._lock:
            self._records.append(invocation)

    @property
    def records(self) -> list[ToolInvocation]:
        with self._lock:
            return list(self._records)


def _arguments_digest(arguments: Any) -> str:
    """参数的规范指纹（不可逆），用于留痕而不泄露内容。"""
    try:
        material = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        material = repr(arguments)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 注册表
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: ToolHandler
    side_effect: bool


class ToolBroker:
    """副作用唯一出口。"""

    def __init__(
        self,
        *,
        sink: InvocationSink | None = None,
        signing_key: SigningKey | None = None,
    ) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._lock = threading.Lock()
        self._sink: InvocationSink = sink or InMemoryInvocationSink()
        self._signing_key = signing_key

    # -- 注册 --

    def register_tool(
        self,
        name: str,
        handler: ToolHandler,
        *,
        side_effect: bool = True,
        replace: bool = False,
    ) -> None:
        """注册工具（T-13 / T-15 后续在此登记真实工具）。

        ``side_effect=False`` 的工具视为只读，不写调用记录。
        """
        if not name:
            raise ValueError("工具名不能为空")
        with self._lock:
            if name in self._tools and not replace:
                raise ValueError(f"工具已注册：{name}")
            self._tools[name] = ToolSpec(name=name, handler=handler, side_effect=side_effect)

    def get_tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools)

    @property
    def sink(self) -> InvocationSink:
        return self._sink

    # -- 执行 --

    def execute(
        self,
        tool_name: str,
        arguments: Any,
        capability_token: str,
        *,
        db: Session,
        expected_binding: BindingContext | None = None,
        signing_key: SigningKey | None = None,
    ) -> Any:
        """执行一个工具调用。

        参数按位置保持精简契约 ``execute(tool_name, arguments, capability_token)``；
        ``db`` 为消费令牌所必需（关键字传入）。
        """
        key = signing_key or self._signing_key

        # 1) 令牌有效性：验签 + 未过期（先于任何副作用与消费）
        claims = decode_claims(capability_token, signing_key=key)
        ensure_not_expired(claims)

        # 2) 工具已注册 —— 未注册直接拒绝，不消费令牌
        spec = self._tools.get(tool_name)
        if spec is None:
            raise ToolNotRegistered(f"工具未注册：{tool_name}")

        # 3) 绑定匹配：请求的工具必须与令牌绑定一致
        if claims.tool != tool_name:
            raise CapabilityBindingMismatch(detail={"dimension": ["tool"]})

        binding = expected_binding or BindingContext(
            tool=tool_name,
            subject=claims.subject,
            environment=claims.environment,
            artifact=claims.artifact,
        )

        # 4) 原子消费（防重放）—— 到此为止令牌只能被放行一次
        capability = verify_capability(
            db, capability_token, binding, consume=True, signing_key=key
        )

        # 5) 放行并留痕
        succeeded = False
        try:
            if isinstance(arguments, dict):
                result = spec.handler(**arguments)
            else:
                result = spec.handler(arguments)
            succeeded = True
            return result
        finally:
            if spec.side_effect:
                self._sink.record(
                    ToolInvocation(
                        capability_id=capability.id,
                        tool=tool_name,
                        subject=claims.subject,
                        environment=claims.environment,
                        artifact=claims.artifact,
                        arguments_digest=_arguments_digest(arguments),
                        side_effect=True,
                        succeeded=succeeded,
                        invoked_at=datetime.now(UTC),
                    )
                )


_default_broker: ToolBroker | None = None
_default_broker_lock = threading.Lock()


def get_tool_broker() -> ToolBroker:
    """进程级默认 Broker（T-13 / T-15 在此注册真实工具）。"""
    global _default_broker
    if _default_broker is None:
        with _default_broker_lock:
            if _default_broker is None:
                _default_broker = ToolBroker()
    return _default_broker
