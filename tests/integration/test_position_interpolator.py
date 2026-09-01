"""
Integration tests for T-030 — Position Interpolator Worker.

Test-isolation guarantees:
- No real 30-second waits: _run_interpolation_job() is called directly or with long intervals.
- Mocked/synthetic adapters: no network I/O.
- In-memory SQLite database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update

from nexafreight.adapters.protocols import AssetPosition, AssetType, Provenance
from nexafreight.enums import LegStatus, TransportMode
from nexafreight.models.leg import Leg
from nexafreight.models.position import PositionReport
from nexafreight.workers.position_interpolator import (
    PositionInterpolatorWorker,
    _cleanup_stale_positions,
    _compute_duration,
    _interpolate_leg,
    _LegData,
    _parse_geometry,
    _query_active_legs,
    _run_interpolation_job,
    _write_position,
    get_current_positions,
    get_interpolator_worker,
)

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

T0 = datetime(2024, 1, 1, 8, 0, 0, tzinfo=UTC)
T_ARRIVAL = T0 + timedelta(hours=4)
NOW = datetime(2024, 1, 1, 10, 0, 0, tzinfo=UTC)  # 2h into a 4h leg → 50%

SIMPLE_LINESTRING = json.dumps(
    {
        "type": "LineString",
        "coordinates": [
            [0.0, 0.0],
            [1.0, 1.0],
        ],
    }
)

GEODESIC_ARC = json.dumps(
    {
        "type": "LineString",
        "coordinates": [
            [-118.24, 34.05],  # Los Angeles
            [139.69, 35.69],  # Tokyo
        ],
    }
)

MALFORMED_GEOJSON = '{"type": "Point", "coordinates": [0, 0]}'
INVALID_JSON = "not valid json {{{}"


def _to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


# ---------------------------------------------------------------------------
# Fake leg builder
# ---------------------------------------------------------------------------


def make_leg_data(
    leg_id: int = 1,
    mode: str = "ROAD",
    geometry: str | None = SIMPLE_LINESTRING,
    departure: datetime | None = T0,
    arrival: datetime | None = T_ARRIVAL,
    actual_departure: datetime | None = None,
) -> _LegData:
    return _LegData(
        leg_id=leg_id,
        mode=mode,
        route_geometry=geometry,
        planned_departure=departure,
        planned_arrival=arrival,
        actual_departure=actual_departure,
    )


# ---------------------------------------------------------------------------
# Fake position helper
# ---------------------------------------------------------------------------


def fake_position(leg_id: str, lat: float = 0.5, lon: float = 0.5) -> AssetPosition:
    return AssetPosition(
        asset_id=leg_id,
        asset_type=AssetType.ROAD,
        lat=lat,
        lon=lon,
        speed_knots=30.0,
        heading_deg=45.0,
        reported_at=NOW,
        provenance=Provenance.SIMULATED,
        source="ROAD_INTERPOLATION",
    )


# ===========================================================================
# 1. Worker starts cleanly
# ===========================================================================


class TestWorkerStartup:
    async def test_worker_starts_with_toggle_enabled(self) -> None:
        """APScheduler should start and a job should be registered."""
        worker = PositionInterpolatorWorker()

        with patch("nexafreight.workers.position_interpolator.get_settings") as mock_settings:
            mock_settings.return_value.enable_position_interpolator = True
            await worker.start(interval_seconds=300)  # Long interval — won't run
            assert worker.is_running
            assert worker._scheduler is not None
            assert worker._scheduler.get_job("position_interpolator") is not None
            await worker.stop()
            assert not worker.is_running

    async def test_worker_does_not_start_with_toggle_disabled(self) -> None:
        """When disabled, worker should not create a scheduler."""
        worker = PositionInterpolatorWorker()
        with patch("nexafreight.workers.position_interpolator.get_settings") as mock_settings:
            mock_settings.return_value.enable_position_interpolator = False
            await worker.start()
            assert not worker.is_running
            assert worker._scheduler is None

    async def test_get_current_positions_empty_when_disabled(self) -> None:
        """get_current_positions() should return empty dict when worker is off."""
        import nexafreight.workers.position_interpolator as mod

        mod._position_cache.clear()
        result = get_current_positions()
        assert result == {}

    async def test_get_interpolator_worker_returns_singleton(self) -> None:
        """get_interpolator_worker() should return the same instance."""
        w1 = get_interpolator_worker()
        w2 = get_interpolator_worker()
        assert w1 is w2


# ===========================================================================
# 2. Lifecycle idempotency
# ===========================================================================


class TestLifecycleIdempotency:
    async def test_stop_before_start_is_safe(self) -> None:
        worker = PositionInterpolatorWorker()
        await worker.stop()  # Must not raise

    async def test_double_stop_is_safe(self) -> None:
        worker = PositionInterpolatorWorker()
        with patch("nexafreight.workers.position_interpolator.get_settings") as mock_settings:
            mock_settings.return_value.enable_position_interpolator = True
            await worker.start(interval_seconds=300)
            await worker.stop()
            await worker.stop()  # Must not raise

    async def test_double_start_does_not_create_second_scheduler(self) -> None:
        worker = PositionInterpolatorWorker()
        with patch("nexafreight.workers.position_interpolator.get_settings") as mock_settings:
            mock_settings.return_value.enable_position_interpolator = True
            await worker.start(interval_seconds=300)
            sched_id_1 = id(worker._scheduler)
            await worker.start(interval_seconds=300)  # Second start
            sched_id_2 = id(worker._scheduler)
            assert sched_id_1 == sched_id_2  # Same scheduler
            await worker.stop()

    async def test_stop_cleans_up_scheduler(self) -> None:
        worker = PositionInterpolatorWorker()
        with patch("nexafreight.workers.position_interpolator.get_settings") as mock_settings:
            mock_settings.return_value.enable_position_interpolator = True
            await worker.start(interval_seconds=300)
            await worker.stop()
            assert worker._scheduler is None
            assert worker._started is False


# ===========================================================================
# 3. Leg query filtering
# ===========================================================================


class TestLegQueryFiltering:
    async def test_query_returns_only_in_progress_road_and_air(
        self, db_session, make_leg, make_shipment
    ) -> None:
        """
        Only IN_PROGRESS ROAD and AIR legs should be returned.
        SEA and COMPLETED legs must be excluded.
        """
        shipment = await make_shipment()

        road_leg = await make_leg(
            shipment_id=shipment.id, mode=TransportMode.ROAD, status=LegStatus.IN_PROGRESS
        )
        air_leg = await make_leg(
            shipment_id=shipment.id, mode=TransportMode.AIR, status=LegStatus.IN_PROGRESS
        )
        _sea_leg = await make_leg(
            shipment_id=shipment.id, mode=TransportMode.SEA, status=LegStatus.IN_PROGRESS
        )
        _completed_leg = await make_leg(
            shipment_id=shipment.id, mode=TransportMode.ROAD, status=LegStatus.COMPLETED
        )

        legs = await _query_active_legs(db_session)
        returned_ids = {leg.leg_id for leg in legs}
        assert road_leg.id in returned_ids
        assert air_leg.id in returned_ids
        assert _sea_leg.id not in returned_ids
        assert _completed_leg.id not in returned_ids

    async def test_query_returns_empty_when_no_active_legs(self, db_session) -> None:
        """Empty database should return empty list."""
        legs = await _query_active_legs(db_session)
        assert legs == []

    async def test_query_returns_correct_fields(self, db_session, make_leg, make_shipment) -> None:
        """Returned _LegData should have correct mode, geometry, and times."""
        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.IN_PROGRESS,
            route_geometry=SIMPLE_LINESTRING,
            planned_departure=T0,
            planned_arrival=T_ARRIVAL,
        )

        legs = await _query_active_legs(db_session)
        assert len(legs) == 1
        ld = legs[0]
        assert ld.leg_id == leg.id
        assert ld.mode == "ROAD"
        assert ld.route_geometry == SIMPLE_LINESTRING


# ===========================================================================
# 4. Geometry validation
# ===========================================================================


class TestGeometryValidation:
    async def test_valid_linestring_accepted(self) -> None:
        result = _parse_geometry(1, SIMPLE_LINESTRING)
        assert result == SIMPLE_LINESTRING

    async def test_none_geometry_rejected(self) -> None:
        result = _parse_geometry(1, None)
        assert result is None

    async def test_empty_string_rejected(self) -> None:
        result = _parse_geometry(1, "")
        assert result is None

    async def test_invalid_json_rejected(self) -> None:
        result = _parse_geometry(1, INVALID_JSON)
        assert result is None

    async def test_non_linestring_type_rejected(self) -> None:
        point = json.dumps({"type": "Point", "coordinates": [0.0, 0.0]})
        result = _parse_geometry(1, point)
        assert result is None

    async def test_feature_collection_accepted(self) -> None:
        fc = json.dumps(
            {
                "type": "FeatureCollection",
                "features": [],
            }
        )
        result = _parse_geometry(1, fc)
        assert result == fc


# ===========================================================================
# 5. Duration computation
# ===========================================================================


class TestDurationComputation:
    async def test_valid_duration_computed(self) -> None:
        leg = make_leg_data(departure=T0, arrival=T0 + timedelta(hours=4))
        duration = _compute_duration(leg)
        assert duration == pytest.approx(4 * 3600.0)

    async def test_actual_departure_preferred_over_planned(self) -> None:
        actual_dep = T0 + timedelta(hours=1)  # Departed 1h late
        leg = make_leg_data(
            departure=T0,
            arrival=T0 + timedelta(hours=5),
            actual_departure=actual_dep,
        )
        duration = _compute_duration(leg)
        assert duration == pytest.approx(4 * 3600.0)

    async def test_none_departure_returns_none(self) -> None:
        leg = make_leg_data(departure=None)
        assert _compute_duration(leg) is None

    async def test_none_arrival_returns_none(self) -> None:
        leg = make_leg_data(arrival=None)
        assert _compute_duration(leg) is None

    async def test_zero_duration_returns_none(self) -> None:
        leg = make_leg_data(departure=T0, arrival=T0)
        assert _compute_duration(leg) is None

    async def test_negative_duration_returns_none(self) -> None:
        leg = make_leg_data(departure=T0, arrival=T0 - timedelta(hours=1))
        assert _compute_duration(leg) is None


# ===========================================================================
# 6. ROAD leg interpolation (mocked adapter)
# ===========================================================================


class TestRoadLegInterpolation:
    async def test_road_leg_returns_position(self) -> None:
        """TruckSimAdapter is mocked so no real geometry computation occurs."""
        leg = make_leg_data(mode="ROAD")
        expected_pos = fake_position("1")

        mock_adapter = AsyncMock()
        mock_adapter.get_current_positions = AsyncMock(return_value=[expected_pos])
        mock_adapter.start = AsyncMock()
        mock_adapter.stop = AsyncMock()
        mock_adapter.add_leg = AsyncMock()

        with patch(
            "nexafreight.adapters.feed.truck_sim.TruckSimAdapter",
            return_value=mock_adapter,
        ):
            pos = await _interpolate_leg(leg, NOW)

        assert pos is not None
        assert pos.asset_id == "1"

    async def test_road_leg_adapter_error_returns_none(self) -> None:
        """Adapter failure should return None without crashing."""
        leg = make_leg_data(mode="ROAD")

        mock_adapter = AsyncMock()
        mock_adapter.start = AsyncMock(side_effect=RuntimeError("Adapter error"))

        with patch(
            "nexafreight.adapters.feed.truck_sim.TruckSimAdapter",
            return_value=mock_adapter,
        ):
            pos = await _interpolate_leg(leg, NOW)
        assert pos is None


# ===========================================================================
# 7. AIR leg interpolation (mocked adapter)
# ===========================================================================


class TestAirLegInterpolation:
    async def test_air_leg_returns_position(self) -> None:
        """ReplayFlightAdapter is mocked so no real geodesic computation occurs."""
        leg = make_leg_data(mode="AIR", geometry=GEODESIC_ARC)
        expected_pos = AssetPosition(
            asset_id="1",
            asset_type=AssetType.AIR,
            lat=10.0,
            lon=20.0,
            speed_knots=485.0,
            heading_deg=280.0,
            reported_at=NOW,
            provenance=Provenance.SIMULATED,
            source="GEODESIC_ARC",
        )

        mock_adapter = AsyncMock()
        mock_adapter.get_current_positions = AsyncMock(return_value=[expected_pos])
        mock_adapter.start = AsyncMock()
        mock_adapter.stop = AsyncMock()
        mock_adapter.add_leg = AsyncMock()

        with patch(
            "nexafreight.adapters.feed.replay_flight.ReplayFlightAdapter",
            return_value=mock_adapter,
        ):
            pos = await _interpolate_leg(leg, NOW)

        assert pos is not None
        assert pos.asset_type == AssetType.AIR


# ===========================================================================
# 8. Database write for ROAD leg
# ===========================================================================


class TestDatabaseWrite:
    async def test_write_position_inserts_row(self, db_session, make_leg, make_shipment) -> None:
        """A valid position write should insert a PositionReport row."""
        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id, mode=TransportMode.ROAD, status=LegStatus.IN_PROGRESS
        )
        pos = fake_position(str(leg.id))

        success = await _write_position(db_session, leg.id, pos, NOW)
        assert success

        await db_session.commit()
        result = await db_session.execute(
            select(PositionReport).where(PositionReport.leg_id == leg.id)
        )
        record = result.scalar_one_or_none()
        assert record is not None
        assert record.provenance == Provenance.SIMULATED or record.provenance == "SIMULATED"
        assert record.asset_type == AssetType.ROAD.value
        assert record.latitude == pytest.approx(0.5)
        assert record.longitude == pytest.approx(0.5)

    async def test_write_position_upserts_same_timestamp(
        self, db_session, make_leg, make_shipment
    ) -> None:
        """Writing the same (leg_id, recorded_at) twice should upsert, not duplicate."""
        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id, mode=TransportMode.ROAD, status=LegStatus.IN_PROGRESS
        )
        pos = fake_position(str(leg.id))

        await _write_position(db_session, leg.id, pos, NOW)
        await _write_position(db_session, leg.id, pos, NOW)  # Same timestamp
        await db_session.commit()

        result = await db_session.execute(
            select(PositionReport).where(PositionReport.leg_id == leg.id)
        )
        records = result.scalars().all()
        matching = [r for r in records if _to_utc(r.reported_at) == NOW]
        assert len(matching) == 1

    async def test_write_different_timestamps_creates_two_rows(
        self, db_session, make_leg, make_shipment
    ) -> None:
        """Different timestamps for the same leg should create separate rows."""
        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id, mode=TransportMode.ROAD, status=LegStatus.IN_PROGRESS
        )
        pos1 = fake_position(str(leg.id))
        t2 = NOW + timedelta(seconds=30)

        await _write_position(db_session, leg.id, pos1, NOW)
        await _write_position(db_session, leg.id, pos1, t2)
        await db_session.commit()

        result = await db_session.execute(
            select(PositionReport).where(PositionReport.leg_id == leg.id)
        )
        records = result.scalars().all()
        assert len(records) == 2


# ===========================================================================
# 9. In-memory cache update
# ===========================================================================


class TestInMemoryCache:
    async def test_cache_updated_after_job_run(self, db_session, make_leg, make_shipment) -> None:
        """After a successful job run, get_current_positions() should have the position."""
        import nexafreight.workers.position_interpolator as mod

        mod._position_cache.clear()

        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.IN_PROGRESS,
            route_geometry=SIMPLE_LINESTRING,
            planned_departure=T0,
            planned_arrival=T_ARRIVAL,
        )
        expected_pos = fake_position(str(leg.id))

        mock_truck_adapter = AsyncMock()
        mock_truck_adapter.get_current_positions = AsyncMock(return_value=[expected_pos])
        mock_truck_adapter.start = AsyncMock()
        mock_truck_adapter.stop = AsyncMock()
        mock_truck_adapter.add_leg = AsyncMock()

        with patch(
            "nexafreight.adapters.feed.truck_sim.TruckSimAdapter",
            return_value=mock_truck_adapter,
        ):
            await _run_interpolation_job(db_session)

        cache = get_current_positions()
        assert str(leg.id) in cache

    async def test_get_current_positions_returns_copy(self) -> None:
        """get_current_positions() must return a copy, not the live dict."""
        import nexafreight.workers.position_interpolator as mod

        mod._position_cache.clear()

        result1 = get_current_positions()
        result2 = get_current_positions()
        assert result1 is not result2

    async def test_get_current_positions_importable_without_side_effects(self) -> None:
        """Importing and calling get_current_positions() must have no side effects."""
        result = get_current_positions()
        assert isinstance(result, dict)


# ===========================================================================
# 10. Malformed geometry handling
# ===========================================================================


class TestMalformedGeometry:
    async def test_malformed_geometry_skips_leg(self, db_session, make_leg, make_shipment) -> None:
        """A malformed geometry should skip that leg, not crash the job."""
        import nexafreight.workers.position_interpolator as mod

        mod._position_cache.clear()

        shipment = await make_shipment()
        _bad_leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.IN_PROGRESS,
            route_geometry=INVALID_JSON,
            planned_departure=T0,
            planned_arrival=T_ARRIVAL,
        )
        good_leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.IN_PROGRESS,
            route_geometry=SIMPLE_LINESTRING,
            planned_departure=T0,
            planned_arrival=T_ARRIVAL,
        )
        good_pos = fake_position(str(good_leg.id))

        mock_adapter = AsyncMock()
        mock_adapter.get_current_positions = AsyncMock(return_value=[good_pos])
        mock_adapter.start = AsyncMock()
        mock_adapter.stop = AsyncMock()
        mock_adapter.add_leg = AsyncMock()

        with patch(
            "nexafreight.adapters.feed.truck_sim.TruckSimAdapter",
            return_value=mock_adapter,
        ):
            await _run_interpolation_job(db_session)

        cache = get_current_positions()
        assert str(_bad_leg.id) not in cache
        assert str(good_leg.id) in cache

    async def test_bad_leg_does_not_create_position_report(
        self, db_session, make_leg, make_shipment
    ) -> None:
        """No PositionReport row should be written for a leg with bad geometry."""
        shipment = await make_shipment()
        bad_leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.IN_PROGRESS,
            route_geometry=INVALID_JSON,
            planned_departure=T0,
            planned_arrival=T_ARRIVAL,
        )

        with patch(
            "nexafreight.adapters.feed.truck_sim.TruckSimAdapter",
            return_value=AsyncMock(
                start=AsyncMock(),
                stop=AsyncMock(),
                add_leg=AsyncMock(),
                get_current_positions=AsyncMock(return_value=[]),
            ),
        ):
            await _run_interpolation_job(db_session)

        result = await db_session.execute(
            select(PositionReport).where(PositionReport.leg_id == bad_leg.id)
        )
        assert result.scalar_one_or_none() is None


# ===========================================================================
# 11. Stale data cleanup
# ===========================================================================


class TestStaleDataCleanup:
    async def test_stale_rows_deleted_fresh_rows_preserved(
        self, db_session, make_leg, make_shipment
    ) -> None:
        """Rows older than 24h should be deleted; recent rows should survive."""
        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id, mode=TransportMode.ROAD, status=LegStatus.IN_PROGRESS
        )

        stale_time = NOW - timedelta(hours=25)
        recent_time = NOW - timedelta(minutes=5)

        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        await db_session.execute(
            sqlite_insert(PositionReport).values(
                leg_id=leg.id,
                asset_type="ROAD",
                latitude=1.0,
                longitude=2.0,
                speed_knots=10.0,
                heading=90.0,
                reported_at=stale_time,
                provenance="SIMULATED",
            )
        )
        await db_session.execute(
            sqlite_insert(PositionReport).values(
                leg_id=leg.id,
                asset_type="ROAD",
                latitude=1.0,
                longitude=2.0,
                speed_knots=10.0,
                heading=90.0,
                reported_at=recent_time,
                provenance="SIMULATED",
            )
        )
        await db_session.commit()

        await _cleanup_stale_positions(db_session, NOW)
        await db_session.commit()

        result = await db_session.execute(
            select(PositionReport).where(PositionReport.leg_id == leg.id)
        )
        remaining = result.scalars().all()
        timestamps = {_to_utc(r.reported_at) for r in remaining}
        assert stale_time not in timestamps
        assert recent_time in timestamps

    async def test_cleanup_failure_does_not_crash(self) -> None:
        """Cleanup failure should be logged and swallowed."""
        broken_session = AsyncMock()
        broken_session.begin_nested = MagicMock(side_effect=RuntimeError("savepoint error"))
        await _cleanup_stale_positions(broken_session, NOW)


# ===========================================================================
# 12. Database write failure resilience
# ===========================================================================


class TestWriteFailureResilience:
    async def test_job_continues_after_write_failure(self) -> None:
        """If _write_position fails for one leg, the job should continue and not crash."""
        import nexafreight.workers.position_interpolator as mod

        mod._position_cache.clear()

        leg1 = make_leg_data(leg_id=1, mode="ROAD")
        leg2 = make_leg_data(leg_id=2, mode="ROAD")
        pos1 = fake_position("1")
        pos2 = fake_position("2")

        call_count = 0

        async def mock_write(session, leg_id, position, recorded_at):
            nonlocal call_count
            call_count += 1
            if leg_id == 1:
                raise RuntimeError("Write error for leg 1")
            return True

        with patch(
            "nexafreight.workers.position_interpolator._query_active_legs",
            return_value=[leg1, leg2],
        ):
            with patch(
                "nexafreight.workers.position_interpolator._interpolate_leg",
                side_effect=[pos1, pos2],
            ):
                with patch(
                    "nexafreight.workers.position_interpolator._write_position",
                    side_effect=mock_write,
                ):
                    with patch(
                        "nexafreight.workers.position_interpolator._cleanup_stale_positions"
                    ):
                        session_mock = AsyncMock()
                        session_mock.commit = AsyncMock()
                        with patch(
                            "nexafreight.workers.position_interpolator.get_session_factory"
                        ) as mock_factory:
                            mock_ctx = MagicMock()
                            mock_ctx.__aenter__ = AsyncMock(return_value=session_mock)
                            mock_ctx.__aexit__ = AsyncMock(return_value=False)
                            mock_factory.return_value = MagicMock(return_value=mock_ctx)
                            await _run_interpolation_job()

        assert call_count == 2

    async def test_leg_query_failure_does_not_crash_job(self) -> None:
        """If _query_active_legs returns empty due to error, job should complete."""
        with patch(
            "nexafreight.workers.position_interpolator._query_active_legs",
            return_value=[],
        ):
            with patch("nexafreight.workers.position_interpolator._cleanup_stale_positions"):
                session_mock = AsyncMock()
                session_mock.commit = AsyncMock()
                with patch(
                    "nexafreight.workers.position_interpolator.get_session_factory"
                ) as mock_factory:
                    mock_ctx = MagicMock()
                    mock_ctx.__aenter__ = AsyncMock(return_value=session_mock)
                    mock_ctx.__aexit__ = AsyncMock(return_value=False)
                    mock_factory.return_value = MagicMock(return_value=mock_ctx)
                    await _run_interpolation_job()


# ===========================================================================
# 13. Option A adapter design verification
# ===========================================================================


class TestOptionAAdapterDesign:
    async def test_removed_leg_does_not_produce_position_in_next_run(
        self, db_session, make_leg, make_shipment
    ) -> None:
        """
        Option A: adapters are fresh per run. A leg removed from the database
        between runs must not produce a position in the subsequent run.
        """
        import nexafreight.workers.position_interpolator as mod

        mod._position_cache.clear()

        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.IN_PROGRESS,
            route_geometry=SIMPLE_LINESTRING,
            planned_departure=T0,
            planned_arrival=T_ARRIVAL,
        )
        pos = fake_position(str(leg.id))

        mock_adapter = AsyncMock()
        mock_adapter.get_current_positions = AsyncMock(return_value=[pos])
        mock_adapter.start = AsyncMock()
        mock_adapter.stop = AsyncMock()
        mock_adapter.add_leg = AsyncMock()

        # First run: leg is IN_PROGRESS.
        with patch(
            "nexafreight.adapters.feed.truck_sim.TruckSimAdapter",
            return_value=mock_adapter,
        ):
            await _run_interpolation_job(db_session)
        assert str(leg.id) in get_current_positions()

        # Remove the leg from the database (mark as COMPLETED).
        await db_session.execute(
            update(Leg).where(Leg.id == leg.id).values(status=LegStatus.COMPLETED)
        )
        await db_session.commit()

        # Clear cache to verify second run doesn't re-populate from old adapter state.
        mod._position_cache.clear()

        # Second run: the leg is now COMPLETED, should not be queried.
        with patch(
            "nexafreight.adapters.feed.truck_sim.TruckSimAdapter",
            return_value=mock_adapter,
        ):
            await _run_interpolation_job(db_session)

        cache = get_current_positions()
        assert str(leg.id) not in cache


# ===========================================================================
# 14. Lifespan integration (lightweight)
# ===========================================================================


class TestLifespanIntegration:
    async def test_app_starts_cleanly_with_interpolator_disabled(self, test_app) -> None:
        """The app should start and stop cleanly."""
        assert test_app is not None

    async def test_get_current_positions_callable_without_app_start(self) -> None:
        """get_current_positions() should be callable before the app starts."""
        result = get_current_positions()
        assert isinstance(result, dict)
