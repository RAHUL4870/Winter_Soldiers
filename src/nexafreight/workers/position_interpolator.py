"""
Position Interpolator Worker (T-030).

Runs every 30 seconds via APScheduler to simulate truck and flight positions
by interpolating along pre-computed route geometries.

Architecture
────────────
This worker bridges the simulation adapters (TruckSimAdapter,
ReplayFlightAdapter from T-028) and the persistence layer (PositionReport ORM
from T-007), making simulated positions continuously available to the system.

It runs independently of and alongside the AIS Listener Worker (T-029), which
handles vessel positions. Together they ensure ALL active leg types have
current positions.

Adapter instantiation: Option A (fresh per run)
────────────────────────────────────────────────
A new TruckSimAdapter and ReplayFlightAdapter are created on every 30-second
job run. This guarantees:
  - No stale state from legs removed between runs.
  - Clean isolation between runs.
  - No complex add/remove synchronization logic.
  - Negligible overhead for a 30-second interval.

APScheduler configuration
─────────────────────────
  trigger:         interval (every 30 seconds)
  max_instances:   1  — only one run at a time
  coalesce:        True — if a run is still executing when the next tick fires,
                   the missed tick is absorbed and a single run starts after
                   the current one completes (no backlog accumulates)
  misfire_grace_time: 10 — a run started within 10s of its scheduled time is
                   not considered a misfire; runs delayed beyond 10s are skipped
                   and a DEBUG message is logged

Provenance policy
─────────────────
All positions from this worker carry Provenance.SIMULATED — they are
interpolated, not observed.

Feature toggle
──────────────
enable_position_interpolator=False: the worker does not start, no APScheduler
instance is created, get_current_positions() returns an empty dict.

Database write strategy
───────────────────────
Per-run: a single SQLAlchemy session is opened. Each leg's position write is
wrapped in a SAVEPOINT so a failure for one leg does not prevent the others.
recorded_at is the UTC wall-clock time at the START of the job run, shared
across all legs in that run. This means (leg_id, recorded_at) is unique per
run, so in practice the upsert is always an INSERT — but ON CONFLICT DO UPDATE
is retained for correctness.

Stale data cleanup
──────────────────
After writing new positions, delete all PositionReport rows with recorded_at
older than 24 hours. Single DELETE statement, logged at DEBUG.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.adapters.protocols import AssetPosition, Provenance
from nexafreight.config import get_settings
from nexafreight.database import get_session_factory
from nexafreight.enums import LegStatus, TransportMode
from nexafreight.models.leg import Leg
from nexafreight.models.position import PositionReport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default job interval in seconds.
_JOB_INTERVAL_S: int = 30

#: APScheduler job ID — used to check registration and avoid duplicates.
_JOB_ID: str = "position_interpolator"

#: Stale data cutoff.
_STALE_HOURS: int = 24

#: Leg modes processed by this worker.
_HANDLED_MODES: frozenset[str] = frozenset({"ROAD", "AIR"})

# ---------------------------------------------------------------------------
# In-memory position cache
# ---------------------------------------------------------------------------

#: Keyed by str(leg_id) → latest AssetPosition for that leg.
#: Updated after every successful job run.
#: Exposed read-only via get_current_positions().
_position_cache: dict[str, AssetPosition] = {}


def get_current_positions() -> dict[str, AssetPosition]:
    """
    Return a copy of the current in-memory position cache.

    Safe to call from any async context, route handler, or SSE endpoint
    without acquiring any lock or hitting the database.

    Returns an empty dict before the first job run or when the feature
    toggle is disabled.
    """
    return dict(_position_cache)


# ---------------------------------------------------------------------------
# Leg data container (internal)
# ---------------------------------------------------------------------------


class _LegData:
    """
    Lightweight container for leg fields extracted from a database row.

    Avoids holding an ORM-tracked object after the session closes.
    """

    __slots__ = (
        "actual_departure",
        "leg_id",
        "mode",
        "planned_arrival",
        "planned_departure",
        "route_geometry",
    )

    def __init__(
        self,
        leg_id: int,
        mode: str,
        route_geometry: str | None,
        planned_departure: datetime | None,
        planned_arrival: datetime | None,
        actual_departure: datetime | None,
    ) -> None:
        self.leg_id = leg_id
        self.mode = mode
        self.route_geometry = route_geometry
        self.planned_departure = planned_departure
        self.planned_arrival = planned_arrival
        self.actual_departure = actual_departure


# ---------------------------------------------------------------------------
# Leg query
# ---------------------------------------------------------------------------


async def _query_active_legs(session: AsyncSession) -> list[_LegData]:
    """
    Query all IN_PROGRESS ROAD and AIR legs from the database.

    Returns an empty list on any database error (logs warning).
    Does not hold the ORM objects after the session closes — extracts
    all needed fields into _LegData immediately.

    Parameters
    ----------
    session : AsyncSession
        An open async SQLAlchemy session.

    Returns
    -------
    list[_LegData]
        Legs ready for interpolation, or empty list on failure.
    """
    try:
        stmt = (
            select(
                Leg.id,
                Leg.transport_mode,
                Leg.route_geometry_json,
                Leg.planned_departure,
                Leg.planned_arrival,
                Leg.actual_departure,
            )
            .where((Leg.status == LegStatus.IN_PROGRESS) | (Leg.status == "IN_PROGRESS"))
            .where(Leg.transport_mode.in_([TransportMode.ROAD, TransportMode.AIR, "ROAD", "AIR"]))
        )
        result = await session.execute(stmt)
        rows = result.all()

        legs = [
            _LegData(
                leg_id=row.id,
                mode=row.transport_mode.value
                if hasattr(row.transport_mode, "value")
                else str(row.transport_mode),
                route_geometry=row.route_geometry_json,
                planned_departure=row.planned_departure,
                planned_arrival=row.planned_arrival,
                actual_departure=row.actual_departure,
            )
            for row in rows
        ]
        logger.debug("Queried %d active IN_PROGRESS ROAD/AIR leg(s).", len(legs))
        return legs

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to query active legs from database: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Geometry validation
# ---------------------------------------------------------------------------


def _parse_geometry(leg_id: int, raw: str | None) -> str | None:
    """
    Validate that route_geometry is a non-empty, parseable GeoJSON string.

    Returns the raw string if valid, None if not (logging a warning).
    We do not transform the geometry — the adapters accept the raw GeoJSON.
    """
    if not raw:
        logger.warning("Leg %d has no route_geometry; skipping interpolation.", leg_id)
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Leg %d has malformed route_geometry (JSON decode error: %s); "
            "skipping interpolation.",
            leg_id,
            exc,
        )
        return None

    geo_type = parsed.get("type") if isinstance(parsed, dict) else None
    if geo_type not in {
        "LineString",
        "FeatureCollection",
        "Feature",
        "MultiLineString",
    }:
        logger.warning(
            "Leg %d has unexpected geometry type %r; skipping interpolation.",
            leg_id,
            geo_type,
        )
        return None

    return raw


# ---------------------------------------------------------------------------
# Duration computation
# ---------------------------------------------------------------------------


def _compute_duration(leg: _LegData) -> float | None:
    """
    Compute planned duration in seconds.

    Uses actual_departure as the reference start time when available,
    otherwise falls back to planned_departure.  Returns None if neither
    is available (leg will be skipped).
    """
    departure = leg.actual_departure or leg.planned_departure
    arrival = leg.planned_arrival

    if departure is None or arrival is None:
        logger.warning("Leg %d has no departure or arrival time; skipping.", leg.leg_id)
        return None

    # Ensure timezone-aware
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=UTC)
    if arrival.tzinfo is None:
        arrival = arrival.replace(tzinfo=UTC)

    duration_s = (arrival - departure).total_seconds()
    if duration_s <= 0:
        logger.warning(
            "Leg %d has non-positive planned duration (%.1f s); skipping.",
            leg.leg_id,
            duration_s,
        )
        return None

    return duration_s


# ---------------------------------------------------------------------------
# Interpolation for a single leg
# ---------------------------------------------------------------------------


async def _interpolate_leg(
    leg: _LegData,
    now: datetime,
) -> AssetPosition | None:
    """
    Compute the current simulated position for one leg using the appropriate
    adapter (TruckSimAdapter for ROAD, ReplayFlightAdapter for AIR).

    Adapters are instantiated fresh per call (Option A design).

    Returns
    -------
    AssetPosition | None
        The interpolated position, or None if interpolation fails.
    """
    geometry = _parse_geometry(leg.leg_id, leg.route_geometry)
    if geometry is None:
        return None

    duration_s = _compute_duration(leg)
    if duration_s is None:
        return None

    departure = leg.actual_departure or leg.planned_departure
    if departure is None:
        return None
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=UTC)

    leg_id_str = str(leg.leg_id)

    try:
        if leg.mode == "ROAD":
            # Deferred import: avoids circular imports and allows test injection.
            from nexafreight.adapters.feed.truck_sim import (  # noqa: PLC0415
                TruckSimAdapter,
            )

            road_adapter = TruckSimAdapter(now_fn=lambda: now)
            await road_adapter.start()
            await road_adapter.add_leg(
                leg_id=leg_id_str,
                geometry_geojson=geometry,
                departure=departure,
                planned_duration_s=duration_s,
            )
            positions = await road_adapter.get_current_positions()
            await road_adapter.stop()

        elif leg.mode == "AIR":
            from nexafreight.adapters.feed.replay_flight import (  # noqa: PLC0415
                ReplayFlightAdapter,
            )

            flight_adapter = ReplayFlightAdapter(now_fn=lambda: now)
            await flight_adapter.start()
            await flight_adapter.add_leg(
                leg_id=leg_id_str,
                geometry_geojson=geometry,
                departure=departure,
                planned_duration_s=duration_s,
            )
            positions = await flight_adapter.get_current_positions()
            await flight_adapter.stop()

        else:
            logger.warning("Unexpected leg mode %r for leg %d.", leg.mode, leg.leg_id)
            return None

    except Exception as exc:  # noqa: BLE001
        logger.warning("Adapter error for leg %d (mode=%s): %s", leg.leg_id, leg.mode, exc)
        return None

    # The adapter returns positions keyed by leg_id_str; find ours.
    for pos in positions:
        if pos.asset_id == leg_id_str:
            return pos

    logger.debug(
        "Adapter returned no position for leg %d; leg may be before departure or after arrival.",
        leg.leg_id,
    )
    return None


# ---------------------------------------------------------------------------
# Database write (per-leg, within a shared session using savepoints)
# ---------------------------------------------------------------------------


async def _write_position(
    session: AsyncSession,
    leg_id: int,
    position: AssetPosition,
    recorded_at: datetime,
) -> bool:
    """
    Upsert a PositionReport row for one leg within an open session.

    Uses a SAVEPOINT so a failure here does not invalidate the parent
    transaction — other legs in the same run can still succeed.

    Parameters
    ----------
    session : AsyncSession
        The parent session (already inside a transaction).
    leg_id : int
        Primary key of the leg being written.
    position : AssetPosition
        The interpolated position DTO.
    recorded_at : datetime
        The UTC timestamp for this job run (same for all legs in the run).

    Returns
    -------
    bool
        True if the write succeeded, False on failure.
    """
    try:
        async with session.begin_nested():  # SAVEPOINT
            asset_type_val = (
                position.asset_type.value
                if hasattr(position.asset_type, "value")
                else str(position.asset_type)
            )
            provenance_val = (
                Provenance.SIMULATED.value
                if hasattr(Provenance.SIMULATED, "value")
                else str(Provenance.SIMULATED)
            )
            stmt = (
                sqlite_insert(PositionReport)
                .values(
                    leg_id=leg_id,
                    asset_type=asset_type_val,
                    latitude=position.lat,
                    longitude=position.lon,
                    speed_knots=position.speed_knots,
                    heading=position.heading_deg,
                    reported_at=recorded_at,
                    provenance=provenance_val,
                )
                .on_conflict_do_update(
                    index_elements=["leg_id", "reported_at"],
                    set_={
                        "asset_type": asset_type_val,
                        "latitude": position.lat,
                        "longitude": position.lon,
                        "speed_knots": position.speed_knots,
                        "heading": position.heading_deg,
                        "provenance": provenance_val,
                    },
                )
            )
            await session.execute(stmt)
        return True

    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write PositionReport for leg %d: %s", leg_id, exc)
        return False


# ---------------------------------------------------------------------------
# Stale data cleanup
# ---------------------------------------------------------------------------


async def _cleanup_stale_positions(session: AsyncSession, now: datetime) -> None:
    """
    Delete PositionReport rows older than 24 hours.

    Single DELETE statement; does not iterate rows.
    Failures are logged and swallowed — cleanup is best-effort.
    """
    cutoff = now - timedelta(hours=_STALE_HOURS)
    try:
        async with session.begin_nested():  # SAVEPOINT for isolation
            stmt = delete(PositionReport).where(PositionReport.reported_at < cutoff)
            result = await session.execute(stmt)
            deleted = result.rowcount
            if deleted > 0:
                logger.debug(
                    "Cleaned up %d stale PositionReport row(s) older than %dh.",
                    deleted,
                    _STALE_HOURS,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to clean up stale position reports: %s", exc)


# ---------------------------------------------------------------------------
# Core job function
# ---------------------------------------------------------------------------


async def _run_interpolation_job(session: AsyncSession | None = None) -> None:
    """
    Core async job function: query legs, interpolate, write, clean up.

    Called by APScheduler every 30 seconds.

    All exceptions are caught at the top level so APScheduler is never
    disrupted by a single failed run. Each failed run logs at ERROR level
    with a full traceback.
    """
    now = datetime.now(UTC)
    logger.debug("Position interpolator job starting at %s.", now.isoformat())

    positions_written = 0
    legs_processed = 0

    async def _execute(sess: AsyncSession) -> None:
        nonlocal positions_written, legs_processed
        # ── Step 1: Query active legs ──────────────────────────────────
        legs = await _query_active_legs(sess)

        if not legs:
            logger.debug("No active ROAD/AIR legs found; skipping job run.")
            await _cleanup_stale_positions(sess, now)
            await sess.commit()
            return

        # ── Step 2: Interpolate positions for all legs ─────────────────
        computed: list[tuple[int, AssetPosition]] = []

        for leg in legs:
            legs_processed += 1
            try:
                position = await _interpolate_leg(leg, now)
                if position is not None:
                    computed.append((leg.leg_id, position))
            except Exception as leg_exc:
                logger.warning("Error interpolating leg %d: %s", leg.leg_id, leg_exc)

        # ── Step 3: Batch write all computed positions ──────────────────
        for leg_id, position in computed:
            try:
                success = await _write_position(sess, leg_id, position, now)
                if success:
                    positions_written += 1
                    # Update in-memory cache immediately after write.
                    _position_cache[str(leg_id)] = position
                    logger.debug(
                        "Wrote and cached position for leg %d (lat=%.4f, lon=%.4f, mode=%s).",
                        leg_id,
                        position.lat,
                        position.lon,
                        position.asset_type.value
                        if hasattr(position.asset_type, "value")
                        else str(position.asset_type),
                    )
            except Exception as write_exc:
                logger.warning("Error writing position for leg %d: %s", leg_id, write_exc)

        # ── Step 4: Stale data cleanup ─────────────────────────────────
        await _cleanup_stale_positions(sess, now)

        # Commit the parent transaction covering all successful writes.
        await sess.commit()

    try:
        if session is not None:
            await _execute(session)
        else:
            session_factory = get_session_factory()
            async with session_factory() as sess:
                await _execute(sess)

        logger.debug(
            "Position interpolator job complete: %d leg(s) processed, %d position(s) written.",
            legs_processed,
            positions_written,
        )

        if positions_written > 0:
            logger.info(
                "Position interpolator: wrote %d simulated position(s) for %d leg(s).",
                positions_written,
                legs_processed,
            )

    except asyncio.CancelledError:
        logger.debug("Position interpolator job cancelled (shutdown signal).")
        raise

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Unexpected error in position interpolator job: %s",
            exc,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Worker class
# ---------------------------------------------------------------------------


class PositionInterpolatorWorker:
    """
    Manages the APScheduler lifecycle for the position interpolator job.

    Lifecycle:
    - start(): creates and starts AsyncIOScheduler, registers 30s job.
    - stop(): gracefully shuts down the scheduler.

    Idempotency:
    - start() twice: second call is a no-op if already running.
    - stop() before start(): safe no-op.
    - stop() twice: safe no-op.

    APScheduler job configuration:
    - max_instances=1: only one concurrent run.
    - coalesce=True: missed runs are absorbed, not queued.
    - misfire_grace_time=10: runs >10s late are considered misfired/skipped.
    """

    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None
        self._started: bool = False

    async def start(self, interval_seconds: int = _JOB_INTERVAL_S) -> None:
        """
        Initialize and start the APScheduler with the interpolator job.

        Idempotent: a second call while running is a safe no-op.

        Parameters
        ----------
        interval_seconds : int
            Job interval in seconds. Default 30. Overridable for tests.
        """
        if self._started:
            logger.debug("PositionInterpolatorWorker.start() called while already running — no-op.")
            return

        settings = get_settings()
        if not settings.enable_position_interpolator:
            logger.info(
                "Position interpolator is disabled via settings "
                "(enable_position_interpolator=False)."
            )
            return

        logger.info("Position interpolator worker starting (interval=%ds).", interval_seconds)

        self._scheduler = AsyncIOScheduler()
        self._scheduler.add_job(
            _run_interpolation_job,
            trigger="interval",
            seconds=interval_seconds,
            id=_JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=10,
        )
        self._scheduler.start()
        self._started = True

        logger.info("Position interpolator worker started; job '%s' registered.", _JOB_ID)

    async def stop(self) -> None:
        """
        Gracefully stop the APScheduler.

        Idempotent: safe before start() and safe to call repeatedly.
        """
        if not self._started or self._scheduler is None:
            logger.debug("PositionInterpolatorWorker.stop() called while not running — no-op.")
            return

        logger.info("Position interpolator worker stopping.")

        try:
            self._scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error during APScheduler shutdown: %s", exc)

        self._scheduler = None
        self._started = False
        logger.info("Position interpolator worker stopped.")

    @property
    def is_running(self) -> bool:
        """True if the scheduler is currently active."""
        return self._started and self._scheduler is not None


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

#: Global worker instance, managed by the FastAPI lifespan.
_interpolator_worker: PositionInterpolatorWorker | None = None


def get_interpolator_worker() -> PositionInterpolatorWorker:
    """
    Retrieve the singleton PositionInterpolatorWorker.

    Safe to import and call from any module without side effects.
    The worker is started during lifespan startup.
    """
    global _interpolator_worker
    if _interpolator_worker is None:
        _interpolator_worker = PositionInterpolatorWorker()
    return _interpolator_worker
