"""统一错误码体系与全局异常处理。

对应 SPEC §6「错误码体系（段位划分）」：

| 段位 | 含义 | 示例 |
|------|------|------|
| `AUTH_` | 认证与租户 | `AUTH_TOKEN_INVALID`、`AUTH_CROSS_TENANT` |
| `POL_` | 策略与审批 | `POL_SELF_APPROVAL_DENIED` |
| `BIZ_` | 业务规则 | `BIZ_FINGERPRINT_MISMATCH` |
| `SYS_` | 系统与依赖 | `SYS_DATAPLANE_UNAVAILABLE` |

所有对外错误响应统一为同一结构（无例外）：

    {"code": "AUTH_TOKEN_INVALID", "message": "令牌无效或已过期", "detail": null}

**前端只按 `code` 分支处理，不得依赖 `message` 文案**（PRD 第 7 章全局约束：
服务端必须独立校验，前端不做任何业务校验）。
"""

import logging
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ErrorCode(StrEnum):
    """全平台错误码。

    新增时必须遵循段位前缀；已发布的编码**不得复用或改义**，
    否则前端的 `code` 分支会静默错配。
    """

    # ---- AUTH_：认证与租户 ----
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
    AUTH_ACCOUNT_DISABLED = "AUTH_ACCOUNT_DISABLED"
    AUTH_EMAIL_TAKEN = "AUTH_EMAIL_TAKEN"
    AUTH_CROSS_TENANT = "AUTH_CROSS_TENANT"
    AUTH_PERMISSION_DENIED = "AUTH_PERMISSION_DENIED"
    # 单次能力令牌（Capability，FR-26 / FR-27）—— 校验失败原因必须可区分，
    # 前端据此给出不同提示（重试 / 重新签发 / 拦截重放）。
    AUTH_CAPABILITY_MALFORMED = "AUTH_CAPABILITY_MALFORMED"
    AUTH_CAPABILITY_SIGNATURE_INVALID = "AUTH_CAPABILITY_SIGNATURE_INVALID"
    AUTH_CAPABILITY_EXPIRED = "AUTH_CAPABILITY_EXPIRED"
    AUTH_CAPABILITY_BINDING_MISMATCH = "AUTH_CAPABILITY_BINDING_MISMATCH"
    AUTH_CAPABILITY_CONSUMED = "AUTH_CAPABILITY_CONSUMED"

    # ---- POL_：策略与审批（T-10 ~ T-12 逐步填充，此处先占位）----
    POL_SELF_APPROVAL_DENIED = "POL_SELF_APPROVAL_DENIED"
    # 双维审批未齐（或已超时失效）—— Commit 复核失败的主因（FR-25）
    POL_APPROVAL_INCOMPLETE = "POL_APPROVAL_INCOMPLETE"
    # PDP（策略决策点）通用拒绝码 —— 不承载业务语义，只表达「准入判定未通过」
    POL_SUBJECT_NOT_AUTHORIZED = "POL_SUBJECT_NOT_AUTHORIZED"
    POL_RISK_BLOCKED = "POL_RISK_BLOCKED"

    # ---- BIZ_：业务规则 ----
    BIZ_VALIDATION_FAILED = "BIZ_VALIDATION_FAILED"
    BIZ_RESOURCE_NOT_FOUND = "BIZ_RESOURCE_NOT_FOUND"
    BIZ_RESOURCE_CONFLICT = "BIZ_RESOURCE_CONFLICT"
    # Commit 时准备单指纹与版本实际指纹不一致（制品被换过，FR-25）
    BIZ_FINGERPRINT_MISMATCH = "BIZ_FINGERPRINT_MISMATCH"
    # Tool Broker：请求调用未在 Broker 注册的工具
    BIZ_TOOL_NOT_REGISTERED = "BIZ_TOOL_NOT_REGISTERED"
    # 令牌签名有效，但库中不存在对应签发记录（非本系统签发或记录已清理）
    BIZ_CAPABILITY_NOT_FOUND = "BIZ_CAPABILITY_NOT_FOUND"

    # ---- SYS_：系统与依赖 ----
    SYS_INTERNAL_ERROR = "SYS_INTERNAL_ERROR"
    SYS_SECRET_BACKEND_UNAVAILABLE = "SYS_SECRET_BACKEND_UNAVAILABLE"
    SYS_SECRET_NOT_FOUND = "SYS_SECRET_NOT_FOUND"
    SYS_DATASOURCE_UNREACHABLE = "SYS_DATASOURCE_UNREACHABLE"
    SYS_DATASOURCE_DRIVER_UNAVAILABLE = "SYS_DATASOURCE_DRIVER_UNAVAILABLE"
    # 签名密钥加载 / 解析失败（Q3：私钥存凭据后端，启动时加载到内存）
    SYS_SIGNING_KEY_UNAVAILABLE = "SYS_SIGNING_KEY_UNAVAILABLE"
    SYS_MODEL_UNAVAILABLE = "SYS_MODEL_UNAVAILABLE"
    SYS_MODEL_TIMEOUT = "SYS_MODEL_TIMEOUT"
    SYS_MODEL_OUTPUT_INVALID = "SYS_MODEL_OUTPUT_INVALID"
    # 数据面引擎不可达或未接通（M9 适配器）
    SYS_DATAPLANE_UNAVAILABLE = "SYS_DATAPLANE_UNAVAILABLE"
    # 运行已失去租约（旧 Worker 试图写回终态，或租约被回收）
    SYS_LEASE_LOST = "SYS_LEASE_LOST"


