"""Integration tests for Alembic migration pipeline."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from nexafreight.database import create_session_factory, create_test_engine
from nexafreight.enums import (
    AlertSeverity,
    CargoClass,
    DisruptionStatus,
    DisruptionType,
    LocationType,
    Provenance,
    TransportMode,
    UserRole,
)
from nexafreight.models import Alert, Disruption, Leg, Location, Shipment, User


def run_alembic_command(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run an alembic command against a specific database file.

    Args:
        db_path: Path to SQLite database file
        *args: Alembic command arguments (e.g., "upgrade", "head")

    Returns:
        CompletedProcess with stdout/stderr
    """
    env = {**os.environ, "DATABASE_PATH": str(db_path)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result


@pytest.mark.asyncio
async def test_upgrade_to_head_from_empty_database(tmp_path: Path) -> None:
    """Upgrading from empty database to head succeeds."""
    db_path = tmp_path / "test.db"

    # Run migration
    result = run_alembic_command(db_path, "upgrade", "head")

    # Should succeed
    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    combined_output = (result.stdout + result.stderr).lower()
    assert "running upgrade" in combined_output or "001_initial_schema" in combined_output

    # Database file should exist
    assert db_path.exists()


@pytest.mark.asyncio
async def test_all_expected_tables_exist_after_upgrade(tmp_path: Path) -> None:
    """All T-007 model tables exist after migration."""
    db_path = tmp_path / "test.db"

    # Run migration
    result = run_alembic_command(db_path, "upgrade", "head")
    assert result.returncode == 0, f"Migration failed: {result.stderr}"

    # Create engine and inspect schema
    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_test_engine(database_url)

    try:
        async with engine.begin() as conn:

            def get_table_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                return set(inspector.get_table_names())

            tables = await conn.run_sync(get_table_names)

        expected_tables = {
            "users",
            "locations",
            "ports",
            "port_daily_stats",
            "vessels",
            "orders",
            "order_items",
            "shipments",
            "legs",
            "position_reports",
            "disruptions",
            "alerts",
            "corridor_alternatives",
            "decisions",
            "audit_logs",
            "alembic_version",  # Alembic's version tracking table
        }
        assert (
            tables == expected_tables
        ), f"Missing: {expected_tables - tables}, Extra: {tables - expected_tables}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_foreign_key_enforcement_in_migrated_schema(tmp_path: Path) -> None:
    """Foreign key constraints are enforced in Alembic-created schema."""
    db_path = tmp_path / "test.db"

    # Run migration
    result = run_alembic_command(db_path, "upgrade", "head")
    assert result.returncode == 0, f"Migration failed: {result.stderr}"

    # Create engine and attempt invalid FK insert
    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_test_engine(database_url)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            # Attempt to insert shipment with non-existent origin
            shipment = Shipment(
                origin_id=999999,  # Does not exist
                destination_id=999998,
                primary_transport_mode=TransportMode.SEA,
                cargo_class=CargoClass.STANDARD,
            )
            session.add(shipment)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unique_constraint_duplicate_active_disruption(tmp_path: Path) -> None:
    """Uniqueness constraint for active disruptions is enforced."""
    db_path = tmp_path / "test.db"

    # Run migration
    result = run_alembic_command(db_path, "upgrade", "head")
    assert result.returncode == 0, f"Migration failed: {result.stderr}"

    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_test_engine(database_url)
    session_factory = create_session_factory(engine)

    try:
        # Create minimal dependencies
        async with session_factory() as session:
            location = Location(
                locode="USNYC",
                name="New York",
                country_code="US",
                location_type=LocationType.PORT,
                latitude=40.7128,
                longitude=-74.0060,
            )
            session.add(location)
            await session.flush()

            shipment = Shipment(
                origin_id=location.id,
                destination_id=location.id,
                primary_transport_mode=TransportMode.SEA,
                cargo_class=CargoClass.STANDARD,
            )
            session.add(shipment)
            await session.flush()

            leg = Leg(
                shipment_id=shipment.id,
                sequence_number=1,
                route_version=1,
                transport_mode=TransportMode.SEA,
                origin_id=location.id,
                destination_id=location.id,
                planned_departure=datetime.now(UTC),
                planned_arrival=datetime.now(UTC) + timedelta(days=1),
                provenance=Provenance.MOCK,
            )
            session.add(leg)
            await session.flush()

            disruption1 = Disruption(
                shipment_id=shipment.id,
                leg_id=leg.id,
                disruption_type=DisruptionType.VESSEL_DELAY,
                status=DisruptionStatus.ACTIVE,
                description="First delay",
                detected_at=datetime.now(UTC),
            )
            session.add(disruption1)
            await session.commit()

        # Attempt duplicate in new session
        async with session_factory() as session:
            disruption2 = Disruption(
                shipment_id=shipment.id,
                leg_id=leg.id,
                disruption_type=DisruptionType.VESSEL_DELAY,
                status=DisruptionStatus.ACTIVE,
                description="Duplicate delay",
                detected_at=datetime.now(UTC),
            )
            session.add(disruption2)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_unique_constraint_one_alert_per_disruption(tmp_path: Path) -> None:
    """Uniqueness constraint for one alert per disruption is enforced."""
    db_path = tmp_path / "test.db"

    result = run_alembic_command(db_path, "upgrade", "head")
    assert result.returncode == 0, f"Migration failed: {result.stderr}"

    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_test_engine(database_url)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            location = Location(
                locode="USNYC",
                name="New York",
                country_code="US",
                location_type=LocationType.PORT,
                latitude=40.7128,
                longitude=-74.0060,
            )
            session.add(location)
            await session.flush()

            shipment = Shipment(
                origin_id=location.id,
                destination_id=location.id,
                primary_transport_mode=TransportMode.SEA,
                cargo_class=CargoClass.STANDARD,
            )
            session.add(shipment)
            await session.flush()

            disruption = Disruption(
                shipment_id=shipment.id,
                disruption_type=DisruptionType.PORT_CONGESTION,
                status=DisruptionStatus.ACTIVE,
                description="Congestion",
                detected_at=datetime.now(UTC),
            )
            session.add(disruption)
            await session.flush()

            # Create first alert
            alert1 = Alert(
                disruption_id=disruption.id,
                shipment_id=shipment.id,
                severity=AlertSeverity.HIGH,
                financial_exposure=5000.0,
            )
            session.add(alert1)
            await session.commit()

        # Attempt duplicate
        async with session_factory() as session:
            alert2 = Alert(
                disruption_id=disruption.id,
                shipment_id=shipment.id,
                severity=AlertSeverity.MEDIUM,
                financial_exposure=3000.0,
            )
            session.add(alert2)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_to_base_succeeds(tmp_path: Path) -> None:
    """Downgrading to base removes all application tables."""
    db_path = tmp_path / "test.db"

    # Upgrade first
    result = run_alembic_command(db_path, "upgrade", "head")
    assert result.returncode == 0, f"Upgrade failed: {result.stderr}"

    # Downgrade
    result = run_alembic_command(db_path, "downgrade", "base")
    assert result.returncode == 0, f"Downgrade failed: {result.stderr}"

    # Check remaining tables
    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_test_engine(database_url)

    try:
        async with engine.begin() as conn:

            def get_table_names(sync_conn: object) -> set[str]:
                inspector = inspect(sync_conn)
                return set(inspector.get_table_names())

            tables = await conn.run_sync(get_table_names)

        # Only alembic_version should remain
        assert tables == {
            "alembic_version"
        }, f"Unexpected tables after downgrade: {tables - {'alembic_version'}}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upgrade_after_downgrade_succeeds(tmp_path: Path) -> None:
    """Re-upgrading after downgrade reproduces working schema."""
    db_path = tmp_path / "test.db"

    # Initial upgrade
    result = run_alembic_command(db_path, "upgrade", "head")
    assert result.returncode == 0, f"Initial upgrade failed: {result.stderr}"

    # Downgrade
    result = run_alembic_command(db_path, "downgrade", "base")
    assert result.returncode == 0, f"Downgrade failed: {result.stderr}"

    # Re-upgrade
    result = run_alembic_command(db_path, "upgrade", "head")
    assert result.returncode == 0, f"Re-upgrade failed: {result.stderr}"

    # Verify schema works by inserting data
    database_url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_test_engine(database_url)
    session_factory = create_session_factory(engine)

    try:
        async with session_factory() as session:
            user = User(
                email="test@example.com",
                hashed_password="hash",
                role=UserRole.OPERATOR,
            )
            session.add(user)
            await session.commit()

        # Verify query
        async with session_factory() as session:
            result_user = await session.execute(
                select(User).where(User.email == "test@example.com")
            )
            retrieved = result_user.scalar_one_or_none()
            assert retrieved is not None
            assert retrieved.role == UserRole.OPERATOR
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_second_upgrade_is_noop(tmp_path: Path) -> None:
    """Running upgrade twice is idempotent (Alembic's version tracking)."""
    db_path = tmp_path / "test.db"

    # First upgrade
    result1 = run_alembic_command(db_path, "upgrade", "head")
    assert result1.returncode == 0, f"First upgrade failed: {result1.stderr}"

    # Second upgrade (should be no-op)
    result2 = run_alembic_command(db_path, "upgrade", "head")
    assert result2.returncode == 0, f"Second upgrade failed: {result2.stderr}"

    # Should indicate already at head or no new migrations to run
    combined_output = (result2.stdout + result2.stderr).lower()
    assert (
        "already" in combined_output
        or "running upgrade" not in combined_output
        or result2.stdout.strip() == ""
    )
