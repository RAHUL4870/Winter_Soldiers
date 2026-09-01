"""
Parquet-based historical AIS replay adapter  (T-027).

Reads historical AIS position records from local Parquet files, advances a
virtual clock at a configurable speed multiplier, and serves positions
through the exact same PositionFeedAdapter protocol as AISStreamAdapter
(T-026).

Interchangeability
──────────────────
This module is a sibling of aisstream.py — neither imports the other.
Any caller typed against PositionFeedAdapter can substitute one for the
other purely at the construction/injection site.

Architectural boundaries
────────────────────────
  • Feed boundary ONLY.  No SQLite, no ORM, no leg_id, no SSE, no APScheduler.
  • No network access.  Parquet files must exist locally before start().
  • Does NOT implement the USE_LIVE_AIS selection switch — that is a later
    worker/factory task.
  • Does NOT import from aisstream.py.  AIS sentinel constants that appear
    in both modules are defined independently; see explanation below.

Provenance policy
─────────────────
Every AssetPosition produced here carries Provenance.REPLAYED.
This is genuine historical AIS data, not synthesised (SIMULATED) or live
(REAL), and not a test double (MOCK).  The REPLAYED label is the truthful
one.

Asset-type policy
─────────────────
Every AssetPosition carries AssetType.SEA.  AIS is a maritime protocol.

Health policy — exhausted-but-serving state
───────────────────────────────────────────
When the adapter exhausts the dataset and loop=False, it transitions to
is_exhausted=True but keeps is_healthy=True and continues returning the
last-known position per vessel.

Rationale: exhaustion is the expected, designed terminal state for a finite
historical dataset.  The adapter is still working correctly.  Reporting
is_healthy=False at that point would fire spurious alarms on the T-068
feed-health dashboard.  Operators can distinguish an exhausted replay from
a live feed by observing that last_success_at stops advancing while
is_healthy stays True.  The is_exhausted property gives callers an explicit
programmatic signal if they need it.

AIS sentinel constants
──────────────────────
TrueHeading == 511  → unavailable
COG         == 360.0 → unavailable
SOG         >= 102.3 → unavailable
Lat sentinel 91.0 and Lon sentinel 181.0 are caught by coordinate bounds.
These values are defined locally (not imported from aisstream.py) because
the two adapters are siblings, and a shared constants module is out of scope
for T-027.

Virtual clock formula
─────────────────────
  virtual_now = virtual_anchor + timedelta(seconds=
                    (monotonic_now − real_anchor) × speed_multiplier)

Re-anchoring happens only on loop reset; it never happens in normal ticks,
so accumulated floating-point drift from the anchor is the only source of
error.

Column alias table
──────────────────
The loader accepts columns by normalised (lowercased, whitespace-stripped)
name.  The first match in each row wins.  Documented aliases cover NOAA
Marine Cadastre and Danish Maritime Authority export formats.

  canonical  │ recognised aliases
  ───────────┼──────────────────────────────────────────────────────
  mmsi       │ mmsi  userid  user_id  shipmmsi
  timestamp  │ basedatetime  base_date_time  timestamp  time
             │ datetime  reported_at  # timestamp  (DMA quirk)
  lat        │ lat  latitude  y
  lon        │ lon  long  longitude  x
  sog        │ sog  speed  speedoverground  speed_over_ground
  cog        │ cog  course  courseoverground  course_over_ground
  heading    │ heading  trueheading  true_heading
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nexafreight.adapters.protocols import (
    AssetPosition,
    AssetType,
    FeedHealth,
    Provenance,
)

# ---------------------------------------------------------------------------
# Module logger
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Stable adapter name reported in every FeedHealth snapshot.
ADAPTER_NAME: str = "replay_ais"
_ADAPTER_NAME: str = ADAPTER_NAME

#: Source label stamped on every AssetPosition.
_SOURCE: str = "AIS_PARQUET_REPLAY"

#: Default tick interval in real seconds.
_DEFAULT_TICK_INTERVAL_S: float = 0.1

#: Default replay speed: 60 historical seconds per 1 real second.
_DEFAULT_SPEED_MULTIPLIER: float = 60.0

# ---------------------------------------------------------------------------
# AIS sentinel values (defined locally; see module docstring for rationale)
# ---------------------------------------------------------------------------

_AIS_HEADING_UNAVAILABLE: int = 511
_AIS_COG_UNAVAILABLE: float = 360.0
_AIS_SOG_UNAVAILABLE: float = 102.3

# ---------------------------------------------------------------------------
# MMSI constants
# ---------------------------------------------------------------------------

_MMSI_LENGTH: int = 9

# ---------------------------------------------------------------------------
# Column alias table
# ---------------------------------------------------------------------------

#: Maps canonical field key → tuple of recognised lower-cased column names.
#: The loader iterates actual column names and tests membership.
_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "mmsi": ("mmsi", "userid", "user_id", "shipmmsi"),
    "timestamp": (
        "basedatetime",
        "base_date_time",
        "timestamp",
        "time",
        "datetime",
        "reported_at",
        "#timestamp",  # Danish Maritime Authority quirk (stripped of space)
        "# timestamp",
    ),
    "lat": ("lat", "latitude", "y"),
    "lon": ("lon", "long", "longitude", "x"),
    "sog": ("sog", "speed", "speedoverground", "speed_over_ground"),
    "cog": ("cog", "course", "courseoverground", "course_over_ground"),
    "heading": ("heading", "trueheading", "true_heading"),
}

# ---------------------------------------------------------------------------
# Injectable seam types
# ---------------------------------------------------------------------------

AsyncSleepFn = Callable[[float], Coroutine[Any, Any, None]]
MonotonicFn = Callable[[], float]
NowFn = Callable[[], datetime]

#: Loader seam: receives (path, configured_mmsis) → list[ReplayRecord].
#: Production code uses _load_records_from_parquet; tests inject a fixture.
RecordLoaderFn = Callable[[Path, frozenset[str]], list["ReplayRecord"]]


# ---------------------------------------------------------------------------
# Internal DTO
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    """
    One normalised historical AIS observation.

    Internal loader DTO, never exposed through the public API.  Kept
    separate from AssetPosition so that loading and normalisation are
    testable independently of the protocol DTO.
    """

    mmsi: str
    timestamp: datetime  # always timezone-aware UTC
    lat: float
    lon: float
    speed_knots: float | None
    heading_deg: float | None


# ---------------------------------------------------------------------------
# MMSI validation helpers
# ---------------------------------------------------------------------------


def _validate_mmsi(raw: str) -> str:
    """
    Normalise and validate one MMSI string.

    Policy (identical to T-026):
      - Strip surrounding whitespace.
      - Must be exactly 9 decimal digits.
      - Leading zeroes are significant and preserved.

    Raises
    ------
    ValueError
        When the value is not a 9-digit decimal string.
    """
    normalised = raw.strip()
    if len(normalised) != _MMSI_LENGTH or not normalised.isdigit():
        raise ValueError(f"Invalid MMSI {normalised!r}: must be exactly 9 decimal digits.")
    return normalised


def _normalise_mmsi_set(
    raw_mmsis: Sequence[str] | set[str],
) -> frozenset[str]:
    """
    Validate and deduplicate a collection of MMSI strings.

    Raises
    ------
    ValueError
        When the collection is empty or any entry is malformed.
    """
    if not raw_mmsis:
        raise ValueError("MMSI list must not be empty.")
    return frozenset(_validate_mmsi(raw) for raw in raw_mmsis)


# ---------------------------------------------------------------------------
# Configuration validation helpers
# ---------------------------------------------------------------------------


def _validate_speed_multiplier(value: float) -> float:
    """
    Validate that the speed multiplier is a finite positive number.

    Raises
    ------
    ValueError
        When the value is zero, negative, NaN, or infinite.
    """
    if not isinstance(value, int | float):
        raise ValueError(f"speed_multiplier must be numeric, got {type(value).__name__}.")
    f = float(value)
    # The comparison `f > 0.0` is False for NaN, which is correct.
    if not (f > 0.0) or f == float("inf"):
        raise ValueError(f"speed_multiplier must be a finite positive number, got {f!r}.")
    return f


def _validate_start_at(value: datetime | None) -> datetime | None:
    """
    Validate and normalise the optional replay start timestamp.

    Raises
    ------
    ValueError
        When the datetime is naive (has no tzinfo).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(
            "start_at must be timezone-aware (UTC).  "
            "Pass e.g. datetime(2024, 1, 1, tzinfo=timezone.utc)."
        )
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# AIS field normalisation helpers
# ---------------------------------------------------------------------------