class AppError(Exception):
    """业务异常基类。

    子类只需覆盖 ``code`` / ``status_code`` / ``message`` 三个类属性，
    全局处理器会将其渲染为统一错误响应体。
    """

    code: ErrorCode = ErrorCode.SYS_INTERNAL_ERROR
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    message: str = "服务内部错误"

    def __init__(self, message: str | None = None, *, detail: Any = None) -> None:
        if message is not None:
            self.message = message
        self.detail = detail
        super().__init__(self.message)


# --------------------------------------------------------------------------
# AUTH_：认证与租户
# --------------------------------------------------------------------------


class AuthInvalidCredentials(AppError):
    code = ErrorCode.AUTH_INVALID_CREDENTIALS
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "邮箱或密码错误"


class AuthTokenInvalid(AppError):
    code = ErrorCode.AUTH_TOKEN_INVALID
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "令牌无效或已过期"


class AuthAccountDisabled(AppError):
    code = ErrorCode.AUTH_ACCOUNT_DISABLED
    status_code = status.HTTP_403_FORBIDDEN
    message = "账号已停用"


class AuthEmailTaken(AppError):
    code = ErrorCode.AUTH_EMAIL_TAKEN
    status_code = status.HTTP_409_CONFLICT
    message = "邮箱已注册"


class CrossTenantDenied(AppError):
    """跨租户访问被拦截（FR-44）。"""

    code = ErrorCode.AUTH_CROSS_TENANT
    status_code = status.HTTP_403_FORBIDDEN
    message = "无权访问该项目"


class PermissionDenied(AppError):
    """项目内角色权限不足。"""

    code = ErrorCode.AUTH_PERMISSION_DENIED
    status_code = status.HTTP_403_FORBIDDEN
    message = "当前角色无权执行该操作"


# --------------------------------------------------------------------------
# BIZ_：业务规则
# --------------------------------------------------------------------------


class ResourceNotFound(AppError):
    code = ErrorCode.BIZ_RESOURCE_NOT_FOUND
    status_code = status.HTTP_404_NOT_FOUND
    message = "资源不存在"


class ResourceConflict(AppError):
    code = ErrorCode.BIZ_RESOURCE_CONFLICT
    status_code = status.HTTP_409_CONFLICT
    message = "资源冲突"


class FingerprintMismatch(AppError):
    """Commit 时制品指纹与准备单声明的指纹不一致（FR-25）。

    这是「审的是一份方案、跑的是另一份配置」的守卫：只要有一步没对齐，
    双维审批所背书的对象就不再是即将执行的对象，必须拒绝而不是放行。
    """

    code = ErrorCode.BIZ_FINGERPRINT_MISMATCH
    status_code = status.HTTP_409_CONFLICT
    message = "制品指纹与准备单不一致，拒绝执行"


