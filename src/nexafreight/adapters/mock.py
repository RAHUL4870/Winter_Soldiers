"""Deterministic, fully synthetic position feed adapter for tests and local dev.

MockFeedAdapter produces identical position sequences given identical
seed_assets and reference_time, driven entirely by an explicit simulated
clock advanced via advance_simulated_time(). It performs no I/O, no
real time.sleep(), and no calls to datetime.now() in its core logic.
"""

from __future__ import annotations

import math
import zlib
from datetime import datetime, timedelta

from nexafreight.adapters.protocols import AssetPosition, FeedHealth
from nexafreight.enums import Provenance, TransportMode

# Deterministic circular-motion parameters. These values are arbitrary and
# chosen purely to produce smooth, bounded, non-degenerate synthetic
# movement — they carry no real-world geographic or kinematic meaning.
_RADIUS_DEG = 0.01  # ~1.1 km at the equator
_ANGULAR_VELOCITY_RAD_PER_SEC = 2 * math.pi / 3600  # one full revolution per simulated hour
_SPEED_SCALE_FACTOR = 100_000.0  # arbitrary scaling to produce plausible-looking knots values


def _phase_for_asset(asset_id: str) -> float:
    """Deterministic per-asset phase offset (radians), stable across processes.

    Uses zlib.crc32 rather than Python's built-in hash() because str hashing
    is randomized per-process by default (PYTHONHASHSEED) — that randomness
    would silently break cross-process/cross-run determinism.
    """
    return (zlib.crc32(asset_id.encode("utf-8")) % 360) * (math.pi / 180)


class MockFeedAdapter:
    """Deterministic, fully synthetic position feed for tests and local dev.

    Given the same seed_assets and reference_time, produces identical
    position sequences across runs and across separate instances — no real
    time, no randomness, no I/O.
    """

    def __init__(
        self,
        seed_assets: list[tuple[str, TransportMode, float, float]],
        reference_time: datetime,
        force_unhealthy: bool = False,
    ) -> None:
        """Construct a mock feed with a fixed set of assets and a start clock.

        Args:
            seed_assets: List of (asset_id, transport_mode, start_lat, start_lon)
                tuples defining each tracked asset's identity and starting point.
            reference_time: The simulated clock's initial value (t=0 for movement).
            force_unhealthy: If True, get_health() always reports unhealthy,
                regardless of started state — used to test failure-handling
                paths in calling code.
        """
        self._seed_assets = list(seed_assets)
        self._reference_time = reference_time
        self._force_unhealthy = force_unhealthy
        self._current_time = reference_time
        self._started = False
        self._messages_received = 0

    async def start(self) -> None:
        """Begin the mock feed. Calling this twice is a safe no-op.

        Does not reset simulated time or message counters if already started.
        """
        if self._started:
            return
        self._started = True

    async def stop(self) -> None:
        """Stop the mock feed. Safe to call even if never started."""
        self._started = False

    async def get_current_positions(self) -> list[AssetPosition]:
        """Return current positions for all configured assets.

        Returns an empty list if the adapter has not been started —
        this is documented behavior, not an error condition.
        """
        if not self._started:
            return []

        elapsed_seconds = (self._current_time - self._reference_time).total_seconds()
        return [
            self._compute_position(asset_id, mode, start_lat, start_lon, elapsed_seconds)
            for asset_id, mode, start_lat, start_lon in self._seed_assets
        ]

    async def get_health(self) -> FeedHealth:
        """Report current health status.

        is_healthy is False if never started, or if force_unhealthy was set
        at construction; True otherwise. last_success_at reflects the
        adapter's simulated clock, never real wall-clock time.
        """
        is_healthy = self._started and not self._force_unhealthy
        return FeedHealth(
            adapter_name="MockFeedAdapter",
            is_healthy=is_healthy,
            last_success_at=self._current_time if self._started else None,
            messages_received=self._messages_received,
        )

    async def health(self) -> FeedHealth:
        """Protocol-compliant alias for get_health."""
        return await self.get_health()

    def advance_simulated_time(self, delta: timedelta) -> None:
        """Test-only hook: advance the adapter's internal simulated clock.

        Deterministically moves all assets forward without any real sleep.
        Increments messages_received by one "message" per tracked asset,
        modeling one position report per asset per advance round.

        Args:
            delta: Amount of simulated time to advance (must be positive
                for recorded_at timestamps to strictly increase).
        """
        self._current_time += delta
        self._messages_received += len(self._seed_assets)

    def _compute_position(
        self,
        asset_id: str,
        mode: TransportMode,
        start_lat: float,
        start_lon: float,
        elapsed_seconds: float,
    ) -> AssetPosition:
        """Compute a deterministic position for one asset at a given elapsed time.

        Movement follows a small circular path around (start_lat, start_lon),
        parameterized purely by elapsed_seconds and a per-asset phase offset.
        This makes position a pure function of (asset config, elapsed time),
        guaranteeing determinism regardless of how time was advanced to reach
        that elapsed value.
        """
        phase = _phase_for_asset(asset_id)
        angle = _ANGULAR_VELOCITY_RAD_PER_SEC * elapsed_seconds + phase

        delta_lat = _RADIUS_DEG * math.sin(angle)
        delta_lon = _RADIUS_DEG * math.cos(angle)

        new_lat = max(-90.0, min(90.0, start_lat + delta_lat))
        new_lon = max(-180.0, min(180.0, start_lon + delta_lon))

        # Velocity components: derivative of position with respect to time
        dlat_dt = _RADIUS_DEG * _ANGULAR_VELOCITY_RAD_PER_SEC * math.cos(angle)
        dlon_dt = -_RADIUS_DEG * _ANGULAR_VELOCITY_RAD_PER_SEC * math.sin(angle)

        heading_deg = math.degrees(math.atan2(dlon_dt, dlat_dt)) % 360
        speed_magnitude = math.sqrt(dlat_dt**2 + dlon_dt**2)
        speed_knots = speed_magnitude * _SPEED_SCALE_FACTOR

        return AssetPosition(
            asset_id=asset_id,
            asset_type=mode,
            latitude=new_lat,
            longitude=new_lon,
            heading_deg=heading_deg,
            speed_knots=speed_knots,
            recorded_at=self._current_time,
            provenance=Provenance.MOCK,
        )
