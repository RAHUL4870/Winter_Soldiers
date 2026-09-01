"""AISStream WebSocket position-feed adapter (T-026).

Connects to wss://stream.aisstream.io/v0/stream, subscribes to a
configured list of vessel MMSIs, and maintains an in-memory latest-
position cache of canonical AssetPosition DTOs.

Architectural boundaries
────────────────────────
- This adapter is a feed boundary ONLY.
- It does NOT persist to SQLite, import ORM models, or write to any DB.
- It does NOT require a leg_id or shipment_id.
- It does NOT publish SSE events or start APScheduler jobs.
- A later worker (T-029) reads from this adapter and persists positions.

Reconnect / backoff policy
──────────────────────────
Delays: 1 s → 2 s → 4 s → 8 s → … capped at 60 s.
Reset:  Only after a valid PositionReport is successfully received and
        cached.  A bare TCP/WebSocket handshake or subscription ACK is
        insufficient — this is the safer choice for flapping connections.

Health policy
─────────────
- BEFORE start():           unhealthy, last_success_at=None, count=0
- AFTER successful connect + subscription sent: still reported by the
  same counters; only `is_healthy` flips to True once the send loop
  is active and has not yet errored.
- AFTER first valid position cached: last_success_at updated, count ≥ 1
- DURING reconnect/backoff: unhealthy
- AFTER stop():             unhealthy
- Configuration errors:     unhealthy (never starts)

Heading strategy
────────────────
AIS PositionReport carries:
  TrueHeading  (0-359 degrees; 511 = unavailable)
  Cog          (course-over-ground; 360.0 = unavailable)

We prefer TrueHeading.  If TrueHeading is unavailable (511) we fall
back to Cog only when Cog is also a valid navigational value
(0.0 ≤ cog < 360.0).  If both are unavailable, heading_deg = None.

Compression
───────────
permessage-deflate is enabled via websockets 13.x's
`compression="deflate"` keyword argument on `connect()`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from nexafreight.adapters.protocols import (
    AssetPosition,
    AssetType,
    FeedHealth,
    Provenance,
)
from nexafreight.config import get_settings

# ---------------------------------------------------------------------------
# Module-level logger — uses stdlib logging; structlog is wired at app level.
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Official AISStream stream endpoint.
_AISSTREAM_URL: str = "wss://stream.aisstream.io/v0/stream"

#: Stable adapter name reported in FeedHealth.
_ADAPTER_NAME: str = "aisstream"

#: AIS sentinel: TrueHeading unavailable.
_AIS_HEADING_UNAVAILABLE: int = 511

#: AIS sentinel: COG unavailable (spec says 360.0).
_AIS_COG_UNAVAILABLE: float = 360.0

#: AIS sentinel: SOG unavailable (102.3 knots per spec).
_AIS_SOG_UNAVAILABLE: float = 102.3

#: Backoff timing.
_BACKOFF_INITIAL_S: float = 1.0
_BACKOFF_MAX_S: float = 60.0

#: MMSI validation: 9 decimal digits.
_MMSI_RE: re.Pattern[str] = re.compile(r"^\d{9}$")


# ---------------------------------------------------------------------------
# Type aliases for injected seams
# ---------------------------------------------------------------------------

#: A coroutine factory that returns an async context manager yielding a
#: WebSocket-like connection.  Injected in tests to avoid real network calls.
#: In production the factory is built from the `websockets` library.
ConnectionFactory = Callable[..., Any]

#: Async sleep coroutine — injected as `asyncio.sleep` in production.
AsyncSleepFn = Callable[[float], Coroutine[Any, Any, None]]


# ---------------------------------------------------------------------------
# Internal mutable state dataclass (private)
# ---------------------------------------------------------------------------


@dataclass
class _AdapterState:
    """All mutable lifecycle state in one place, separate from public API."""

    is_running: bool = False
    is_healthy: bool = False
    last_success_at: datetime | None = None
    messages_received: int = 0

    #: Latest positions keyed by normalised MMSI string.
    cache: dict[str, AssetPosition] = field(default_factory=dict)

    #: The background receive task; None when stopped.
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    #: Current WebSocket connection; None when not connected.
    #: Only the receive loop touches this field during its lifetime.
    _ws: Any | None = field(default=None, repr=False)

    #: Current backoff delay in seconds.
    _backoff: float = _BACKOFF_INITIAL_S


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_mmsi(raw: str) -> str:
    """Normalise and validate a single MMSI string.

    MMSIs are exactly 9 decimal digits.  Leading zeroes are significant
    and must be preserved.  We strip whitespace but otherwise require the
    raw value to match the pattern exactly.

    Raises:
        ValueError: If the MMSI does not conform to the 9-digit format.
    """
    normalised = raw.strip()
    if not _MMSI_RE.match(normalised):
        raise ValueError(f"Invalid MMSI {normalised!r}: must be exactly 9 decimal digits.")
    return normalised


def _normalise_mmsi_set(raw_mmsis: list[str] | set[str]) -> frozenset[str]:
    """Validate and deduplicate a collection of MMSI strings.

    Raises:
        ValueError: If any MMSI is malformed or the resulting set is empty.
    """
    if not raw_mmsis:
        raise ValueError("MMSI list must not be empty.")

    validated: set[str] = set()
    for raw in raw_mmsis:
        validated.add(_validate_mmsi(raw))

    return frozenset(validated)


# ---------------------------------------------------------------------------
# AIS field extraction helpers
# ---------------------------------------------------------------------------


def _extract_heading(position_report: dict[str, Any]) -> float | None:
    """Determine the best available heading from an AIS PositionReport dict.

    Strategy:
      1. Use TrueHeading if present and not the unavailable sentinel (511).
      2. Fall back to Cog if present, valid (0 <= cog < 360.0), and not
         the unavailable sentinel (360.0).
      3. Return None when both are unavailable.
    """
    true_heading = position_report.get("TrueHeading")
    if (
        true_heading is not None
        and isinstance(true_heading, int | float)
        and int(true_heading) != _AIS_HEADING_UNAVAILABLE
    ):
        return float(true_heading)

    cog = position_report.get("Cog")
    if (
        cog is not None
        and isinstance(cog, int | float)
        and float(cog) < _AIS_COG_UNAVAILABLE
        and float(cog) >= 0.0
    ):
        return float(cog)

    return None


def _extract_speed(position_report: dict[str, Any]) -> float | None:
    """Extract speed-over-ground in knots.

    Returns None when the AIS unavailable sentinel (102.3) is present
    or when the field is absent/non-numeric.
    """
    sog = position_report.get("Sog")
    if sog is None or not isinstance(sog, int | float):
        return None
    sog_f = float(sog)
    if sog_f >= _AIS_SOG_UNAVAILABLE:
        return None
    return sog_f


def _extract_mmsi(message: dict[str, Any]) -> str | None:
    """Extract MMSI from an AISStream message envelope.

    AISStream places the MMSI in ``MetaData.MMSI`` and also in
    ``Message.PositionReport.UserID``.  We prefer the envelope
    MetaData field and fall back to the inner UserID field.
    The value is returned as a zero-padded 9-digit string or None
    when extraction fails validation.
    """
    # Primary: MetaData.MMSI (integer in the envelope)
    meta = message.get("MetaData", {})
    raw_mmsi = meta.get("MMSI")

    if raw_mmsi is None:
        # Fallback: PositionReport.UserID
        pos = message.get("Message", {}).get("PositionReport", {})
        raw_mmsi = pos.get("UserID")

    if raw_mmsi is None:
        return None

    try:
        # Normalise integer or string to 9-digit zero-padded string
        return _validate_mmsi(str(int(raw_mmsi)).zfill(9))
    except (ValueError, TypeError):
        return None


def _validate_coordinates(lat: Any, lon: Any) -> tuple[float, float] | None:
    """Validate and return (lat, lon) as floats, or None if invalid.

    Valid ranges: lat in [-90, 90], lon in [-180, 180].
    AIS reports (91.0 / 181.0) are used as sentinels for "not available".
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None

    if not (-90.0 <= lat_f <= 90.0):
        return None
    if not (-180.0 <= lon_f <= 180.0):
        return None
    return lat_f, lon_f