# --------------------------------------------------------------------------
# SYS_：系统与依赖
# --------------------------------------------------------------------------


class SecretBackendUnavailable(AppError):
    """凭据后端（Vault / 本地密钥库）不可达或初始化失败。"""

    code = ErrorCode.SYS_SECRET_BACKEND_UNAVAILABLE
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "凭据后端不可用"


class SecretNotFound(AppError):
    """凭据引用不存在或已失效。"""

    code = ErrorCode.SYS_SECRET_NOT_FOUND
    status_code = status.HTTP_404_NOT_FOUND
    message = "凭据引用不存在"


class DatasourceUnreachable(AppError):
    """数据源连通性测试失败或元数据探查失败。"""

    code = ErrorCode.SYS_DATASOURCE_UNREACHABLE
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "数据源不可达"


class DatasourceDriverUnavailable(AppError):
    """登记了本环境未安装驱动（DBAPI）的数据源类型。"""

    code = ErrorCode.SYS_DATASOURCE_DRIVER_UNAVAILABLE
    status_code = status.HTTP_501_NOT_IMPLEMENTED
    message = "缺少该类型数据源的驱动"


class DataplaneUnavailable(AppError):
    """数据面引擎不可达或适配器尚未接通（M9）。"""

    code = ErrorCode.SYS_DATAPLANE_UNAVAILABLE
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "数据面引擎不可达"


class LeaseLost(AppError):
    """本次运行已不再持有租约（FR-29 租约 fencing）。

    旧 Worker 在租约过期或被回收后醒来时收到此错——它不能再写回任何终态，
    否则一次「其实已经被别人接管」的执行会把自己的结论覆盖上去。
    """

    code = ErrorCode.SYS_LEASE_LOST
    status_code = status.HTTP_409_CONFLICT
    message = "运行租约已失效"


# --------------------------------------------------------------------------
# SYS_：大模型（M3）
# --------------------------------------------------------------------------


class ModelUnavailable(AppError):
    """模型后端未配置或调用失败。"""

    code = ErrorCode.SYS_MODEL_UNAVAILABLE
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "大模型服务不可用"


class ModelTimeout(AppError):
    code = ErrorCode.SYS_MODEL_TIMEOUT
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    message = "大模型调用超时"


class ModelOutputInvalid(AppError):
    """模型返回的内容无法解析为预期的结构化方案。"""

    code = ErrorCode.SYS_MODEL_OUTPUT_INVALID
    status_code = status.HTTP_502_BAD_GATEWAY
    message = "模型输出无法解析为结构化方案"


# --------------------------------------------------------------------------
# AUTH_ / POL_ / BIZ_ / SYS_：安全内核 M7（Capability / PDP / Tool Broker）
# --------------------------------------------------------------------------


class CapabilityMalformed(AppError):
    """能力令牌格式非法（分段数错误、基无法解码等）。"""

    code = ErrorCode.AUTH_CAPABILITY_MALFORMED
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "能力令牌格式非法"


class CapabilitySignatureInvalid(AppError):
    """令牌签名校验失败（内容被篡改或非本系统签发）。"""

    code = ErrorCode.AUTH_CAPABILITY_SIGNATURE_INVALID
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "能力令牌签名无效"


class CapabilityExpired(AppError):
    """令牌已超过有效窗口（FR-26：有效期 ≤ 5 分钟）。"""

    code = ErrorCode.AUTH_CAPABILITY_EXPIRED
    status_code = status.HTTP_401_UNAUTHORIZED
    message = "能力令牌已过期"


class CapabilityBindingMismatch(AppError):
    """四维绑定（工具 / 主体 / 环境 / 制品）中任一维度不匹配（FR-26）。"""

    code = ErrorCode.AUTH_CAPABILITY_BINDING_MISMATCH
    status_code = status.HTTP_403_FORBIDDEN
    message = "能力令牌绑定不匹配"


class CapabilityConsumed(AppError):
    """令牌已被消费 —— 防重放 Guard 拒绝重复使用（FR-27）。"""

    code = ErrorCode.AUTH_CAPABILITY_CONSUMED
    status_code = status.HTTP_409_CONFLICT
    message = "能力令牌已被消费（重放被拒）"


