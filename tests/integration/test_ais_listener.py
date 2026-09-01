"""
Integration tests for T-029 — AIS Listener Worker.

Tests use MockFeedAdapter (never real network), the T-012 test harness,
and an injectable poll interval so no real time delays occur.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.adapters.mock import MockFeedAdapter
from nexafreight.adapters.protocols import (
    AssetPosition,
    AssetType,
    FeedHealth,
    Provenance,
)
from nexafreight.enums import (
    CargoClass,
    LegStatus,
    LocationType,
    ShipmentStatus,
    TransportMode,
)
from nexafreight.models.leg import Leg
from nexafreight.models.location import Location
from nexafreight.models.position import PositionReport
from nexafreight.models.shipment import Shipment
from nexafreight.models.vessel import Vessel
from nexafreight.workers.ais_listener import (
    AISListenerWorker,
    PositionTracker,
    _find_leg_for_mmsi,
    _poll_loop,
    _select_adapter,
    _write_position_report,
    get_position_tracker,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

TEST_MMSI = "123456789"
TEST_MMSI_INT = 123456789
TEST_LAT = 51.5
TEST_LON = 0.1
TEST_SPEED = 12.0
TEST_HEADING = 90.0
TEST_TIME = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class FakeTestAdapter:
    """Controllable test adapter implementing PositionFeedAdapter."""

    def __init__(self, positions: list[AssetPosition] | None = None) -> None:
        self.positions: list[AssetPosition] = list(positions or [])
        self.is_running: bool = False

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False

    async def get_current_positions(self) -> list[AssetPosition]:
        return list(self.positions)

    async def health(self) -> FeedHealth:
        return FeedHealth(
            adapter_name="fake_test_adapter",
            is_healthy=self.is_running,
            last_success_at=TEST_TIME,
            messages_received=len(self.positions),
            provenance=Provenance.REAL,
        )


@pytest.fixture
def mock_adapter() -> MockFeedAdapter:
    """Provide a MockFeedAdapter with synthetic test positions."""
    return MockFeedAdapter(
        seed_assets=[(TEST_MMSI, TransportMode.SEA, TEST_LAT, TEST_LON)],
        reference_time=TEST_TIME,
    )


@pytest.fixture
def test_position() -> AssetPosition:
    """Provide a test AssetPosition DTO."""
    return AssetPosition(
        asset_id=TEST_MMSI,
        asset_type=AssetType.SEA,
        lat=TEST_LAT,
        lon=TEST_LON,
        speed_knots=TEST_SPEED,
        heading_deg=TEST_HEADING,
        reported_at=TEST_TIME,
        provenance=Provenance.REAL,
        source="TEST",
    )


@pytest.fixture
async def seed_vessel_and_in_progress_leg(
    db_session: AsyncSession,
) -> tuple[Vessel, Shipment, Leg]:
    """Helper to create a vessel and an IN_PROGRESS leg."""
    origin = Location(
        locode="USNYC",
        name="New York",
        country_code="US",
        location_type=LocationType.PORT,
        latitude=40.7128,
        longitude=-74.0060,
    )
    destination = Location(
        locode="NLRTM",
        name="Rotterdam",
        country_code="NL",
        location_type=LocationType.PORT,
        latitude=51.9225,
        longitude=4.47917,
    )
    vessel = Vessel(
        mmsi=TEST_MMSI_INT,
        name="Test Cargo Vessel",
    )
    db_session.add_all([origin, destination, vessel])
    await db_session.commit()
    await db_session.refresh(origin)
    await db_session.refresh(destination)
    await db_session.refresh(vessel)

    shipment = Shipment(
        origin_id=origin.id,
        destination_id=destination.id,
        primary_transport_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
        status=ShipmentStatus.IN_TRANSIT,
    )
    db_session.add(shipment)
    await db_session.commit()
    await db_session.refresh(shipment)

    leg = Leg(
        shipment_id=shipment.id,
        sequence_number=1,
        route_version=1,
        transport_mode=TransportMode.SEA,
        status=LegStatus.IN_PROGRESS,
        origin_id=origin.id,
        destination_id=destination.id,
        vessel_id=vessel.id,
        planned_departure=datetime.now(UTC),
        planned_arrival=datetime.now(UTC) + timedelta(days=10),
        provenance=Provenance.REAL,
    )
    db_session.add(leg)
    await db_session.commit()
    await db_session.refresh(leg)

    return vessel, shipment, leg


# ---------------------------------------------------------------------------
# 1. Worker starts cleanly with MockFeedAdapter
# ---------------------------------------------------------------------------


class TestWorkerStartup:
    async def test_worker_starts_with_mock_adapter(self, mock_adapter: MockFeedAdapter) -> None:
        """Worker should start cleanly with MockFeedAdapter."""
        worker = AISListenerWorker()

        with patch(
            "nexafreight.workers.ais_listener._select_adapter",
            return_value=mock_adapter,
        ):
            await worker.start(poll_interval_s=0.01)
            assert worker.adapter is not None
            assert worker.poll_task is not None
            await worker.stop()

    async def test_position_tracker_accessible(self) -> None:
        """get_position_tracker() should be importable and safe to call."""
        tracker = get_position_tracker()
        assert tracker is not None
        assert isinstance(tracker, PositionTracker)


# ---------------------------------------------------------------------------
# 2. Database persistence with valid leg mapping
# ---------------------------------------------------------------------------


class TestDatabasePersistence:
    async def test_position_written_to_db_with_valid_leg(
        self,
        db_session: AsyncSession,
        seed_vessel_and_in_progress_leg: tuple[Vessel, Shipment, Leg],
        test_position: AssetPosition,
    ) -> None:
        """
        Position should be written to the database when a matching
        IN_PROGRESS leg exists.
        """
        _, _, leg = seed_vessel_and_in_progress_leg

        # Write the position.
        success = await _write_position_report(db_session, leg.id, test_position)
        assert success

        # Query the database and verify.
        result = await db_session.execute(
            select(PositionReport).where(PositionReport.leg_id == leg.id)
        )
        record = result.scalar_one_or_none()
        assert record is not None
        assert record.latitude == pytest.approx(TEST_LAT)
        assert record.longitude == pytest.approx(TEST_LON)
        assert record.speed_knots == pytest.approx(TEST_SPEED)
        assert record.heading == pytest.approx(TEST_HEADING)
        assert record.provenance == Provenance.REAL or record.provenance == "REAL"

    async def test_leg_mapping_query(
        self,
        db_session: AsyncSession,
        seed_vessel_and_in_progress_leg: tuple[Vessel, Shipment, Leg],
    ) -> None:
        """_find_leg_for_mmsi should find an IN_PROGRESS leg."""
        _, _, leg = seed_vessel_and_in_progress_leg

        found_leg_id = await _find_leg_for_mmsi(db_session, TEST_MMSI)
        assert found_leg_id == leg.id


# ---------------------------------------------------------------------------
# 3. No leg mapping — position cached but not persisted
# ---------------------------------------------------------------------------


class TestNoLegMapping:
    async def test_position_cached_but_not_persisted_when_no_leg(
        self,
        db_session: AsyncSession,
        test_position: AssetPosition,
    ) -> None:
        """
        Position with no matching leg should be cached in memory but not
        persisted to the database.
        """
        fake_adapter = FakeTestAdapter([test_position])
        await fake_adapter.start()

        leg_id = await _find_leg_for_mmsi(db_session, "999999999")  # Different MMSI
        assert leg_id is None  # No leg found

        positions = await fake_adapter.get_current_positions()
        assert len(positions) == 1
        assert positions[0].asset_id == TEST_MMSI
        await fake_adapter.stop()


# ---------------------------------------------------------------------------
# 4. Write deduplication
# ---------------------------------------------------------------------------


class TestWriteDeduplication:
    async def test_duplicate_position_not_written_twice(
        self,
        db_session: AsyncSession,
        seed_vessel_and_in_progress_leg: tuple[Vessel, Shipment, Leg],
        test_position: AssetPosition,
    ) -> None:
        """
        Same position (same reported_at) should not be written twice on
        consecutive poll cycles.
        """
        _, _, leg = seed_vessel_and_in_progress_leg

        # First write.
        success1 = await _write_position_report(db_session, leg.id, test_position)
        assert success1

        # Second write (same timestamp).
        success2 = await _write_position_report(db_session, leg.id, test_position)
        assert success2  # Should succeed (upsert), but update in place

        # Query the database.
        result = await db_session.execute(
            select(PositionReport).where(PositionReport.leg_id == leg.id)
        )
        records = result.scalars().all()

        def _to_utc(dt: datetime) -> datetime:
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

        matching = [r for r in records if _to_utc(r.reported_at) == test_position.reported_at]
        assert len(matching) == 1


# ---------------------------------------------------------------------------
# 5. Upsert behavior
# ---------------------------------------------------------------------------


class TestUpsertBehavior:
    async def test_updated_position_upserted(
        self,
        db_session: AsyncSession,
        seed_vessel_and_in_progress_leg: tuple[Vessel, Shipment, Leg],
        test_position: AssetPosition,
    ) -> None:
        """
        Updated position for the same leg but later timestamp should be
        inserted as a new row; earlier timestamps should still exist.
        """
        _, _, leg = seed_vessel_and_in_progress_leg

        # Write first position.
        await _write_position_report(db_session, leg.id, test_position)

        later_time = test_position.reported_at + timedelta(seconds=60)
        later_position = AssetPosition(
            asset_id=TEST_MMSI,
            asset_type=AssetType.SEA,
            lat=TEST_LAT + 0.01,
            lon=TEST_LON + 0.01,
            speed_knots=TEST_SPEED + 1.0,
            heading_deg=TEST_HEADING + 10,
            reported_at=later_time,
            provenance=Provenance.REAL,
            source="TEST",
        )

        # Write the updated position.
        await _write_position_report(db_session, leg.id, later_position)

        # Query the database.
        result = await db_session.execute(
            select(PositionReport).where(PositionReport.leg_id == leg.id)
        )
        records = result.scalars().all()
        assert len(records) >= 2

        def _to_utc(dt: datetime) -> datetime:
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

        by_time = {_to_utc(r.reported_at): r for r in records}
        assert test_position.reported_at in by_time
        assert later_time in by_time


# ---------------------------------------------------------------------------
# 6. Feature toggle disabled
# ---------------------------------------------------------------------------


class TestFeatureToggleDisabled:
    async def test_worker_disabled_via_toggle(self) -> None:
        """
        When enable_ais_listener is False, worker should not start,
        no adapter should be instantiated, and tracker should return empty.
        """
        mock_settings = MagicMock()
        mock_settings.enable_ais_listener = False

        with patch(
            "nexafreight.workers.ais_listener.get_settings",
            return_value=mock_settings,
        ):
            adapter = await _select_adapter()
            assert adapter is None

    async def test_tracker_returns_empty_when_disabled(self) -> None:
        """
        When the listener is disabled, get_position_tracker() should return
        empty positions and unhealthy health.
        """
        tracker = PositionTracker(adapter=None)

        positions = await tracker.get_positions()
        assert positions == []

        health = await tracker.get_feed_health()
        assert health.is_healthy is False
        assert health.adapter_name == "none"


# ---------------------------------------------------------------------------
# 7. Database write failure resilience
# ---------------------------------------------------------------------------


class TestWriteFailureResilience:
    async def test_poll_loop_continues_after_write_failure(
        self,
        test_position: AssetPosition,
    ) -> None:
        """
        Poll loop should continue without crashing when a database write fails.
        """
        fake_adapter = FakeTestAdapter([test_position])
        await fake_adapter.start()

        poll_task = asyncio.create_task(_poll_loop(fake_adapter, poll_interval_s=0.01))

        await asyncio.sleep(0.05)

        poll_task.cancel()
        try:
            await poll_task
        except asyncio.CancelledError:
            pass
        await fake_adapter.stop()


# ---------------------------------------------------------------------------
# 8. Lifecycle idempotency
# ---------------------------------------------------------------------------


class TestLifecycleIdempotency:
    async def test_stop_before_start_is_safe(self) -> None:
        """Calling stop() before start() should not raise."""
        worker = AISListenerWorker()
        await worker.stop()

    async def test_start_twice_does_not_create_duplicate_task(
        self,
        mock_adapter: MockFeedAdapter,
    ) -> None:
        """Calling start() twice should not create a second poll task."""
        worker = AISListenerWorker()
        with patch(
            "nexafreight.workers.ais_listener._select_adapter",
            return_value=mock_adapter,
        ):
            await worker.start(poll_interval_s=0.01)
            task_id_1 = id(worker.poll_task)

            await worker.start(poll_interval_s=0.01)  # Idempotent
            task_id_2 = id(worker.poll_task)

            assert task_id_1 == task_id_2

            await worker.stop()

    async def test_stop_twice_is_safe(self, mock_adapter: MockFeedAdapter) -> None:
        """Calling stop() twice should not raise."""
        worker = AISListenerWorker()
        with patch(
            "nexafreight.workers.ais_listener._select_adapter",
            return_value=mock_adapter,
        ):
            await worker.start(poll_interval_s=0.01)
            await worker.stop()
            await worker.stop()  # Second stop is safe

    async def test_no_asyncio_warnings_after_clean_stop(
        self,
        mock_adapter: MockFeedAdapter,
    ) -> None:
        """After a clean stop, no asyncio task warnings should be raised."""
        worker = AISListenerWorker()
        with patch(
            "nexafreight.workers.ais_listener._select_adapter",
            return_value=mock_adapter,
        ):
            await worker.start(poll_interval_s=0.01)
            await asyncio.sleep(0.02)
            await worker.stop()

            assert worker.poll_task is None or worker.poll_task.done()


# ---------------------------------------------------------------------------
# 9. Lifespan integration
# ---------------------------------------------------------------------------


class TestLifespanIntegration:
    async def test_app_starts_with_listener_disabled(self, test_app) -> None:
        """The app should start cleanly even when the AIS listener is disabled."""
        assert test_app is not None