def _parse_position_report(
    message: dict[str, Any],
    now: datetime,
    configured_mmsis: frozenset[str],
) -> AssetPosition | None:
    """Parse an AISStream message dict into an AssetPosition, or return None.

    Returns None when:
    - MessageType is not "PositionReport".
    - MMSI cannot be extracted or is not in the configured set.
    - Latitude/longitude are missing or invalid.
    - Any unexpected parsing error occurs (non-fatal).

    Does NOT raise — all failures are non-fatal by design.
    """
    # Guard: only handle PositionReport messages
    msg_type = message.get("MessageType")
    if msg_type != "PositionReport":
        return None

    # Extract and gate on configured MMSIs
    mmsi = _extract_mmsi(message)
    if mmsi is None:
        logger.debug("Skipping AIS message: MMSI could not be extracted.")
        return None
    if mmsi not in configured_mmsis:
        # Defensive: feed may send unsolicited MMSIs.
        logger.debug("Ignoring unsolicited MMSI %s.", mmsi)
        return None

    pos_report: dict[str, Any] = message.get("Message", {}).get("PositionReport", {})

    # Latitude / longitude
    coords = _validate_coordinates(
        pos_report.get("Latitude"),
        pos_report.get("Longitude"),
    )
    if coords is None:
        logger.debug("Skipping MMSI %s: invalid or unavailable coordinates.", mmsi)
        return None
    lat, lon = coords

    speed = _extract_speed(pos_report)
    heading = _extract_heading(pos_report)

    # Use injected current time as the authoritative timestamp.
    # AISStream's inner "second-of-minute" field is NOT a full timestamp.
    reported_at = now

    return AssetPosition(
        asset_id=mmsi,
        asset_type=AssetType.SEA,
        lat=lat,
        lon=lon,
        speed_knots=speed,
        heading_deg=heading,
        reported_at=reported_at,
        provenance=Provenance.REAL,
        source=_ADAPTER_NAME.upper(),
    )