class CapabilityNotFound(AppError):
    """令牌签名有效但库中无对应签发记录。"""

    code = ErrorCode.BIZ_CAPABILITY_NOT_FOUND
    status_code = status.HTTP_404_NOT_FOUND
    message = "能力令牌签发记录不存在"


class SubjectNotAuthorized(AppError):
    """PDP：主体未获授权。"""

    code = ErrorCode.POL_SUBJECT_NOT_AUTHORIZED
    status_code = status.HTTP_403_FORBIDDEN
    message = "主体未获授权"


class RiskBlocked(AppError):
    """PDP：高风险工具未满足额外准入条件。"""

    code = ErrorCode.POL_RISK_BLOCKED
    status_code = status.HTTP_403_FORBIDDEN
    message = "高风险操作被策略拦截"


# --------------------------------------------------------------------------
# POL_：审批（M6，FR-23 ~ FR-25）
# --------------------------------------------------------------------------


class SelfApprovalDenied(AppError):
    """职责分离拦截（FR-24）：拦截自批与职责混用。"""

    code = ErrorCode.POL_SELF_APPROVAL_DENIED
    status_code = status.HTTP_403_FORBIDDEN
    message = "职责分离：不可审批自己提交的版本"


class ApprovalIncomplete(AppError):
    """双维审批未齐或已失效（FR-23 / FR-25）。"""

    code = ErrorCode.POL_APPROVAL_INCOMPLETE
    status_code = status.HTTP_403_FORBIDDEN
    message = "审批未完成，不允许执行"


class ToolNotRegistered(AppError):
    """Tool Broker：请求调用的工具未注册。"""

    code = ErrorCode.BIZ_TOOL_NOT_REGISTERED
    status_code = status.HTTP_400_BAD_REQUEST
    message = "工具未注册"


class SigningKeyUnavailable(AppError):
    """签名密钥加载 / 解析失败（Q3：私钥存凭据后端，启动时加载到内存）。"""

    code = ErrorCode.SYS_SIGNING_KEY_UNAVAILABLE
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = "签名密钥不可用"


# --------------------------------------------------------------------------
# 响应渲染与处理器注册
# --------------------------------------------------------------------------

# 框架自身抛出的 HTTPException（路由不存在、方法不允许等）到错误码的映射，
# 使框架错误与业务错误对外结构完全一致。
_HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    status.HTTP_401_UNAUTHORIZED: ErrorCode.AUTH_TOKEN_INVALID,
    status.HTTP_403_FORBIDDEN: ErrorCode.AUTH_PERMISSION_DENIED,
    status.HTTP_404_NOT_FOUND: ErrorCode.BIZ_RESOURCE_NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.BIZ_VALIDATION_FAILED,
    status.HTTP_409_CONFLICT: ErrorCode.BIZ_RESOURCE_CONFLICT,
    status.HTTP_422_UNPROCESSABLE_CONTENT: ErrorCode.BIZ_VALIDATION_FAILED,
}


def error_response(
    code: ErrorCode | str,
    message: str,
    *,
    status_code: int,
    detail: Any = None,
) -> JSONResponse:
    """渲染统一错误响应体。"""
    body: dict[str, Any] = {
        "code": code.value if isinstance(code, ErrorCode) else code,
        "message": message,
        "detail": detail,
    }
    return JSONResponse(status_code=status_code, content=jsonable_encoder(body))


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器，保证任何出口都是统一错误体。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        # 4xx 是预期内的业务分支（如审批未完成），5xx 才需要告警
        if exc.status_code >= 500:
            logger.error("业务异常 [%s] %s", exc.code, exc.message)
        return error_response(
            exc.code, exc.message, status_code=exc.status_code, detail=exc.detail
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            ErrorCode.BIZ_VALIDATION_FAILED,
            "请求参数校验失败",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.errors(),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.SYS_INTERNAL_ERROR)
        return error_response(code, str(exc.detail), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 不向调用方泄露堆栈；完整信息只进服务端日志
        logger.exception("未捕获异常", exc_info=exc)
        return error_response(
            ErrorCode.SYS_INTERNAL_ERROR,
            "服务内部错误",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
