"""Alembic 迁移环境。

数据库地址统一从应用配置（app.core.config）读取，避免 alembic.ini 与 .env 两处维护。
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# 导入模型包，使 Base.metadata 完整（autogenerate 才认得全表）。
# 注意：models 包的 __init__ 必须 re-export 每一个模型模块，
# 否则「模型未导入」会被 autogenerate 误判成「表该删」，生成 drop table 的迁移。
import app.models  # noqa: F401
from alembic import context
from app.core.config import settings
from app.db.base import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# 由第三方组件自建自管的表，不归 Alembic 管，必须显式排除——
# 否则 autogenerate 与 `alembic check` 会认为它们「多余」，生成删除它们的迁移。
_EXTERNAL_TABLE_PREFIXES = ("checkpoint",)  # LangGraph 的 PostgresSaver


def include_object(object_, name, type_, reflected, compare_to):
    """autogenerate / check 的对象过滤器。"""
    if type_ == "table" and name.startswith(_EXTERNAL_TABLE_PREFIXES):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