def _norm_speed(raw: Any) -> float | None:
    """
    Normalise speed-over-ground to knots, or None when unavailable.

    Returns None for:
      - null / missing values
      - non-numeric strings
      - NaN
      - negative values
      - values at or above the AIS unavailable sentinel (102.3 knots)
    """
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN check without importing math
        return None
    if v < 0.0 or v >= _AIS_SOG_UNAVAILABLE:
        return None
    return v


_normalise_speed = _norm_speed


def _norm_heading(raw_heading: Any, raw_cog: Any) -> float | None:
    """
    Determine best heading using the identical policy to T-026.

    Strategy:
      1. Use TrueHeading when present and not the unavailable sentinel (511).
      2. Fall back to COG when it is valid (0 ≤ cog < 360.0).
      3. Return None — never fabricate a heading.
    """
    if raw_heading is not None:
        try:
            h = float(raw_heading)
            if h == h and int(h) != _AIS_HEADING_UNAVAILABLE and 0.0 <= h < 360.0:
                return h
        except (TypeError, ValueError):
            pass

    if raw_cog is not None:
        try:
            c = float(raw_cog)
            if c == c and 0.0 <= c < _AIS_COG_UNAVAILABLE:
                return c
        except (TypeError, ValueError):
            return None

    return None


_normalise_heading = _norm_heading


