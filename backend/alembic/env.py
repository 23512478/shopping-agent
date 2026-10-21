"""Alembic 迁移环境：连接串统一从 DATABASE_URL 环境变量拿，不写死在 ini 里。

本地和 compose 里的值都是 postgresql:// 开头，SQLAlchemy 要带驱动名，
psycopg 3 的驱动是 postgresql+psycopg://，这里统一翻译。
"""
import os

from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    dsn = os.getenv("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL 未配置，迁移不知道往哪个库跑。"
            "本地联调：postgresql://agent:agent@localhost:5432/shopping"
        )
    # postgresql:// 是 psycopg2 的默认驱动名，本机装的是 psycopg 3
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


def run_migrations_offline() -> None:
    """离线模式：不连库，把 SQL 打印出来（--sql 参数用）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行。"""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
