"""Async database engine and session management for NexaFreight Control Tower."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool, StaticPool

from nexafreight.config import Settings, get_settings
from nexafreight.models.base import Base


def _set_sqlite_pragmas(dbapi_connection: Any, connection_record: Any, *, is_memory: bool) -> None:
    """Configure SQLite pragmas on every new DBAPI connection.

    - journal_mode=WAL: allows concurrent readers while a writer is active,
      critical since APScheduler workers and FastAPI request handlers read/write
      concurrently in this single-process, $0-budget architecture.
    - foreign_keys=ON: SQLite disables FK enforcement by default; the domain
      model (T-007) relies on FK constraints for referential integrity.
    - synchronous=NORMAL: safe durability/performance tradeoff in WAL mode —
      risks only the last transaction on power loss, acceptable here.
    - busy_timeout=5000: avoids "database is locked" errors under concurrent
      access by waiting up to 5s instead of failing immediately.

    Args:
        dbapi_connection: Raw DBAPI connection object (aiosqlite Connection).
        connection_record: SQLAlchemy connection record (unused, required by event signature).
        is_memory: If True, skip WAL mode (not supported for :memory: databases).

    Note:
        The event.listens_for signature requires `Any` for DBAPI connection
        types as they vary by driver and are not part of SQLAlchemy's type system.
    """
    cursor = dbapi_connection.cursor()
    try:
        if not is_memory:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def create_engine(settings: Settings | None = None, *, echo: bool | None = None) -> AsyncEngine:
    """Create the production async SQLAlchemy engine.

    Args:
        settings: Application settings; defaults to get_settings() if None.
        echo: SQLAlchemy echo mode; defaults to settings.debug if None.

    Returns:
        Configured AsyncEngine with WAL mode and foreign key enforcement.

    Example:
        >>> engine = create_engine()
        >>> async with engine.begin() as conn:
        ...     await conn.execute(text("SELECT 1"))
    """
    if settings is None:
        settings = get_settings()

    database_url = settings.database_url
    is_memory = ":memory:" in database_url

    engine = create_async_engine(
        database_url,
        echo=echo if echo is not None else settings.debug,
    )

    # Register pragma setter on the sync engine (aiosqlite DBAPI events fire on sync_engine)
    @event.listens_for(engine.sync_engine, "connect")
    def on_connect(dbapi_conn: Any, connection_record: Any) -> None:
        _set_sqlite_pragmas(dbapi_conn, connection_record, is_memory=is_memory)

    return engine


def create_test_engine(database_url: str = "sqlite+aiosqlite:///:memory:") -> AsyncEngine:
    """Create an async SQLAlchemy engine for testing.

    Args:
        database_url: Test database URL; defaults to in-memory SQLite.

    Returns:
        Test AsyncEngine with StaticPool (required for in-memory persistence).

    Note:
        Uses StaticPool and check_same_thread=False so the single in-memory
        database persists across async pool connections within a test.
    """
    is_memory = ":memory:" in database_url

    engine = create_async_engine(
        database_url,
        echo=False,
        poolclass=StaticPool if is_memory else NullPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def on_connect(dbapi_conn: Any, connection_record: Any) -> None:
        _set_sqlite_pragmas(dbapi_conn, connection_record, is_memory=is_memory)

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the async session factory.

    Args:
        engine: AsyncEngine to bind sessions to.

    Returns:
        Configured async_sessionmaker.

    Configuration:
        - expire_on_commit=False: required because API response serialization
          (T-021+) accesses ORM attributes after commit within the same request;
          without this, attribute access after commit raises on an expired session.
        - autoflush=False: gives services explicit control over flush timing,
          avoiding surprising partial writes when building up multiple related
          objects before committing (e.g. T-017's consolidation engine).
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """Get cached production AsyncEngine singleton.

    Returns:
        Production AsyncEngine (same object on repeated calls).

    Note:
        Call get_engine.cache_clear() in test teardown to reset state.
    """
    return create_engine()


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get cached async session factory singleton.

    Returns:
        async_sessionmaker bound to the production engine.

    Note:
        Call get_session_factory.cache_clear() in test teardown to reset state.
    """
    return create_session_factory(get_engine())


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Request-scoped AsyncSession dependency for FastAPI.

    Yields:
        AsyncSession: Database session with automatic cleanup.

    Behavior:
        - Rolls back on exception.
        - Always closes the session.
        - Does NOT auto-commit — commits are the explicit responsibility of
          route/service code, keeping transaction boundaries visible in business logic.

    Example:
        >>> from fastapi import Depends
        >>> async def my_route(db: AsyncSession = Depends(get_db_session)):
        ...     result = await db.execute(select(User))
        ...     await db.commit()
    """
    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# Compatibility alias
get_db = get_db_session


async def dispose_engine(engine: AsyncEngine | None = None) -> None:
    """Dispose of the engine's connection pool.

    Args:
        engine: Engine to dispose; defaults to get_engine() if None.

    Note:
        Called during FastAPI lifespan shutdown (T-009).
    """
    target = engine or get_engine()
    await target.dispose()


async def create_all_tables(engine: AsyncEngine) -> None:
    """TEST-ONLY: Create all tables from Base.metadata directly.

    Args:
        engine: AsyncEngine to create tables on.

    Warning:
        Production/integration environments MUST use Alembic migrations (T-008)
        as the single source of schema truth. This exists only so isolated unit
        tests can get a schema without depending on migration state.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all_tables(engine: AsyncEngine) -> None:
    """TEST-ONLY: Drop all tables from Base.metadata.

    Args:
        engine: AsyncEngine to drop tables from.

    Warning:
        Mirrors create_all_tables. Never use in production.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