# ---------------------------------------------------------------------------
# Subscription message builder
# ---------------------------------------------------------------------------


def _build_subscription_payload(
    api_key_value: str,
    mmsis: frozenset[str],
) -> str:
    """Build the JSON subscription message for AISStream.

    The api_key_value must already be the unwrapped plain-text string.
    This function is called only at subscription time; the key is never
    stored in module state after this call returns.
    """
    payload = {
        "APIKey": api_key_value,
        "FiltersShipMMSI": sorted(mmsis),  # deterministic ordering
        "FilterMessageTypes": ["PositionReport"],
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------


class AISStreamAdapter:
    """Asynchronous AISStream WebSocket position-feed adapter.

    Conforms to the ``PositionFeedAdapter`` protocol defined in
    ``nexafreight.adapters.protocols``.

    Parameters
    ----------
    mmsis:
        Collection of 9-digit vessel MMSI strings to subscribe to.
        Must be non-empty.  Duplicates are removed automatically.
    api_key:
        Optional API key override as a plain string.  When None (default)
        the key is read from the application Settings at ``start()`` time.
        Tests inject a fake key here to avoid touching the Settings object.
    ws_url:
        WebSocket URL override for tests.  Defaults to the official
        AISStream endpoint.
    connection_factory:
        Async context-manager factory for WebSocket connections.  Defaults
        to ``websockets.connect``.  Tests inject a fake factory here so no
        real network connection is made.
    sleep_fn:
        Async sleep callable.  Defaults to ``asyncio.sleep``.  Tests inject
        a no-op coroutine to avoid real waiting.
    now_fn:
        Callable returning a timezone-aware UTC ``datetime``.  Defaults to
        ``lambda: datetime.now(timezone.utc)``.  Tests inject a fixed clock.

    Raises
    ------
    ValueError
        If ``mmsis`` is empty or contains malformed MMSI strings.
        Raised at construction time, before any network activity.
    """

    def __init__(
        self,
        mmsis: list[str] | set[str],
        *,
        api_key: str | None = None,
        ws_url: str = _AISSTREAM_URL,
        connection_factory: ConnectionFactory | None = None,
        sleep_fn: AsyncSleepFn | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        # Validate and deduplicate MMSIs at construction time.
        self._mmsis: frozenset[str] = _normalise_mmsi_set(mmsis)

        # Injected or plain-string key (tests only).
        # None means "read from Settings at start() time".
        self._injected_api_key: str | None = api_key

        self._ws_url: str = ws_url

        # Seams for testing
        self._connection_factory: ConnectionFactory = (
            connection_factory if connection_factory is not None else _default_connection_factory()
        )
        self._sleep_fn: AsyncSleepFn = sleep_fn or asyncio.sleep
        self._now_fn: Callable[[], datetime] = now_fn or (lambda: datetime.now(UTC))

        # All mutable runtime state lives here.
        self._state: _AdapterState = _AdapterState()

        # Lock to protect cache + state writes from concurrent access.
        # We do NOT hold this lock while awaiting network I/O.
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Protocol-required public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background WebSocket receive task.

        Idempotent: calling ``start()`` on an already-running adapter
        is a no-op.  This method returns quickly; the receive loop runs
        as a background asyncio.Task.

        Raises:
            RuntimeError: If the AISStream API key is not configured (either
                injected or present in application Settings).  No network
                connection is attempted before raising.
        """
        async with self._lock:
            if self._state.is_running:
                logger.debug(
                    "AISStreamAdapter.start() called on already-running adapter — ignored."
                )
                return

            # Resolve API key; fail fast with a clear error — no retry.
            api_key = self._resolve_api_key()

            logger.info(
                "AISStreamAdapter starting.",
                extra={"mmsi_count": len(self._mmsis), "url": self._ws_url},
            )
            self._state.is_running = True
            self._state._backoff = _BACKOFF_INITIAL_S

            # Start the background receive loop as a Task.
            self._state._task = asyncio.create_task(
                self._receive_loop(api_key),
                name="aisstream-receive-loop",
            )

    async def stop(self) -> None:
        """Stop the background WebSocket receive task and close connection.

        Idempotent: safe to call before ``start()`` or multiple times.
        Cancels the receive task and waits for it to exit cleanly.
        """
        async with self._lock:
            if not self._state.is_running and self._state._task is None:
                logger.debug("AISStreamAdapter.stop() called on already-stopped adapter — ignored.")
                return

            logger.info("AISStreamAdapter stopping.")
            self._state.is_running = False
            self._state.is_healthy = False

        task = self._state._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # Expected; intentional cancellation during stop.

        async with self._lock:
            self._state._task = None
            self._state._ws = None

        logger.info("AISStreamAdapter stopped.")

    async def get_current_positions(self) -> list[AssetPosition]:
        """Return a stable snapshot of latest position for each tracked vessel.

        Returns a list sorted by ``asset_id`` (MMSI) for deterministic ordering.
        The returned list is a new object; callers may not mutate the cache.
        """
        async with self._lock:
            snapshot = dict(self._state.cache)
        return sorted(snapshot.values(), key=lambda p: p.asset_id)

    async def health(self) -> FeedHealth:
        """Return an immutable snapshot of current feed health.

        Never contains secrets or raw message data.
        """
        async with self._lock:
            return FeedHealth(
                adapter_name=_ADAPTER_NAME,
                is_healthy=self._state.is_healthy,
                last_success_at=self._state.last_success_at,
                messages_received=self._state.messages_received,
                provenance=Provenance.REAL,
            )

    async def get_health(self) -> FeedHealth:
        """Compatibility method alias for health()."""
        return await self.health()

    # ------------------------------------------------------------------
    # Internal: API key resolution
    # ------------------------------------------------------------------

    def _resolve_api_key(self) -> str:
        """Resolve the AISStream API key.

        Priority:
          1. Injected plain-string key (tests / explicit override).
          2. Application Settings ``aisstream_api_key`` (SecretStr).

        Returns the unwrapped key string ONLY to the caller.
        The key is never stored in adapter state after this method returns.

        Raises:
            RuntimeError: If no key is available. Message does not contain
                the key value.
        """
        if self._injected_api_key is not None:
            return self._injected_api_key

        settings = get_settings()

        if settings.aisstream_api_key is None:
            raise RuntimeError(
                "AISStreamAdapter: no API key configured. "
                "Set AISSTREAM_API_KEY in your environment or .env file. "
                "Do not attempt to start the adapter without a key."
            )
        # Unwrap SecretStr at the last possible moment.
        return settings.aisstream_api_key.get_secret_value()

    # ------------------------------------------------------------------
    # Internal: receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self, api_key: str) -> None:
        """Background asyncio Task: connect -> subscribe -> receive -> reconnect.

        This loop runs until:
        - ``is_running`` is set to False by ``stop()``.
        - The task is cancelled by ``stop()``.

        Intentional ``asyncio.CancelledError`` from stop() exits cleanly.
        Configuration errors (bad key, empty MMSI) do NOT enter this loop.
        All transient network / protocol errors use exponential backoff.
        """
        while True:
            # Exit immediately if stop() was called between iterations.
            async with self._lock:
                if not self._state.is_running:
                    return

            try:
                await self._connect_and_receive(api_key)
                async with self._lock:
                    if not self._state.is_running:
                        return
                    self._state.is_healthy = False
                    delay = self._state._backoff
                    self._state._backoff = min(self._state._backoff * 2, _BACKOFF_MAX_S)
                logger.info("AISStreamAdapter disconnected; retrying in %.0f s.", delay)
                await self._sleep_fn(delay)
            except asyncio.CancelledError:
                # Intentional shutdown; exit cleanly.
                logger.debug("AISStreamAdapter receive loop cancelled (stop requested).")
                async with self._lock:
                    self._state.is_healthy = False
                return
            except Exception as exc:  # noqa: BLE001
                # Network/protocol error; back off and retry.
                async with self._lock:
                    if not self._state.is_running:
                        return
                    self._state.is_healthy = False
                    delay = self._state._backoff
                    # Increase backoff for the next failure.
                    self._state._backoff = min(self._state._backoff * 2, _BACKOFF_MAX_S)

                logger.warning(
                    "AISStreamAdapter connection error; retrying in %.0f s.",
                    delay,
                    extra={
                        "error_type": type(exc).__name__,
                        "retry_delay_s": delay,
                    },
                )
                await self._sleep_fn(delay)

    async def _connect_and_receive(self, api_key: str) -> None:
        """Open one WebSocket connection, subscribe, and receive until disconnect.

        On a clean exit the caller's ``_receive_loop`` decides whether to
        reconnect.  Raises on any connection or protocol error so the loop
        can apply backoff.

        Compression
        -----------
        ``compression="deflate"`` enables permessage-deflate negotiation via
        the websockets 13.x API.  See websockets docs section compression.
        """
        logger.info(
            "AISStreamAdapter connecting to %s (MMSIs: %d).",
            self._ws_url,
            len(self._mmsis),
        )

        # Build subscription payload here; key is used and then discarded.
        subscription_json = _build_subscription_payload(api_key, self._mmsis)

        async with self._connection_factory(
            self._ws_url,
            compression="deflate",
        ) as ws:
            async with self._lock:
                self._state._ws = ws
                self._state.is_healthy = True

            logger.info(
                "AISStreamAdapter connected; sending subscription for %d MMSIs.",
                len(self._mmsis),
            )

            # Send subscription immediately after handshake.
            await ws.send(subscription_json)

            logger.info(
                "AISStreamAdapter subscribed. Waiting for PositionReport messages.",
            )

            # Receive loop for this connection.
            async for raw_message in ws:
                # Check for stop between messages.
                async with self._lock:
                    if not self._state.is_running:
                        return

                await self._handle_raw_message(raw_message)

        # WebSocket closed cleanly — treat as a disconnect and reconnect.
        async with self._lock:
            self._state.is_healthy = False
            self._state._ws = None
        logger.info("AISStreamAdapter WebSocket closed; will reconnect.")

    async def _handle_raw_message(self, raw: str | bytes) -> None:
        """Parse one raw WebSocket message and update the cache if valid.

        Non-fatal for all parsing errors.  Never increments
        ``messages_received`` for invalid/ignored messages.
        """
        # Decode bytes to str if necessary.
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                logger.debug("AISStreamAdapter: received non-UTF-8 bytes; skipping.")
                return
        else:
            text = raw

        # Parse JSON safely.
        try:
            message: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("AISStreamAdapter: received non-JSON message; skipping.")
            return

        if not isinstance(message, dict):
            logger.debug("AISStreamAdapter: received non-object JSON; skipping.")
            return

        now = self._now_fn()

        # Attempt to parse as a PositionReport.
        position = _parse_position_report(message, now, self._mmsis)
        if position is None:
            # Not a PositionReport or invalid — non-fatal, do not count.
            return

        # Valid position: update cache and counters.
        async with self._lock:
            self._state.cache[position.asset_id] = position
            self._state.messages_received += 1
            self._state.last_success_at = now
            # Reset backoff only after a valid position is received.
            self._state._backoff = _BACKOFF_INITIAL_S
            self._state.is_healthy = True


# ---------------------------------------------------------------------------
# Production WebSocket connection factory
# ---------------------------------------------------------------------------


def _default_connection_factory() -> ConnectionFactory:
    """Return the production connection factory using the ``websockets`` library.

    We import ``websockets`` here (not at module level) so that test files
    that do not install ``websockets`` can still import this module without
    errors, provided they inject a fake factory.

    The returned factory mirrors the ``websockets.connect`` signature and
    supports the ``compression="deflate"`` keyword argument introduced in
    websockets >= 10 and present in 13.x.
    """
    try:
        import websockets  # noqa: PLC0415

        return websockets.connect
    except ImportError as exc:
        raise ImportError(
            "The 'websockets' package is required for AISStreamAdapter. "
            "Add websockets>=13.1 to your dependencies."
        ) from exc