def _coords_valid(lat: float, lon: float) -> bool:
    """
    Check whether coordinates fall inside valid geographic bounds.

    AIS unavailable sentinels (lat 91.0, lon 181.0) are outside these
    bounds and are therefore rejected automatically.
    """
    if lat != lat or lon != lon:  # NaN
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


_coordinates_valid = _coords_valid


def _coerce_timestamp(raw: Any) -> datetime | None:
    """
    Coerce a Parquet cell into a timezone-aware UTC datetime.

    Accepted input types:
      - datetime (naive → assumed UTC; aware → converted to UTC)
      - str in ISO-8601 format

    Naive datetimes are assumed UTC because both NOAA Marine Cadastre and
    Danish Maritime Authority AIS exports use UTC without an explicit offset.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw.astimezone(UTC)
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _coerce_mmsi(raw: Any) -> str | None:
    """
    Coerce a Parquet MMSI value (int or str) to a 9-digit zero-padded string.

    Integer MMSIs lose leading zeroes in Parquet; zero-padding restores them.
    Returns None when the value cannot form a valid MMSI.
    """
    if raw is None:
        return None
    try:
        as_int = int(raw)
    except (TypeError, ValueError):
        return None
    if as_int < 0:
        return None
    candidate = str(as_int).zfill(_MMSI_LENGTH)
    try:
        return _validate_mmsi(candidate)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Column map resolution
# ---------------------------------------------------------------------------


def resolve_column_map(column_names: Sequence[str]) -> dict[str, str]:
    """
    Map canonical field keys → actual column names found in a Parquet table.

    Matching is case-insensitive and ignores surrounding whitespace.  The
    first matching alias for each canonical key wins.

    Returns a dict of ``{canonical_key: actual_column_name}`` containing
    only the keys that were resolved.  Callers validate required keys.
    """
    result: dict[str, str] = {}
    for actual in column_names:
        normalised = actual.strip().lower().replace(" ", "")
        for canonical, aliases in _COLUMN_ALIASES.items():
            if canonical in result:
                continue
            if normalised in aliases:
                result[canonical] = actual
                break
    return result


# ---------------------------------------------------------------------------
# Row → ReplayRecord conversion
# ---------------------------------------------------------------------------


def rows_to_records(
    rows: Sequence[dict[str, Any]],
    column_map: dict[str, str],
    configured_mmsis: frozenset[str],
) -> list[ReplayRecord]:
    """
    Convert raw Parquet rows into validated, filtered, sorted ReplayRecords.

    Steps
    -----
    1. Verify required columns are mapped; raise ValueError if not.
    2. For each row: coerce MMSI → skip if not in configured set.
    3. Coerce timestamp → skip if None.
    4. Coerce and validate lat/lon → skip if invalid.
    5. Normalise speed and heading via sentinel policy.
    6. Sort result by (mmsi, timestamp) ascending.

    Malformed rows are skipped non-fatally.  A single bad row must not
    invalidate an entire historical dataset.
    """
    mmsi_col = column_map.get("mmsi")
    ts_col = column_map.get("timestamp")
    lat_col = column_map.get("lat")
    lon_col = column_map.get("lon")
    sog_col = column_map.get("sog")
    cog_col = column_map.get("cog")
    hdg_col = column_map.get("heading")

    missing = [
        name
        for name, col in (
            ("mmsi", mmsi_col),
            ("timestamp", ts_col),
            ("lat", lat_col),
            ("lon", lon_col),
        )
        if col is None
    ]
    if missing:
        raise ValueError(
            f"Parquet data is missing required AIS column(s): "
            f"{', '.join(missing)}.  Available columns could not be mapped "
            f"to the expected AIS schema."
        )

    records: list[ReplayRecord] = []
    skipped = 0

    for row in rows:
        mmsi = _coerce_mmsi(row.get(mmsi_col))  # type: ignore[arg-type]
        if mmsi is None or mmsi not in configured_mmsis:
            continue

        ts = _coerce_timestamp(row.get(ts_col))  # type: ignore[arg-type]
        if ts is None:
            skipped += 1
            continue

        try:
            lat = float(row[lat_col])  # type: ignore[index]
            lon = float(row[lon_col])  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue

        if not _coords_valid(lat, lon):
            skipped += 1
            continue

        records.append(
            ReplayRecord(
                mmsi=mmsi,
                timestamp=ts,
                lat=lat,
                lon=lon,
                speed_knots=_norm_speed(
                    row.get(sog_col) if sog_col else None  # type: ignore[arg-type]
                ),
                heading_deg=_norm_heading(
                    row.get(hdg_col) if hdg_col else None,  # type: ignore[arg-type]
                    row.get(cog_col) if cog_col else None,  # type: ignore[arg-type]
                ),
            )
        )

    if skipped:
        logger.debug(
            "Replay loader skipped %d malformed or out-of-bounds AIS row(s).",
            skipped,
        )

    records.sort(key=lambda r: (r.mmsi, r.timestamp))
    return records


# ---------------------------------------------------------------------------
# Parquet file discovery and production loader
# ---------------------------------------------------------------------------


def _discover_parquet_files(path: Path) -> list[Path]:
    """
    Resolve *path* to an ordered list of Parquet files.

    Rules
    -----
    - A file path → returns that single file in a list.
    - A directory → returns all ``*.parquet`` files beneath it, sorted
      lexicographically by full path for deterministic load order.

    Raises
    ------
    FileNotFoundError
        When the path does not exist, or a directory contains no Parquet files.
    """
    if not path.exists():
        raise FileNotFoundError(f"AIS replay data path does not exist: {path}")
    if path.is_file():
        return [path]

    files = sorted(p for p in path.rglob("*.parquet") if p.is_file())
    if not files:
        raise FileNotFoundError(f"AIS replay data directory contains no .parquet files: {path}")
    return files


def _load_records_from_parquet(
    path: Path,
    configured_mmsis: frozenset[str],
) -> list[ReplayRecord]:
    """
    Production loader: read Parquet file(s) at *path* with pyarrow.

    pyarrow is imported lazily so that:
      - Importing this module never triggers a heavy dependency load.
      - Unit tests that inject a fake loader never need pyarrow at all.

    Raises
    ------
    FileNotFoundError
        Propagated from _discover_parquet_files.
    ValueError
        When required columns are absent (propagated from rows_to_records).
    ImportError
        When pyarrow is not installed.
    """
    try:
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "pyarrow is required to read AIS replay Parquet data.  "
            "Add pyarrow>=17.0.0 to your dependencies."
        ) from exc

    files = _discover_parquet_files(path)
    all_records: list[ReplayRecord] = []

    for file_path in files:
        table = pq.read_table(file_path)
        col_map = resolve_column_map(table.column_names)
        all_records.extend(rows_to_records(table.to_pylist(), col_map, configured_mmsis))

    # Final global sort after merging multiple files.
    all_records.sort(key=lambda r: (r.mmsi, r.timestamp))
    return all_records


# ---------------------------------------------------------------------------
# Internal mutable state container
# ---------------------------------------------------------------------------


@dataclass
class _ReplayState:
    """
    All mutable lifecycle state in one place, separated from the public API.

    Only the adapter instance that owns this object may write to it.
    All writes happen under the adapter's asyncio.Lock.
    """

    # ── Lifecycle ─────────────────────────────────────────────────────────
    is_running: bool = False
    is_healthy: bool = False
    is_exhausted: bool = False

    # ── Health metrics ────────────────────────────────────────────────────
    last_success_at: datetime | None = None
    messages_received: int = 0

    # ── Position cache ────────────────────────────────────────────────────
    cache: dict[str, AssetPosition] = field(default_factory=dict)

    # ── Replay timeline ───────────────────────────────────────────────────
    #: Records sorted globally by (timestamp, mmsi) for playback order.
    timeline: list[ReplayRecord] = field(default_factory=list, repr=False)

    #: Index of the next record that has not yet been served.
    cursor: int = 0

    # ── Virtual clock ─────────────────────────────────────────────────────
    #: The historical timestamp that the virtual clock was at when
    #: real_anchor was captured.  Together they define the virtual clock:
    #:   virtual_now = virtual_anchor + timedelta(seconds=
    #:                     (monotonic() − real_anchor) × speed_multiplier)
    virtual_anchor: datetime | None = None
    real_anchor: float = 0.0

    #: Cached result of the most recent virtual clock calculation.
    virtual_now: datetime | None = None

    # ── Background task ───────────────────────────────────────────────────
    _task: asyncio.Task[None] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Conversion: internal DTO → canonical protocol DTO
# ---------------------------------------------------------------------------


def _record_to_position(record: ReplayRecord) -> AssetPosition:
    """
    Convert a ReplayRecord to the canonical AssetPosition DTO.

    ``reported_at`` carries the record's **historical** timestamp, not the
    wall-clock instant of replay.  The REPLAYED provenance label already
    communicates to callers that the timeline is shifted.
    """
    return AssetPosition(
        asset_id=record.mmsi,
        asset_type=AssetType.SEA,
        lat=record.lat,
        lon=record.lon,
        speed_knots=record.speed_knots,
        heading_deg=record.heading_deg,
        reported_at=record.timestamp,
        provenance=Provenance.REPLAYED,
        source=_SOURCE,
    )


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------


class ReplayFeedAdapter:
    """
    Historical AIS Parquet replay adapter.

    Satisfies the T-011 ``PositionFeedAdapter`` protocol and is a
    transparent drop-in replacement for ``AISStreamAdapter`` (T-026).

    Parameters
    ----------
    data_path:
        Path to a single ``.parquet`` file or a directory of Parquet files.
        Directories are loaded in lexicographic path order.
        Existence is validated at ``start()`` time, not at construction.
    mmsis:
        Non-empty collection of 9-digit MMSI strings.  Records for other
        MMSIs are discarded at load time to reduce memory usage.
    speed_multiplier:
        Virtual clock speed relative to real time.  ``1.0`` = real-time;
        ``60.0`` (default) = 60 historical seconds per real second.
        Must be a finite positive number.
    start_at:
        Optional timezone-aware UTC timestamp at which replay begins.
        Defaults to the earliest timestamp in the filtered dataset.
    loop:
        When True, replay restarts seamlessly after the final record.
        Default False.
    tick_interval_s:
        Real-time interval between virtual-clock ticks.  Default 0.1 s.
    sleep_fn:
        Async sleep callable.  Default ``asyncio.sleep``.
        Tests inject a no-wait substitute.
    monotonic_fn:
        Monotonic real-time source in seconds.  Default ``time.monotonic``.
        Tests inject a controllable fake.
    now_fn:
        Wall-clock UTC source used for ``FeedHealth.last_success_at``.
        Default ``lambda: datetime.now(timezone.utc)``.
    record_loader:
        Record loader seam.  Default ``_load_records_from_parquet``.
        Tests inject a fixture function so no file is read from disk.

    Raises
    ------
    ValueError
        At construction: empty/malformed MMSIs, non-positive speed
        multiplier, naive start_at, non-positive tick_interval_s.
    """

    def __init__(
        self,
        data_path: str | Path,
        mmsis: Sequence[str] | set[str],
        *,
        speed_multiplier: float = _DEFAULT_SPEED_MULTIPLIER,
        start_at: datetime | None = None,
        loop: bool = False,
        tick_interval_s: float = _DEFAULT_TICK_INTERVAL_S,
        sleep_fn: AsyncSleepFn | None = None,
        monotonic_fn: MonotonicFn | None = None,
        now_fn: NowFn | None = None,
        record_loader: RecordLoaderFn | None = None,
    ) -> None:
        # ── Validate all cheap/local parameters at construction time ───────
        self._data_path: Path = Path(data_path)
        self._mmsis: frozenset[str] = _normalise_mmsi_set(mmsis)
        self._speed: float = _validate_speed_multiplier(speed_multiplier)
        self._speed_multiplier: float = self._speed
        self._configured_start_at: datetime | None = _validate_start_at(start_at)
        self._loop: bool = bool(loop)

        if tick_interval_s <= 0.0:
            raise ValueError(f"tick_interval_s must be > 0, got {tick_interval_s!r}.")
        self._tick_interval_s: float = float(tick_interval_s)

        # ── Injectable seams ───────────────────────────────────────────────
        self._sleep_fn: AsyncSleepFn = sleep_fn or asyncio.sleep
        self._monotonic_fn: MonotonicFn = monotonic_fn or time.monotonic
        self._now_fn: NowFn = now_fn or (lambda: datetime.now(UTC))
        self._record_loader: RecordLoaderFn = record_loader or _load_records_from_parquet

        # ── All mutable runtime state ──────────────────────────────────────
        self._state: _ReplayState = _ReplayState()

        # Guards cache and lifecycle state mutations.
        # Never held across an await of the tick sleep or network I/O.
        self._lock: asyncio.Lock = asyncio.Lock()

    # ──────────────────────────────────────────────────────────────────────
    # PositionFeedAdapter protocol — required public interface
    # ──────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Load the replay dataset and start the background tick-loop task.

        Idempotent: a second call on a running adapter is a safe no-op and
        does not create a duplicate task.

        Raises
        ------
        FileNotFoundError
            When data_path does not exist or a directory has no Parquet files.
        ValueError
            When the loaded dataset has no records for any configured MMSI,
            or required columns are missing from the Parquet schema.
        """
        async with self._lock:
            if self._state.is_running:
                logger.debug("ReplayFeedAdapter.start() called on a running adapter — no-op.")
                return

            logger.info(
                "ReplayFeedAdapter starting: path=%s  mmsis=%d  speed=%.1fx  loop=%s",
                self._data_path,
                len(self._mmsis),
                self._speed,
                self._loop,
            )

            # Load synchronously so failures surface to the caller immediately
            # rather than dying silently inside the background task.
            timeline = self._load_timeline()

            # Determine replay start.
            virtual_start = self._configured_start_at or timeline[0].timestamp

            self._state.timeline = timeline
            self._state.cursor = 0
            self._state.is_exhausted = False
            self._state.virtual_anchor = virtual_start
            self._state.virtual_now = virtual_start
            self._state.real_anchor = self._monotonic_fn()
            self._state.is_running = True
            self._state.is_healthy = True

            logger.info(
                "ReplayFeedAdapter loaded %d record(s) for %d MMSI(s); "
                "virtual clock starts at %s.",
                len(timeline),
                len(self._mmsis),
                virtual_start.isoformat(),
            )

            self._state._task = asyncio.create_task(
                self._tick_loop(),
                name="replay-ais-tick-loop",
            )

    async def stop(self) -> None:
        """
        Stop the tick-loop task and release the loaded dataset.

        Idempotent: safe before ``start()`` and safe to call repeatedly.
        No reconnect logic — stopping this adapter is permanent until
        ``start()`` is called again.
        """
        async with self._lock:
            if not self._state.is_running and self._state._task is None:
                logger.debug("ReplayFeedAdapter.stop() called on a stopped adapter — no-op.")
                return

            logger.info("ReplayFeedAdapter stopping.")
            self._state.is_running = False
            self._state.is_healthy = False

        task = self._state._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # Expected; intentional cancellation from stop().

        async with self._lock:
            self._state._task = None
            self._state.timeline = []  # release memory

        logger.info("ReplayFeedAdapter stopped.")

    async def get_current_positions(self) -> list[AssetPosition]:
        """
        Return a stable snapshot of the latest replayed position per vessel.

        - Sorted by ``asset_id`` (MMSI) for deterministic ordering.
        - Returns a new list on every call; the internal cache dict is never
          exposed.
        - When replay is exhausted the last-known positions continue to be
          returned until the adapter is stopped.
        - Returns an empty list before ``start()`` or after ``stop()``.
        """
        async with self._lock:
            snapshot = dict(self._state.cache)
        return sorted(snapshot.values(), key=lambda p: p.asset_id)

    async def health(self) -> FeedHealth:
        """
        Return an immutable snapshot of current feed health.

        See the module docstring for the full health policy, including the
        rationale for reporting is_healthy=True when replay is exhausted.
        """
        async with self._lock:
            return FeedHealth(
                adapter_name=ADAPTER_NAME,
                is_healthy=self._state.is_healthy,
                last_success_at=self._state.last_success_at,
                messages_received=self._state.messages_received,
                provenance=Provenance.REPLAYED,
            )

    async def get_health(self) -> FeedHealth:
        """Compatibility method alias for health()."""
        return await self.health()

    # ──────────────────────────────────────────────────────────────────────
    # Introspection properties (not part of the protocol)
    # ──────────────────────────────────────────────────────────────────────

    @property
    def is_exhausted(self) -> bool:
        """True once every record has been served and loop=False."""
        return self._state.is_exhausted

    @property
    def virtual_now(self) -> datetime | None:
        """Current virtual clock value; None before ``start()``."""
        return self._state.virtual_now

    # ──────────────────────────────────────────────────────────────────────
    # Internal: data loading
    # ──────────────────────────────────────────────────────────────────────

    def _load_timeline(self) -> list[ReplayRecord]:
        """
        Invoke the record loader and validate the result.

        Sorts the final list by (timestamp, mmsi) for global playback order.
        The loader itself sorts by (mmsi, timestamp); re-sorting here for
        playback order is cheap and explicit.

        Raises
        ------
        FileNotFoundError
            Propagated from the loader when the path is invalid.
        ValueError
            When the filtered dataset is empty.
        """
        try:
            records = self._record_loader(self._data_path, self._mmsis)
        except FileNotFoundError:
            logger.warning("ReplayFeedAdapter data path unavailable: %s", self._data_path)
            raise

        if not records:
            raise ValueError(
                f"AIS replay dataset at '{self._data_path}' contains no records "
                f"for any of the {len(self._mmsis)} configured MMSI(s).  "
                f"Refusing to start with an empty replay timeline."
            )

        # Sort by (timestamp, mmsi) for temporal playback order.
        return sorted(records, key=lambda r: (r.timestamp, r.mmsi))

    # ──────────────────────────────────────────────────────────────────────
    # Internal: virtual clock and tick loop
    # ──────────────────────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        """
        Background task: advance the virtual clock and serve due records.

        Exits cleanly on asyncio.CancelledError raised by stop().
        Unexpected exceptions are logged and cause the loop to exit
        (not retry), because a replay failure is deterministic.
        """
        try:
            while True:
                # Check for stop() between ticks without holding the lock
                # across the sleep.
                async with self._lock:
                    if not self._state.is_running:
                        return

                await self.tick()
                await self._sleep_fn(self._tick_interval_s)

        except asyncio.CancelledError:
            logger.debug("ReplayFeedAdapter tick loop cancelled (stop requested).")
            async with self._lock:
                self._state.is_healthy = False
            raise

        except Exception:
            logger.exception(
                "ReplayFeedAdapter tick loop encountered an unexpected error; " "replay halted."
            )
            async with self._lock:
                self._state.is_healthy = False

    async def tick(self) -> int:
        """
        Advance the virtual clock once and serve every newly due record.

        Exposed publicly so tests can drive replay deterministically without
        running the background task.

        Batching: a single tick may advance past many historical records.  At
        60x speed and a 0.1 s tick interval, each tick covers 6 virtual
        seconds.  All records in that window are served; only the last one per
        MMSI survives in the cache — the correct semantics for a latest-
        position feed.

        Returns
        -------
        int
            Number of records served during this tick (0 when exhausted or
            when is_running is False).
        """
        async with self._lock:
            if not self._state.is_running:
                return 0
            if self._state.virtual_anchor is None:
                return 0

            # ── Advance the virtual clock ──────────────────────────────────
            real_elapsed = self._monotonic_fn() - self._state.real_anchor
            if real_elapsed < 0.0:
                real_elapsed = 0.0  # defensive: monotonic should never go back
            virtual_elapsed_s = real_elapsed * self._speed
            virtual_now = self._state.virtual_anchor + timedelta(seconds=virtual_elapsed_s)
            self._state.virtual_now = virtual_now

            # ── Serve all records whose timestamp is now due ───────────────
            timeline = self._state.timeline
            served = 0

            while (
                self._state.cursor < len(timeline)
                and timeline[self._state.cursor].timestamp <= virtual_now
            ):
                record = timeline[self._state.cursor]
                self._state.cache[record.mmsi] = _record_to_position(record)
                self._state.cursor += 1
                self._state.messages_received += 1
                served += 1

            if served:
                self._state.last_success_at = self._now_fn()
                logger.debug(
                    "ReplayFeedAdapter served %d record(s); virtual=%s.",
                    served,
                    virtual_now.isoformat(),
                )

            # ── Handle end-of-timeline ─────────────────────────────────────
            if self._state.cursor >= len(timeline):
                if self._loop:
                    self._loop_reset_locked(virtual_now)
                elif not self._state.is_exhausted:
                    self._state.is_exhausted = True
                    logger.info(
                        "ReplayFeedAdapter replay exhausted after %d total "
                        "record(s); serving last-known positions.",
                        self._state.messages_received,
                    )

            return served

    def _loop_reset_locked(self, _virtual_now: datetime) -> None:
        """
        Re-anchor the virtual clock to loop the dataset seamlessly.

        Called with ``self._lock`` already held.

        The cursor resets to 0 and both anchors are re-captured at the
        current instant so the virtual clock resumes from the start of the
        historical data.  Callers observe no gap: data simply begins again
        from the earliest record.

        ``messages_received`` is deliberately NOT reset — the FeedHealth
        contract requires it to be monotonically increasing.
        ``is_exhausted`` stays False throughout.
        """
        tl = self._state.timeline
        if not tl:
            return

        self._state.cursor = 0
        self._state.virtual_anchor = tl[0].timestamp
        self._state.virtual_now = tl[0].timestamp
        self._state.real_anchor = self._monotonic_fn()
        # is_exhausted stays False — no exhaustion during looping.
        logger.debug(
            "ReplayFeedAdapter looped; virtual clock re-anchored to %s.",
            tl[0].timestamp.isoformat(),
        )
