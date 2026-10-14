"""策略决策点 PDP（SPEC §3 M7，FR-26）。

**职责边界**：只依据「主体 + 工具 + 环境 + 制品」四个通用维度给出 allow / deny
及其理由，**不承载业务语义**——不知道数据源、审批、版本是什么。

**不属于本模块**：双维审批、职责分离、审批事实复核（那是 M6 / T-11 的事）；
本模块也不执行任何副作用（副作用一律走 Tool Broker）。

策略以列表形式串行求值，命中首个 deny 即短路返回；因此**可扩展**：
新增策略只需实现 ``Policy`` 协议并 ``register_policy``，无需改动既有策略。
"""

import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.core.errors import ErrorCode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolicyRequest:
    """一次准入判定的输入 —— 只有四个通用维度。"""

    subject: str
    tool: str
    environment: str
    artifact: str


@dataclass(frozen=True)
class Decision:
    """判定结果。``allow=False`` 时 ``code`` 指明对外错误码。"""

    allow: bool
    reason: str
    code: ErrorCode | None = None

    @classmethod
    def permit(cls, reason: str = "策略通过") -> "Decision":
        return cls(allow=True, reason=reason)

    @classmethod
    def deny(cls, reason: str, code: ErrorCode) -> "Decision":
        return cls(allow=False, reason=reason, code=code)


class Policy(Protocol):
    """策略协议。``evaluate`` 返回 ``None`` 表示「本策略无意见」，交后续策略继续。"""

    name: str

    def evaluate(self, request: PolicyRequest) -> Decision | None: ...


# --------------------------------------------------------------------------
# 最小策略集
# --------------------------------------------------------------------------


@dataclass
class AuthorizedSubjectPolicy:
    """未授权主体拒绝。

    ``allowed_subjects`` 为 ``None`` 时只拒绝空主体（宽松默认）；
    传入具体集合即启用白名单。
    """

    allowed_subjects: frozenset[str] | None = None
    name = "authorized-subject"

    def evaluate(self, request: PolicyRequest) -> Decision | None:
        if not request.subject:
            return Decision.deny("主体为空", ErrorCode.POL_SUBJECT_NOT_AUTHORIZED)
        if self.allowed_subjects is not None and request.subject not in self.allowed_subjects:
            return Decision.deny(
                f"主体未在白名单内：{request.subject}",
                ErrorCode.POL_SUBJECT_NOT_AUTHORIZED,
            )
        return None


@dataclass
class HighRiskToolPolicy:
    """高风险工具需要额外条件。

    ``high_risk_tools`` 中的工具必须绑定非空制品，且（若配置了
    ``allowed_environments``）只能在允许的环境下执行。
    """

    high_risk_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_environments: frozenset[str] | None = None
    name = "high-risk-tool"

    def evaluate(self, request: PolicyRequest) -> Decision | None:
        if request.tool not in self.high_risk_tools:
            return None
        if (
            self.allowed_environments is not None
            and request.environment not in self.allowed_environments
        ):
            return Decision.deny(
                f"高风险工具不允许在环境 {request.environment!r} 执行",
                ErrorCode.POL_RISK_BLOCKED,
            )
        if not request.artifact:
            return Decision.deny("高风险工具必须绑定制品", ErrorCode.POL_RISK_BLOCKED)
        return None


# --------------------------------------------------------------------------
# 决策点
# --------------------------------------------------------------------------


class PolicyDecisionPoint:
    """策略决策点：按注册顺序求值，首个 deny 短路。"""

    def __init__(self, policies: list[Policy] | None = None) -> None:
        self._policies: list[Policy] = (
            list(policies)
            if policies is not None
            else [AuthorizedSubjectPolicy(), HighRiskToolPolicy()]
        )

    def register_policy(self, policy: Policy) -> None:
        """追加策略（可扩展点：T-12 可注册更细的准入策略）。"""
        self._policies.append(policy)

    @property
    def policies(self) -> list[Policy]:
        return list(self._policies)

    def decide(
        self, subject: str, tool: str, environment: str, artifact: str
    ) -> Decision:
        request = PolicyRequest(
            subject=subject, tool=tool, environment=environment, artifact=artifact
        )
        for policy in self._policies:
            outcome = policy.evaluate(request)
            if outcome is not None and not outcome.allow:
                logger.info("PDP 拒绝：策略=%s 理由=%s", policy.name, outcome.reason)
                return outcome
        return Decision.permit()


_default_pdp = PolicyDecisionPoint()


def get_pdp() -> PolicyDecisionPoint:
    """进程级默认决策点（可被 T-12 在装配阶段追加策略）。"""
    return _default_pdp
