"""Alembic environment: async migrations driven by application settings."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.core.db import Base

# This is the Alembic Config object, which provides access to the values
# within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata used by ``alembic revision --autogenerate`` to diff live
# schemas against the ORM models.
target_metadata = Base.metadata


def _database_url() -> str:
    """Return ``AA_DATABASE_URL`` or fail with an actionable message."""
    url = Settings().database_url
    if not url:
        raise RuntimeError("AA_DATABASE_URL is not set; set it before running migrations")
    return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode, emitting SQL without a live connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on a connection handed over from the async engine."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Connect with an async engine and execute migrations via ``run_sync``."""
    connectable: AsyncEngine = create_async_engine(_database_url(), poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against the live database configured in the environment."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
