"""跨方言列类型。

生产库是 PostgreSQL，JSON 列一律用 JSONB（可索引、查询效率高）；
测试用内存 SQLite，退化为通用 JSON。同一份模型两边都能建表。
"""

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

JsonColumn = JSON().with_variant(JSONB(), "postgresql")
