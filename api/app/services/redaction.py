"""快照脱敏（SPEC §8：证据落库前必过）。

任何要写进事件、证据或审计记录的 JSON 快照都必须先过这里。理由是**边界**问题：
快照是为了「事后能看懂当时发生了什么」而留的，它的写入点散布在各处，
读取点却是审计台——一旦某处快照顺手带上了连接串，凭据就多了一个长期的、
不受凭据服务管辖的出口。

按 **键名** 脱敏而不是按值：值形态千变万化（URL 里的口令、Base64 里的密钥），
而键名是写入方自己起的，可控且可枚举。这是「宁可少记一点」的选择——
误脱敏的代价是排查时少一块拼图，漏脱敏的代价是凭据泄漏。

三重上限（深度 / 条数 / 单串长度）防止恶意或异常的巨大结构把证据表撑爆——
快照本身不该成为拒绝服务的入口。
"""

import re
from typing import Any

REDACTED = "***"
TRUNCATED = "…(截断)"

_MAX_DEPTH = 8
_MAX_ITEMS = 200
_MAX_TEXT = 2000

# 凭据类键名。只枚举明确的形态，不用宽松的「以 _key 结尾」——
# 那会把 idempotency_key、operation_key 这类调试必需字段一并抹掉，
# 使快照失去价值却换不来任何安全收益。
_SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|credential|authorization|bearer"
    r"|api[_-]?key|access[_-]?key|secret[_-]?key|private[_-]?key|signing[_-]?key"
    r"|encryption[_-]?key|conn(ection)?[_-]?string)",
    re.IGNORECASE,
)


def is_secret_key(key: str) -> bool:
    """键名是否属于凭据类。"""
    return bool(_SECRET_KEY_PATTERN.search(key))


def redact_snapshot(value: Any, *, depth: int = 0) -> Any:
    """递归脱敏并限幅，返回可安全落库的新结构。

    不修改入参——快照常直接来自业务对象，就地改写会把脱敏的影响扩散到调用方。
    """
    if depth > _MAX_DEPTH:
        return TRUNCATED

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                redacted["_truncated"] = f"还有 {len(value) - _MAX_ITEMS} 项未记录"
                break
            name = str(key)
            redacted[name] = (
                REDACTED if is_secret_key(name) else redact_snapshot(item, depth=depth + 1)
            )
        return redacted

    if isinstance(value, (list, tuple)):
        items = [
            redact_snapshot(item, depth=depth + 1) for item in list(value)[:_MAX_ITEMS]
        ]
        if len(value) > _MAX_ITEMS:
            items.append(f"{TRUNCATED} 共 {len(value)} 项")
        return items

    if isinstance(value, str) and len(value) > _MAX_TEXT:
        return value[:_MAX_TEXT] + TRUNCATED

    # 其余标量（数字 / 布尔 / None）本身就是 JSON 安全的，原样返回
    return value
