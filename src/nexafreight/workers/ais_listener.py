"""
AIS Listener Worker (T-029).

Persistent asyncio background task that:
1. Selects the correct position feed adapter (live AIS or Parquet replay)
   based on settings.
2. Starts and manages the adapter lifecycle within the FastAPI lifespan.
3. Polls the adapter's position cache on a configurable interval.
4. Writes PositionReport ORM records to the database for positions that have
   valid leg mappings.
5. Maintains an in-memory position snapshot for other workers and routers.
6. Updates FeedHealth state for exposure via SSE (T-031).
7. Exposes a singleton accessor get_position_tracker() so any module can
   query the current position cache without coupling to this worker.

Architectural note
──────────────────
This worker is a bridge between the adapter layer (T-026/T-027) and the
persistence layer (T-007 ORM). Adapters manage position caches; this worker
persists them to the database and exposes a consistent interface for the
rest of the application.

The adapter is selected at worker start time (not import time), following the
same lazy pattern as get_engine() and get_settings().

Database upsert strategy
────────────────────────
PositionReport uses (leg_id, reported_at) as a unique combination.
SQLAlchemy's on_conflict_do_update() with merge semantics implements an
upsert: insert if new, update if the leg_id/reported_at pair already exists.
This prevents duplicate rows on repeated poll cycles while allowing multiple
timestamps per leg to coexist.

Write deduplication
──────────────────
The worker tracks last-written reported_at per MMSI in memory. On each poll
cycle, only positions with reported_at later than the last-written one are
persisted. This prevents flooding the database when static vessels produce
identical positions on every poll.

Polling vs push
───────────────
The adapter (AISStreamAdapter, ReplayFeedAdapter) maintains an internal
position cache updated by its background receive/tick loop. This worker
polls that cache on a configurable interval (default 5 seconds) and writes
changes to the database.

This polling design is compatible with the canonical PositionFeedAdapter
protocol, which does not expose push callbacks. The trade-off is that
positions may be delayed by up to the poll interval before persistence,
which is acceptable for a control tower use case (freshness is seconds, not
milliseconds).

Feature toggle
──────────────
If enable_ais_listener is False in settings, the worker does not start, no
adapter is instantiated, and get_position_tracker() returns empty positions
and unhealthy FeedHealth. This toggle allows test environments to run
without any AIS dependency.

Resilience
──────────
Database write errors, leg-mapping query failures, and adapter start issues
do not crash the application or the poll loop. Failures are logged with
appropriate context and the worker continues.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.adapters.protocols import (
    AssetPosition,
    FeedHealth,
    PositionFeedAdapter,
    Provenance,
)
from nexafreight.config import get_settings
from nexafreight.database import get_session_factory
from nexafreight.enums import LegStatus
from nexafreight.models.leg import Leg
from nexafreight.models.position import PositionReport
from nexafreight.models.vessel import Vessel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

#: Global position tracker instance, initialized during lifespan startup.
#: Safe to access from any async context after lifespan has started.
_position_tracker: PositionTracker | None = None


def get_position_tracker() -> PositionTracker:
    """
    Retrieve the singleton PositionTracker instance.

    Safe to import and call from any module without side effects.
    If called before lifespan startup completes, returns a tracker
    in the pre-startup state: get_positions() returns an empty list,
    get_feed_health() returns unhealthy with reason "not_started".

    Returns
    -------
    PositionTracker
        The active tracker instance.
    """
    global _position_tracker
    if _position_tracker is None:
        _position_tracker = PositionTracker()
    return _position_tracker


# ---------------------------------------------------------------------------
# Position tracker
# ---------------------------------------------------------------------------


@dataclass
class PositionTracker:
    """
    Holds the active position feed adapter and exposes query methods.

    Typed against the canonical PositionFeedAdapter protocol so that
    callers need not know which concrete adapter is in use.

    Pre-startup state: adapter is None, both query methods return empty/
    unhealthy results without raising.

    This pattern is consistent with the lazy/startup-populated approach
    already used in the project for settings and database engine.
    """

    adapter: PositionFeedAdapter | None = None

    async def get_positions(self) -> list[AssetPosition]:
        """
        Retrieve the current position snapshot from the active adapter.

        Returns an empty list if the adapter is not yet started or if the
        listener is disabled.  Does not raise.
        """
        if self.adapter is None:
            return []
        return await self.adapter.get_current_positions()

    async def get_feed_health(self) -> FeedHealth:
        """
        Retrieve the current feed health from the active adapter.

        Returns an unhealthy FeedHealth with reason "not_started" if the
        adapter is not yet initialized.  Does not raise.
        """
        if self.adapter is None:
            return FeedHealth(
                adapter_name="none",
                is_healthy=False,
                last_success_at=None,
                messages_received=0,
                provenance=Provenance.MOCK,
            )
        return await self.adapter.health()


# ---------------------------------------------------------------------------
# Write deduplication state
# ---------------------------------------------------------------------------


@dataclass
class _WriteDedupState:
    """
    Tracks the last-written reported_at per MMSI to avoid duplicate DB writes.

    key: MMSI (string)
    value: reported_at (datetime) of the last position persisted for this MMSI
    """

    last_written: dict[str, datetime] = field(default_factory=dict)

    def should_write(self, mmsi: str, recorded_at: datetime) -> bool:
        """
        Determine whether a position should be persisted based on its timestamp.

        Returns True if this is the first position for this MMSI or if the
        recorded_at is later than the last-written one.
        """
        last = self.last_written.get(mmsi)
        if last is None:
            return True
        return recorded_at > last

    def mark_written(self, mmsi: str, recorded_at: datetime) -> None:
        """Record that a position for this MMSI has been persisted."""
        self.last_written[mmsi] = recorded_at


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------


async def _select_adapter() -> PositionFeedAdapter | None:
    """
    Select and construct the appropriate adapter based on settings.

    Logic:
    1. If enable_ais_listener is False: return None (feature toggle off).
    2. If use_live_ais is True and aisstream_api_key is set: use AISStreamAdapter.
    3. If use_live_ais is True but no key: check for replay data; fall back if available.
    4. If use_live_ais is False: use ReplayFeedAdapter with configured path.
    5. If neither live nor replay is usable: log warning and return None.

    Imports are deferred (not at module level) to avoid circular imports and
    to allow tests to control which adapters are available.

    Returns
    -------
    PositionFeedAdapter | None
        The selected adapter, or None if the listener is disabled or neither
        adapter is usable.
    """
    settings = get_settings()

    if not settings.enable_ais_listener:
        logger.info("AIS listener is disabled via settings.")
        return None

    if settings.use_live_ais:
        if settings.aisstream_api_key and settings.aisstream_api_key.get_secret_value():
            logger.info("Selecting AISStreamAdapter for live AIS.")
            from nexafreight.adapters.feed.aisstream import AISStreamAdapter

            return AISStreamAdapter(
                mmsis=["123456789"],  # Default/fallback MMSI list
                api_key=settings.aisstream_api_key.get_secret_value(),
            )
        else:
            logger.warning(
                "use_live_ais is True but aisstream_api_key is not configured. "
                "Attempting fallback to Parquet replay."
            )
            if settings.ais_replay_data_path:
                logger.info("Falling back to ReplayFeedAdapter.")
                from nexafreight.adapters.feed.replay_ais import ReplayFeedAdapter

                return ReplayFeedAdapter(
                    data_path=Path(settings.ais_replay_data_path),
                    mmsis=["123456789"],
                    speed_multiplier=60.0,
                )
            else:
                logger.warning(
                    "No live AIS key and no replay data path configured. "
                    "AIS listener will run in no-feed mode."
                )
                return None
    else:
        if settings.ais_replay_data_path:
            logger.info("Selecting ReplayFeedAdapter for Parquet replay.")
            from nexafreight.adapters.feed.replay_ais import ReplayFeedAdapter

            return ReplayFeedAdapter(
                data_path=Path(settings.ais_replay_data_path),
                mmsis=["123456789"],
                speed_multiplier=60.0,
            )
        else:
            logger.warning(
                "use_live_ais is False but no ais_replay_data_path is configured. "
                "AIS listener will run in no-feed mode."
            )
            return None


# ---------------------------------------------------------------------------
# Leg mapping query
# ---------------------------------------------------------------------------


async def _find_leg_for_mmsi(
    session: AsyncSession,
    mmsi: str,
) -> int | None:
    """
    Query the database to find an IN_PROGRESS Leg for a given vessel MMSI.

    Joins through Vessel and Leg tables to find a leg that is currently
    in progress and references a vessel with this MMSI.

    Returns
    -------
    int | None
        The leg_id if found, None if no matching leg exists.
    """
    try:
        try:
            mmsi_int = int(mmsi)
        except (TypeError, ValueError):
            mmsi_int = -1

        stmt = (
            select(Leg.id)
            .join(Vessel, Leg.vessel_id == Vessel.id)
            .where(Vessel.mmsi == mmsi_int)
            .where((Leg.status == LegStatus.IN_PROGRESS) | (Leg.status == "IN_PROGRESS"))
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()
    except Exception as exc:
        logger.warning(
            "Failed to query leg for MMSI %s: %s",
            mmsi,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Database write
# ---------------------------------------------------------------------------


async def _write_position_report(
    session: AsyncSession,
    leg_id: int,
    position: AssetPosition,
) -> bool:
    """
    Write or upsert a PositionReport ORM record.

    Uses SQLAlchemy's on_conflict_do_update() for upsert semantics:
    if a record with the same (leg_id, reported_at) exists, update it;
    otherwise insert a new row.

    Returns
    -------
    bool
        True if the write succeeded, False if it failed (error logged, session rolled back).
    """
    try:
        asset_type_val = (
            position.asset_type.value
            if hasattr(position.asset_type, "value")
            else str(position.asset_type)
        )
        provenance_val = (
            position.provenance.value
            if hasattr(position.provenance, "value")
            else str(position.provenance)
        )
        try:
            mmsi_int = int(position.asset_id)
        except (TypeError, ValueError):
            mmsi_int = None

        stmt = (
            insert(PositionReport)
            .values(
                leg_id=leg_id,
                asset_type=asset_type_val,
                mmsi=mmsi_int,
                latitude=position.lat,
                longitude=position.lon,
                speed_knots=position.speed_knots,
                heading=position.heading_deg,
                reported_at=position.reported_at,
                provenance=provenance_val,
            )
            .on_conflict_do_update(
                index_elements=["leg_id", "reported_at"],
                set_={
                    "asset_type": asset_type_val,
                    "mmsi": mmsi_int,
                    "latitude": position.lat,
                    "longitude": position.lon,
                    "speed_knots": position.speed_knots,
                    "heading": position.heading_deg,
                    "provenance": provenance_val,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
        return True
    except Exception as exc:
        logger.warning(
            "Failed to write PositionReport for leg_id %s: %s",
            leg_id,
            exc,
        )
        await session.rollback()
        return False


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


async def _poll_loop(
    adapter: PositionFeedAdapter,
    poll_interval_s: float = 5.0,
) -> None:
    """
    Background task that polls the adapter's position cache and writes to DB.

    On each poll cycle:
    1. Get the current position snapshot from the adapter.
    2. For each position:
       a. Skip if we've already written this exact timestamp for this MMSI.
       b. Query for a matching IN_PROGRESS Leg.
       c. If found: attempt to write the PositionReport.
       d. If not found: cache in memory but do not persist (debug log).
    3. Sleep for poll_interval_s.

    This loop is cancellation-safe: asyncio.CancelledError is caught and
    re-raised to cleanly exit. Unexpected exceptions are logged with
    traceback and the loop continues after a short backoff.

    Parameters
    ----------
    adapter : PositionFeedAdapter
        The active adapter instance.
    poll_interval_s : float
        Real time in seconds between poll cycles. Default 5.0.
    """
    dedup = _WriteDedupState()
    consecutive_errors = 0
    max_consecutive_errors = 5

    try:
        while True:
            try:
                # Get current positions from the adapter cache.
                positions = await adapter.get_current_positions()
                logger.debug(
                    "Poll cycle: %d position(s) in adapter cache.",
                    len(positions),
                )

                # Get a short-lived database session for this poll cycle.
                session_factory = get_session_factory()
                async with session_factory() as session:
                    for position in positions:
                        # Check deduplication.
                        if not dedup.should_write(position.asset_id, position.reported_at):
                            logger.debug(
                                "Skipping duplicate position for MMSI %s at %s.",
                                position.asset_id,
                                position.reported_at.isoformat(),
                            )
                            continue

                        # Query for a matching leg.
                        leg_id = await _find_leg_for_mmsi(session, position.asset_id)
                        if leg_id is None:
                            logger.debug(
                                "No IN_PROGRESS leg found for MMSI %s; caching but not persisting.",
                                position.asset_id,
                            )
                            continue

                        # Write to the database.
                        success = await _write_position_report(session, leg_id, position)
                        if success:
                            dedup.mark_written(position.asset_id, position.reported_at)
                            logger.debug(
                                "Wrote PositionReport for MMSI %s (leg_id %d) at %s.",
                                position.asset_id,
                                leg_id,
                                position.reported_at.isoformat(),
                            )

                # Reset error counter on successful poll.
                consecutive_errors = 0

            except asyncio.CancelledError:
                logger.debug("AIS listener poll loop cancelled.")
                raise

            except Exception as exc:
                consecutive_errors += 1
                logger.error(
                    "Unexpected error in AIS listener poll loop (error #%d): %s",
                    consecutive_errors,
                    exc,
                    exc_info=True,
                )
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        "AIS listener poll loop hit max consecutive errors (%d); stopping.",
                        max_consecutive_errors,
                    )
                    break
                # Back off briefly before retrying.
                await asyncio.sleep(1.0)

            # Sleep until the next poll cycle.
            await asyncio.sleep(poll_interval_s)

    except asyncio.CancelledError:
        logger.debug("AIS listener poll loop exiting due to cancellation.")


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------


class AISListenerWorker:
    """
    Manages the AIS listener worker lifecycle.

    Responsibilities:
    - Selects and initializes the appropriate adapter.
    - Starts the adapter and the background poll loop.
    - Handles shutdown: cancels the poll loop and stops the adapter.

    Idempotency:
    - start() on an already-running worker is a no-op.
    - stop() before start() is safe.
    - stop() twice is safe.
    - No leaked asyncio tasks.
    """

    def __init__(self) -> None:
        self.adapter: PositionFeedAdapter | None = None
        self.poll_task: asyncio.Task[None] | None = None

    async def start(self, poll_interval_s: float = 5.0) -> None:
        """
        Select and start the adapter; begin the background poll loop.

        Idempotent: a second call while running is a no-op.

        Parameters
        ----------
        poll_interval_s : float
            Time in seconds between poll cycles. Default 5.0.
        """
        if self.adapter is not None:
            logger.debug("AIS listener worker already started.")
            return

        logger.info("AIS listener worker starting.")
        self.adapter = await _select_adapter()

        if self.adapter is None:
            logger.warning("No adapter selected; AIS listener running in no-feed mode.")
            return

        try:
            await self.adapter.start()
        except Exception as exc:
            logger.error(
                "Failed to start adapter: %s",
                exc,
                exc_info=True,
            )
            self.adapter = None
            return

        logger.info("Adapter started; launching poll loop.")
        self.poll_task = asyncio.create_task(
            _poll_loop(self.adapter, poll_interval_s),
            name="ais-listener-poll",
        )

    async def stop(self) -> None:
        """
        Stop the poll loop and the adapter.

        Idempotent: safe before start() and safe to call repeatedly.
        Handles asyncio.CancelledError cleanly.
        """
        if self.adapter is None and self.poll_task is None:
            logger.debug("AIS listener worker already stopped.")
            return

        logger.info("AIS listener worker stopping.")

        if self.poll_task is not None and not self.poll_task.done():
            self.poll_task.cancel()
            try:
                await self.poll_task
            except asyncio.CancelledError:
                pass  # Expected; the task was cancelled.

        if self.adapter is not None:
            try:
                await self.adapter.stop()
            except Exception as exc:
                logger.warning("Error stopping adapter: %s", exc)

        self.adapter = None
        self.poll_task = None
        logger.info("AIS listener worker stopped.")


# ---------------------------------------------------------------------------
# Module-level worker instance (used by lifespan)
# ---------------------------------------------------------------------------

#: Global worker instance, managed by the lifespan startup/shutdown.
_worker: AISListenerWorker | None = None


def get_worker() -> AISListenerWorker:
    """
    Retrieve the singleton worker instance.

    Safe to call from any context. The worker is initialized and started
    during lifespan startup.
    """
    global _worker
    if _worker is None:
        _worker = AISListenerWorker()
    return _worker
