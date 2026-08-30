"""Tests for async database engine and session management."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import Integer, String, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from nexafreight.database import (
    create_all_tables,
    create_session_factory,
    create_test_engine,
    dispose_engine,
    drop_all_tables,
    get_db_session,
    get_engine,
    get_session_factory,
)
from nexafreight.models.base import Base


class _ProbeModel(Base):
    """Throwaway test-only model for verifying database functionality."""

    __tablename__ = "probe_test_table"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)


@pytest.fixture(autouse=True, scope="module")
def _cleanup_probe_table():
    yield
    if "probe_test_table" in Base.metadata.tables:
        Base.metadata.remove(Base.metadata.tables["probe_test_table"])


@pytest.mark.asyncio
async def test_create_test_engine_returns_working_engine() -> None:
    """create_test_engine() returns a functioning in-memory AsyncEngine."""
    engine = create_test_engine()
    assert isinstance(engine, AsyncEngine)

    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT 1 AS test"))
        row = result.fetchone()
        assert row is not None
        assert row[0] == 1

    await dispose_engine(engine)


@pytest.mark.asyncio
async def test_create_all_and_drop_all_tables() -> None:
    """create_all_tables and drop_all_tables manage schema correctly."""
    engine = create_test_engine()

    # Create schema
    await create_all_tables(engine)

    # Insert and query data
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        probe = _ProbeModel(id=1, name="test_record")
        session.add(probe)
        await session.commit()

    # Verify in new session
    async with session_factory() as session:
        result = await session.execute(select(_ProbeModel).where(_ProbeModel.id == 1))
        retrieved = result.scalar_one_or_none()
        assert retrieved is not None
        assert retrieved.name == "test_record"

    # Drop schema
    await drop_all_tables(engine)

    # Verify table no longer exists
    async with session_factory() as session:
        with pytest.raises(OperationalError, match="no such table"):
            await session.execute(select(_ProbeModel))

    await dispose_engine(engine)


@pytest.mark.asyncio
async def test_pragma_foreign_keys_enabled() -> None:
    """Foreign key enforcement is enabled via pragma."""
    engine = create_test_engine()

    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA foreign_keys"))
        fk_enabled = result.scalar()
        assert fk_enabled == 1  # 1 = ON

    await dispose_engine(engine)


@pytest.mark.asyncio
async def test_wal_mode_on_file_database(tmp_path: Path) -> None:
    """WAL mode is applied to file-based SQLite databases."""
    db_path = tmp_path / "test_wal.db"
    database_url = f"sqlite+aiosqlite:///{db_path}"

    engine = create_test_engine(database_url)

    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA journal_mode"))
        journal_mode = result.scalar()
        assert str(journal_mode).lower() == "wal"

    await dispose_engine(engine)


@pytest.mark.asyncio
async def test_wal_mode_skipped_for_memory_database() -> None:
    """In-memory databases do not use WAL mode (correctly skipped)."""
    engine = create_test_engine("sqlite+aiosqlite:///:memory:")

    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA journal_mode"))
        journal_mode = result.scalar()
        # In-memory SQLite typically reports "memory" or "delete", not "wal"
        assert journal_mode in ("memory", "delete")

    await dispose_engine(engine)


def test_get_engine_is_cached_singleton() -> None:
    """get_engine() returns the same object on repeated calls."""
    engine1 = get_engine()
    engine2 = get_engine()

    assert engine1 is engine2

    # Clear cache and verify new object
    get_engine.cache_clear()
    engine3 = get_engine()

    assert engine3 is not engine1

    # Cleanup
    get_engine.cache_clear()


def test_get_session_factory_is_cached_singleton() -> None:
    """get_session_factory() returns the same object on repeated calls."""
    factory1 = get_session_factory()
    factory2 = get_session_factory()

    assert factory1 is factory2

    # Clear cache
    get_session_factory.cache_clear()
    factory3 = get_session_factory()

    assert factory3 is not factory1

    # Cleanup
    get_session_factory.cache_clear()
    get_engine.cache_clear()


@pytest.mark.asyncio
async def test_get_db_session_rolls_back_on_exception() -> None:
    """get_db_session() rolls back uncommitted changes on exception."""
    engine = create_test_engine()
    await create_all_tables(engine)

    # Manually create a session factory for testing
    session_factory = create_session_factory(engine)

    with patch("nexafreight.database.get_session_factory", return_value=session_factory):
        session_gen = get_db_session()

        try:
            session: AsyncSession = await session_gen.__anext__()

            # Insert without committing
            probe = _ProbeModel(id=999, name="should_not_persist")
            session.add(probe)
            await session.flush()

            # Simulate exception
            raise RuntimeError("Simulated request failure")
        except RuntimeError:
            pass
        finally:
            try:
                await session_gen.__anext__()
            except StopAsyncIteration:
                pass

    # Verify rollback: row should not exist
    async with session_factory() as verify_session:
        result = await verify_session.execute(select(_ProbeModel).where(_ProbeModel.id == 999))
        retrieved = result.scalar_one_or_none()
        assert retrieved is None  # Rollback successful

    await drop_all_tables(engine)
    await dispose_engine(engine)


@pytest.mark.asyncio
async def test_dispose_engine_completes_without_error() -> None:
    """dispose_engine() successfully disposes engine."""
    engine = create_test_engine()

    # Perform some operation
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))

    # Dispose should complete without raising
    await dispose_engine(engine)


@pytest.mark.asyncio
async def test_busy_timeout_pragma_set() -> None:
    """busy_timeout pragma is configured to avoid lock errors."""
    engine = create_test_engine()

    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA busy_timeout"))
        timeout = result.scalar()
        assert timeout == 5000  # 5000ms = 5s

    await dispose_engine(engine)


@pytest.mark.asyncio
async def test_synchronous_pragma_set() -> None:
    """synchronous pragma is set to NORMAL for WAL mode."""
    engine = create_test_engine()

    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA synchronous"))
        sync_mode = result.scalar()
        # NORMAL = 1 in SQLite
        assert sync_mode == 1

    await dispose_engine(engine)
