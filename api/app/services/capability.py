"""能力令牌的签发 / 校验 / 单次消费（FR-26 / FR-27，SPEC §3 M7）。

令牌形式为 JWT 风格的紧凑编码：``header.payload.signature``，签名算法 Ed25519
（ADR-03）。**签名覆盖全部绑定字段与过期时间**——``payload`` 的规范 JSON 与
``header`` 一同进入签名输入，任何字段被篡改都会验签失败。

校验失败严格区分原因（不同错误码）：
  - 格式非法          → ``AUTH_CAPABILITY_MALFORMED``
  - 签名无效 / 被篡改 → ``AUTH_CAPABILITY_SIGNATURE_INVALID``
  - 已过期            → ``AUTH_CAPABILITY_EXPIRED``
  - 四维绑定不匹配     → ``AUTH_CAPABILITY_BINDING_MISMATCH``
  - 已被消费（重放）   → ``AUTH_CAPABILITY_CONSUMED``
  - 无签发记录        → ``BIZ_CAPABILITY_NOT_FOUND``

**单次消费是原子的**：用 ``UPDATE ... WHERE consumed = false`` 的条件更新并检查
影响行数，两个并发消费请求只有一个 ``rowcount == 1``，另一个收到「已被消费」。
"""

import base64
import json
import logging
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.core.errors import (
    CapabilityBindingMismatch,
    CapabilityConsumed,
    CapabilityExpired,
    CapabilityMalformed,
    CapabilityNotFound,
    CapabilitySignatureInvalid,
)
from app.core.keys import SigningKey, get_signing_key
from app.db.types import as_utc
from app.models.capability import CAPABILITY_TTL_SECONDS, Capability
from app.schemas.capability import (
    BindingContext,
    CapabilityClaims,
    CapabilityTokenOut,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 紧凑编码原语
# --------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _resolve_key(signing_key: SigningKey | None) -> SigningKey:
    return signing_key or get_signing_key()




# --------------------------------------------------------------------------
# 签发
# --------------------------------------------------------------------------


def sign_claims(claims: CapabilityClaims, *, signing_key: SigningKey | None = None) -> str:
    """把载荷序列化为令牌串（私有原语，测试与内部签发共用）。"""
    key = _resolve_key(signing_key)
    header = {"alg": key.algorithm, "typ": "CAP", "kid": key.kid}
    # 键排序和紧凑分隔符生成唯一规范表示，避免等价载荷产生不同签名输入。
    header_b64 = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps(
            claims.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = key.sign(signing_input)
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def _decode(token: str, key: SigningKey) -> tuple[CapabilityClaims, str]:
    """验签并解析令牌，返回 (载荷, kid)。``kid`` 来自 header。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise CapabilityMalformed("令牌分段数不为 3")
    header_b64, payload_b64, signature_b64 = parts

    # 先验签：签名输入即 header.payload，覆盖全部字段
    try:
        signature = _b64url_decode(signature_b64)
    except Exception as exc:  # noqa: BLE001 —— 签名段无法解码即视为格式非法
        raise CapabilityMalformed("签名段无法解码") from exc
    try:
        key.verify(f"{header_b64}.{payload_b64}".encode("ascii"), signature)
    except Exception as exc:  # noqa: BLE001 —— 验签失败统一归为签名无效
        raise CapabilitySignatureInvalid() from exc

    # 验签通过后再解析载荷；解析失败说明我们用未定义格式签发过，属格式非法
    try:
        payload = json.loads(_b64url_decode(payload_b64))
        claims = CapabilityClaims.model_validate(payload)
        header = json.loads(_b64url_decode(header_b64))
    except Exception as exc:  # noqa: BLE001
        raise CapabilityMalformed("令牌载荷无法解析") from exc

    kid = header.get("kid") if isinstance(header, dict) else None
    if not isinstance(kid, str) or not kid:
        raise CapabilityMalformed("令牌缺少 kid")
    return claims, kid


def decode_claims(token: str, *, signing_key: SigningKey | None = None) -> CapabilityClaims:
    """仅做验签 + 解析（不校验过期与绑定）。"""
    claims, _ = _decode(token, _resolve_key(signing_key))
    return claims


# --------------------------------------------------------------------------
# 校验：过期 + 四维绑定
# --------------------------------------------------------------------------


def ensure_not_expired(claims: CapabilityClaims, *, now: int | None = None) -> None:
    current = int(time.time()) if now is None else now
    if current > claims.exp:
        raise CapabilityExpired()


def match_binding(claims: CapabilityClaims, expected: BindingContext) -> None:
    """逐维比对；首个不匹配的维度写进 ``detail`` 便于排查。"""
    actual = BindingContext.from_claims(claims)
    if actual != expected:
        mismatched = [
            name
            for name in ("tool", "subject", "environment", "artifact")
            if getattr(actual, name) != getattr(expected, name)
        ]
        raise CapabilityBindingMismatch(
            detail={"dimension": mismatched, "expected": expected.model_dump()}
        )


def verify_claims(
    token: str,
    expected: BindingContext,
    *,
    signing_key: SigningKey | None = None,
) -> CapabilityClaims:
    """离线校验（数据面仅凭公钥即可）：验签 → 未过期 → 四维绑定匹配。"""
    claims = decode_claims(token, signing_key=signing_key)
    ensure_not_expired(claims)
    match_binding(claims, expected)
    return claims


def verify_capability(
    db: Session,
    token: str,
    expected: BindingContext,
    *,
    consume: bool = False,
    signing_key: SigningKey | None = None,
) -> Capability:
    """控制面校验：在离线校验基础上，核对签发记录并（可选）原子消费。"""
    key = _resolve_key(signing_key)
    claims, kid = _decode(token, key)
    ensure_not_expired(claims)
    match_binding(claims, expected)

    capability = db.get(Capability, claims.jti)
    if capability is None:
        raise CapabilityNotFound()
    # 令牌验签只能证明“由私钥签过”；数据库记录还要证明它确由本控制面正式签发。
    if capability.kid != kid:
        # 库中记录与令牌声明的签名密钥不一致：视为被伪造/篡改
        raise CapabilitySignatureInvalid(detail={"reason": "kid 不一致"})
    if (
        capability.tool,
        capability.bound_subject,
        capability.environment,
        capability.bound_artifact,
    ) != (claims.tool, claims.subject, claims.environment, claims.artifact):
        raise CapabilityBindingMismatch(detail={"dimension": ["db"]})
    if as_utc(capability.expires_at) < datetime.now(UTC):
        raise CapabilityExpired()
    if capability.consumed:
        raise CapabilityConsumed()

    if consume:
        return consume_capability(db, capability.id)
    return capability


# --------------------------------------------------------------------------
# 单次消费（FR-27：原子条件更新）
# --------------------------------------------------------------------------


def consume_capability(db: Session, capability_id: uuid.UUID) -> Capability:
    """原子消费：``UPDATE ... WHERE consumed = false`` 且影响行数必须为 1。

    并发下只有一个请求能令 ``rowcount == 1``；其余拿到 0 并收到「已被消费」。
    """
    result = db.execute(
        update(Capability)
        .where(Capability.id == capability_id, Capability.consumed.is_(False))
        .values(consumed=True, consumed_at=func.now())
    )
    if result.rowcount != 1:
        # 条件更新未命中既可能是不存在，也可能是并发请求已抢先消费；对外统一按重放拒绝。
        db.rollback()
        raise CapabilityConsumed()
    db.commit()
    return db.get(Capability, capability_id)


# --------------------------------------------------------------------------
# 签发（对外）
# --------------------------------------------------------------------------


def issue_capability(
    db: Session,
    *,
    tool: str,
    subject: str,
    environment: str,
    artifact: str,
    preparation_id: uuid.UUID | None = None,
    ttl_seconds: int = CAPABILITY_TTL_SECONDS,
    signing_key: SigningKey | None = None,
    commit: bool = True,
) -> CapabilityTokenOut:
    """签发一枚单次能力令牌（FR-26）。

    ``ttl_seconds`` 不得超过 5 分钟（SPEC §5）；默认即为 300 秒。

    ``commit=False`` 供需要**与其他业务事实同事务**的调用方使用（T-12 的 Commit
    要把令牌、运行事实与 Outbox 记录一次提交）；此时提交权归调用方，
    本函数只 ``flush`` 到事务内并回填主键。
    """
    if not 0 < ttl_seconds <= CAPABILITY_TTL_SECONDS:
        raise ValueError(f"令牌有效期必须在 (0, {CAPABILITY_TTL_SECONDS}] 秒内")

    key = _resolve_key(signing_key)
    now = int(time.time())
    claims = CapabilityClaims(
        jti=uuid.uuid4(),
        tool=tool,
        subject=subject,
        environment=environment,
        artifact=artifact,
        iat=now,
        exp=now + ttl_seconds,
    )
    token = sign_claims(claims, signing_key=key)

    capability = Capability(
        id=claims.jti,
        preparation_id=preparation_id,
        tool=tool,
        bound_subject=subject,
        environment=environment,
        bound_artifact=artifact,
        kid=key.kid,
        consumed=False,
        expires_at=datetime.fromtimestamp(claims.exp, tz=UTC),
    )
    db.add(capability)
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(capability)

    return CapabilityTokenOut(
        token=token,
        capability_id=capability.id,
        kid=key.kid,
        tool=tool,
        subject=subject,
        environment=environment,
        artifact=artifact,
        expires_at=capability.expires_at,
    )
