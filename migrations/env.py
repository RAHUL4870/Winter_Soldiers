"""Async Alembic migration environment for NexaFreight Control Tower.

This module bridges Alembic's synchronous migration execution with
SQLAlchemy's async engine, using the project's typed settings as the
single source of truth for database configuration.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import event, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import nexafreight.models  # noqa: F401 (Import all models to register them on Base.metadata)
from nexafreight.config import get_settings
from nexafreight.models.base import Base

# Alembic Config object (provides access to alembic.ini values)
config = context.config

# Interpret the config file for Python logging unless explicitly told not to
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogeneration (all models from T-007)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL script generation without DB connection).

    This configures the context with just a URL and not an Engine, though an
    Engine is acceptable here as well. By skipping the Engine creation we don't
    even need a DBAPI to be available. Calls to context.execute() here emit the
    given string to the script output.
    """
    settings = get_settings()
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Execute migrations within a synchronous connection context.

    Called by run_async_migrations via connection.run_sync().
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode using an async engine.

    Creates an async engine from the project's settings, acquires a connection,
    and runs migrations synchronously within that connection via run_sync().
    """
    settings = get_settings()

    # Build async engine configuration from settings
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = settings.database_url

    # Create async engine with NullPool for migrations
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # Apply SQLite-specific pragmas on connect
    is_memory = ":memory:" in settings.database_url

    @event.listens_for(connectable.sync_engine, "connect")
    def set_sqlite_pragmas(dbapi_conn: Any, connection_record: Any) -> None:
        cursor = dbapi_conn.cursor()
        try:
            if not is_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations (async execution)."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
