"""跨方言列类型与时区工具。

生产库是 PostgreSQL，JSON 列一律用 JSONB（可索引、查询效率高）；
测试用内存 SQLite，退化为通用 JSON。同一份模型两边都能建表。
"""

from datetime import UTC, datetime

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

JsonColumn = JSON().with_variant(JSONB(), "postgresql")


def as_utc(value: datetime) -> datetime:
    """把可能为 naive 的时间统一为带时区的 UTC。

    PostgreSQL 的 ``timestamptz`` 读回来就是 aware 的，而 SQLite 没有时区类型，
    ``DateTime(timezone=True)`` 读回来是 naive。测试跑 SQLite、生产跑 PostgreSQL，
    于是「和现在比大小」这类判断在两个环境里行为不同——在测试里抛
    ``TypeError``，在生产里悄悄按 UTC 解释。

    所有跨这两个方言语义的时间比较都必须先过这个函数。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
