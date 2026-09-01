"""
Unit tests for T-027 — ReplayFeedAdapter.

Test-isolation guarantees
─────────────────────────
  File-free    No Parquet file is ever read from disk.  The record_loader
               seam is injected in every adapter test.  The single column-
               normalisation test that demonstrates in-memory pyarrow usage
               calls table.to_pylist() on an in-memory pa.Table — nothing is
               written to or read from any file.

  Network-free No HTTP or WebSocket clients are created.

  DB-free      No SQLAlchemy / aiosqlite / DuckDB imports.

  Time-free    FakeMonotonic and SleepRecorder eliminate all real waits.
               Full suite runs in < 0.5 s.

Public-API-only philosophy
──────────────────────────
Tests drive the adapter through its public interface (start, stop,
get_current_positions, health, tick).  Internal attributes are accessed only
when there is no public way to assert the same thing (e.g. cursor, timeline
length, _task).  Every such access is commented.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nexafreight.adapters.feed.replay_ais import (
    ADAPTER_NAME,
    ReplayFeedAdapter,
    ReplayRecord,
    _coerce_mmsi,
    _coerce_timestamp,
    _coords_valid,
    _norm_heading,
    _norm_speed,
    _normalise_mmsi_set,
    _validate_speed_multiplier,
    resolve_column_map,
    rows_to_records,
)
from nexafreight.adapters.protocols import (
    AssetPosition,
    AssetType,
    FeedHealth,
    PositionFeedAdapter,
    Provenance,
)

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

MMSI_A = "111111111"
MMSI_B = "222222222"
MMSI_C = "333333333"  # never in configured set

T0 = datetime(2024, 3, 1, 0, 0, 0, tzinfo=UTC)
FAKE_PATH = Path("/test/does/not/exist/ais.parquet")
WALL_NOW = datetime(2025, 7, 1, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fake clocks / sleeps
# ---------------------------------------------------------------------------


class FakeMonotonic:
    """Controllable monotonic clock.  Tests advance it explicitly."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


class SleepRecorder:
    """No-wait async sleep that records every delay requested."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)  # yield control without real waiting


def fixed_now() -> datetime:
    return WALL_NOW


# ---------------------------------------------------------------------------
# ReplayRecord construction helpers
# ---------------------------------------------------------------------------


def make_record(
    mmsi: str,
    offset_s: int = 0,
    lat: float = 10.0,
    lon: float = 20.0,
    speed: float = 12.0,
    heading: float = 90.0,
) -> ReplayRecord:
    """Build one ReplayRecord at T0 + offset_s seconds."""
    return ReplayRecord(
        mmsi=mmsi,
        timestamp=T0 + timedelta(seconds=offset_s),
        lat=lat,
        lon=lon,
        speed_knots=speed,
        heading_deg=heading,
    )


# ---------------------------------------------------------------------------
# Adapter construction helpers
# ---------------------------------------------------------------------------


def make_loader(records: list[ReplayRecord]) -> Any:
    """
    Return a record-loader seam that filters *records* to the configured set.

    This is the canonical way tests provide data without touching disk.
    """

    def _loader(path: Path, mmsis: frozenset[str]) -> list[ReplayRecord]:
        return [r for r in records if r.mmsi in mmsis]

    return _loader


def make_failing_loader(exc: Exception) -> Any:
    def _loader(path: Path, mmsis: frozenset[str]) -> list[ReplayRecord]:
        raise exc

    return _loader


def build_adapter(
    records: list[ReplayRecord],
    *,
    mmsis: list[str] | None = None,
    speed: float = 1.0,
    loop: bool = False,
    start_at: datetime | None = None,
    mono: FakeMonotonic | None = None,
    slp: SleepRecorder | None = None,
) -> tuple[ReplayFeedAdapter, FakeMonotonic, SleepRecorder]:
    """
    Construct a fully-seamed adapter and return it with its clock/sleep fakes.
    """
    _mono = mono or FakeMonotonic()
    _slp = slp or SleepRecorder()
    adapter = ReplayFeedAdapter(
        data_path=FAKE_PATH,
        mmsis=mmsis or [MMSI_A, MMSI_B],
        speed_multiplier=speed,
        start_at=start_at,
        loop=loop,
        sleep_fn=_slp,
        monotonic_fn=_mono,
        now_fn=fixed_now,
        record_loader=make_loader(records),
    )
    return adapter, _mono, _slp


async def start_manual(adapter: ReplayFeedAdapter) -> None:
    """
    Start the adapter and immediately cancel the background tick-loop so
    tests can drive tick() manually for full determinism.

    Restores is_healthy=True because cancelling the loop resets it to False
    via the CancelledError handler — we want the adapter in the running/
    healthy state for per-tick tests.
    """
    await adapter.start()
    task = adapter._state._task  # internal access: no public cancel API
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        adapter._state._task = None
    # Restore the healthy flag that start() established before the task ran.
    adapter._state.is_healthy = True


# ===========================================================================
# 1.  Protocol conformance
# ===========================================================================


class TestProtocolConformance:
    def test_satisfies_protocol_runtime_check(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        assert isinstance(adapter, PositionFeedAdapter)

    def test_all_protocol_methods_are_coroutines(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        assert asyncio.iscoroutinefunction(adapter.start)
        assert asyncio.iscoroutinefunction(adapter.stop)
        assert asyncio.iscoroutinefunction(adapter.get_current_positions)
        assert asyncio.iscoroutinefunction(adapter.health)


# ===========================================================================
# 2.  Data loading and MMSI filtering
# ===========================================================================


class TestDataLoading:
    @pytest.mark.asyncio
    async def test_only_configured_mmsis_loaded(self) -> None:
        records = [
            make_record(MMSI_A),
            make_record(MMSI_B),
            make_record(MMSI_C),
        ]
        adapter, _, _ = build_adapter(records, mmsis=[MMSI_A, MMSI_B])
        await start_manual(adapter)

        loaded = {r.mmsi for r in adapter._state.timeline}
        assert loaded == {MMSI_A, MMSI_B}
        assert MMSI_C not in loaded
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_timeline_sorted_by_timestamp(self) -> None:
        # Deliberately out of temporal order.
        records = [
            make_record(MMSI_A, offset_s=30),
            make_record(MMSI_B, offset_s=5),
            make_record(MMSI_A, offset_s=10),
        ]
        adapter, _, _ = build_adapter(records)
        await start_manual(adapter)

        timestamps = [r.timestamp for r in adapter._state.timeline]
        assert timestamps == sorted(timestamps)
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_empty_filtered_dataset_fails_at_start(self) -> None:
        # Only MMSI_C present; MMSI_A / MMSI_B are configured but absent.
        records = [make_record(MMSI_C)]
        adapter, _, _ = build_adapter(records, mmsis=[MMSI_A])
        with pytest.raises(ValueError, match="no records"):
            await adapter.start()

    @pytest.mark.asyncio
    async def test_missing_path_propagates_file_not_found(self) -> None:
        adapter = ReplayFeedAdapter(
            data_path=FAKE_PATH,
            mmsis=[MMSI_A],
            record_loader=make_failing_loader(FileNotFoundError("gone")),
            sleep_fn=SleepRecorder(),
            monotonic_fn=FakeMonotonic(),
            now_fn=fixed_now,
        )
        with pytest.raises(FileNotFoundError):
            await adapter.start()

    @pytest.mark.asyncio
    async def test_loader_called_only_at_start_not_at_import_or_init(
        self,
    ) -> None:
        """Parquet data must not be read before start()."""
        calls: list[str] = []

        def counting_loader(path: Path, mmsis: frozenset[str]) -> list[ReplayRecord]:
            calls.append("load")
            return [make_record(MMSI_A)]

        adapter = ReplayFeedAdapter(
            data_path=FAKE_PATH,
            mmsis=[MMSI_A],
            record_loader=counting_loader,
            sleep_fn=SleepRecorder(),
            monotonic_fn=FakeMonotonic(),
            now_fn=fixed_now,
        )
        assert calls == [], "Loader must not run at construction time"
        await start_manual(adapter)
        assert calls == ["load"], "Loader must run exactly once, at start()"
        await adapter.stop()


# ===========================================================================
# 3.  Column normalisation (in-memory pyarrow — no disk I/O)
# ===========================================================================


class TestColumnNormalisation:
    def test_resolve_column_map_handles_noaa_casing(self) -> None:
        cols = ["MMSI", "BaseDateTime", "LAT", "LON", "SOG", "COG", "Heading"]
        m = resolve_column_map(cols)
        assert m["mmsi"] == "MMSI"
        assert m["timestamp"] == "BaseDateTime"
        assert m["lat"] == "LAT"
        assert m["lon"] == "LON"
        assert m["sog"] == "SOG"
        assert m["cog"] == "COG"
        assert m["heading"] == "Heading"

    def test_resolve_column_map_handles_lowercase_aliases(self) -> None:
        cols = [
            "mmsi",
            "timestamp",
            "latitude",
            "longitude",
            "speed",
            "course",
        ]
        m = resolve_column_map(cols)
        assert m["lat"] == "latitude"
        assert m["lon"] == "longitude"
        assert m["sog"] == "speed"
        assert m["cog"] == "course"

    def test_rows_to_records_filters_and_sorts(self) -> None:
        rows = [
            {
                "MMSI": 222222222,
                "BaseDateTime": T0 + timedelta(seconds=5),
                "LAT": 1.0,
                "LON": 2.0,
                "SOG": 5.0,
                "Heading": 10,
            },
            {
                "MMSI": 111111111,
                "BaseDateTime": T0,
                "LAT": 3.0,
                "LON": 4.0,
                "SOG": 6.0,
                "Heading": 20,
            },
            {
                "MMSI": 333333333,
                "BaseDateTime": T0,
                "LAT": 5.0,
                "LON": 6.0,
                "SOG": 7.0,
                "Heading": 30,
            },
        ]
        m = resolve_column_map(list(rows[0].keys()))
        result = rows_to_records(rows, m, frozenset([MMSI_A, MMSI_B]))
        # Only configured MMSIs, sorted by (mmsi, timestamp) ascending.
        assert [r.mmsi for r in result] == [MMSI_A, MMSI_B]

    def test_rows_to_records_missing_required_column_raises(self) -> None:
        rows = [{"MMSI": 111111111, "LAT": 1.0}]  # no timestamp, no lon
        m = resolve_column_map(list(rows[0].keys()))
        with pytest.raises(ValueError, match="missing required AIS column"):
            rows_to_records(rows, m, frozenset([MMSI_A]))

    def test_in_memory_pyarrow_table_roundtrip(self) -> None:
        """
        Build a pyarrow Table in memory, call to_pylist(), normalise it.
        No file is written to or read from disk.
        """
        pa = pytest.importorskip("pyarrow")
        table = pa.table(
            {
                "MMSI": [111111111, 222222222, 333333333],
                "BaseDateTime": [
                    T0,
                    T0 + timedelta(seconds=1),
                    T0,
                ],
                "LAT": [10.0, 11.0, 12.0],
                "LON": [20.0, 21.0, 22.0],
                "SOG": [8.0, 9.0, 10.0],
                "Heading": [45, 511, 90],
            }
        )
        m = resolve_column_map(table.column_names)
        records = rows_to_records(table.to_pylist(), m, frozenset([MMSI_A, MMSI_B]))
        assert len(records) == 2
        by_mmsi = {r.mmsi: r for r in records}
        assert by_mmsi[MMSI_A].heading_deg == pytest.approx(45.0)
        assert by_mmsi[MMSI_B].heading_deg is None  # 511 = unavailable


# ===========================================================================
# 4.  Virtual clock advancement and position serving
# ===========================================================================


class TestVirtualClock:
    @pytest.mark.asyncio
    async def test_first_record_served_at_zero_elapsed(self) -> None:
        """
        The timeline starts at T0.  At zero real elapsed time the first
        record (also at T0) is due.
        """
        records = [make_record(MMSI_A, offset_s=0)]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter)

        # mono has not advanced; real_elapsed = 0 → virtual_now = T0.
        served = await adapter.tick()
        assert served == 1
        positions = await adapter.get_current_positions()
        assert len(positions) == 1
        assert positions[0].asset_id == MMSI_A
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_future_record_not_served_before_clock_reaches_it(
        self,
    ) -> None:
        records = [
            make_record(MMSI_A, offset_s=0),
            make_record(MMSI_A, offset_s=100),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter)

        mono.advance(5.0)  # 5 real seconds → 5 virtual seconds → T0 + 5 s
        await adapter.tick()

        # Cursor should be 1 (first record served; second at T0+100 not yet due).
        assert adapter._state.cursor == 1  # internal: only way to check
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_clock_advance_serves_records_in_window(self) -> None:
        records = [
            make_record(MMSI_A, offset_s=0),
            make_record(MMSI_A, offset_s=10),
            make_record(MMSI_A, offset_s=200),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter)

        mono.advance(50.0)  # virtual window covers T0 and T0+10
        served = await adapter.tick()
        assert served == 2

        positions = await adapter.get_current_positions()
        assert len(positions) == 1
        # The cache holds the LATEST position — the T0+10 record.
        assert positions[0].lat == pytest.approx(make_record(MMSI_A, offset_s=10).lat)
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_single_tick_batches_all_records_in_large_jump(self) -> None:
        """A large clock jump must serve every record it passes in one tick."""
        records = [make_record(MMSI_A, offset_s=i) for i in range(50)]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter)

        mono.advance(10_000.0)
        served = await adapter.tick()
        assert served == 50
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_virtual_now_matches_formula(self) -> None:
        """virtual_now = T0 + elapsed_real × speed."""
        records = [make_record(MMSI_A, offset_s=0)]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=60.0)
        await start_manual(adapter)

        mono.advance(10.0)  # 10 real seconds at 60x → 600 virtual seconds
        await adapter.tick()

        expected = T0 + timedelta(seconds=600.0)
        assert adapter.virtual_now == expected
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_virtual_now_none_before_start(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        assert adapter.virtual_now is None


# ===========================================================================
# 5.  Speed multiplier correctness
# ===========================================================================


class TestSpeedMultiplier:
    @pytest.mark.asyncio
    async def test_higher_speed_serves_more_records_per_real_delta(
        self,
    ) -> None:
        records = [make_record(MMSI_A, offset_s=i * 10) for i in range(20)]

        # 1x speed, 10 real seconds → virtual window = 10 s → 1 record due.
        adapter_1x, mono_1x, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter_1x)
        mono_1x.advance(10.0)
        await adapter_1x.tick()
        count_1x = adapter_1x._state.cursor
        await adapter_1x.stop()

        # 10x speed, 10 real seconds → virtual window = 100 s → 10 records due.
        adapter_10x, mono_10x, _ = build_adapter(records, mmsis=[MMSI_A], speed=10.0)
        await start_manual(adapter_10x)
        mono_10x.advance(10.0)
        await adapter_10x.tick()
        count_10x = adapter_10x._state.cursor
        await adapter_10x.stop()

        assert count_10x > count_1x

    @pytest.mark.asyncio
    async def test_speed_multiplier_scales_linearly(self) -> None:
        """60x / 10 real seconds → exactly 600 virtual seconds elapsed."""
        records = [make_record(MMSI_A, offset_s=0)]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=60.0)
        await start_manual(adapter)
        mono.advance(10.0)
        await adapter.tick()
        assert adapter.virtual_now == T0 + timedelta(seconds=600)
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_realtime_multiplier_one(self) -> None:
        records = [
            make_record(MMSI_A, offset_s=0),
            make_record(MMSI_A, offset_s=5),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter)
        mono.advance(5.0)
        await adapter.tick()
        assert adapter.virtual_now == T0 + timedelta(seconds=5)
        await adapter.stop()


# ===========================================================================
# 6.  Cache behaviour
# ===========================================================================


class TestCacheBehaviour:
    @pytest.mark.asyncio
    async def test_later_record_replaces_earlier_for_same_mmsi(self) -> None:
        records = [
            make_record(MMSI_A, offset_s=0, lat=1.0),
            make_record(MMSI_A, offset_s=10, lat=2.0),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter)
        mono.advance(100.0)
        await adapter.tick()

        positions = await adapter.get_current_positions()
        assert len(positions) == 1
        assert positions[0].lat == pytest.approx(2.0)
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_get_current_positions_returns_new_list_each_call(
        self,
    ) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)], mmsis=[MMSI_A])
        await start_manual(adapter)
        await adapter.tick()

        snap1 = await adapter.get_current_positions()
        snap2 = await adapter.get_current_positions()
        assert snap1 is not snap2
        assert snap1 == snap2
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_positions_sorted_by_asset_id(self) -> None:
        """get_current_positions() must return a deterministic order."""
        records = [
            make_record(MMSI_B, offset_s=0),  # feed B first
            make_record(MMSI_A, offset_s=0),
        ]
        adapter, mono, _ = build_adapter(records, speed=1.0)
        await start_manual(adapter)
        mono.advance(100.0)
        await adapter.tick()

        positions = await adapter.get_current_positions()
        ids = [p.asset_id for p in positions]
        assert ids == sorted(ids)
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_unconfigured_mmsi_never_in_cache(self) -> None:
        records = [make_record(MMSI_A), make_record(MMSI_C)]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter)
        mono.advance(100.0)
        await adapter.tick()

        positions = await adapter.get_current_positions()
        assert MMSI_C not in {p.asset_id for p in positions}
        await adapter.stop()


# ===========================================================================
# 7.  Provenance and asset type
# ===========================================================================


class TestProvenanceAndAssetType:
    @pytest.mark.asyncio
    async def test_every_position_is_replayed_and_sea(self) -> None:
        records = [make_record(MMSI_A), make_record(MMSI_B, offset_s=1)]
        adapter, mono, _ = build_adapter(records, speed=1.0)
        await start_manual(adapter)
        mono.advance(100.0)
        await adapter.tick()

        positions = await adapter.get_current_positions()
        assert len(positions) == 2
        for pos in positions:
            assert pos.provenance == Provenance.REPLAYED
            assert pos.asset_type == AssetType.SEA
            # Explicitly NOT these labels:
            assert pos.provenance is not Provenance.REAL
            assert pos.provenance is not Provenance.SIMULATED
            assert pos.provenance is not Provenance.MOCK
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_health_provenance_is_replayed(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        h = await adapter.health()
        assert h.provenance == Provenance.REPLAYED

    @pytest.mark.asyncio
    async def test_reported_at_is_historical_timestamp_not_wall_clock(
        self,
    ) -> None:
        """reported_at must carry the historical AIS timestamp, not now_fn()."""
        adapter, _, _ = build_adapter([make_record(MMSI_A, offset_s=0)], mmsis=[MMSI_A])
        await start_manual(adapter)
        await adapter.tick()

        positions = await adapter.get_current_positions()
        assert positions[0].reported_at == T0  # historical timestamp
        assert positions[0].reported_at != WALL_NOW  # not the injected wall clock
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_source_label(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)], mmsis=[MMSI_A])
        await start_manual(adapter)
        await adapter.tick()
        positions = await adapter.get_current_positions()
        assert positions[0].source == "AIS_PARQUET_REPLAY"
        await adapter.stop()


# ===========================================================================
# 8.  Replay exhaustion without looping
# ===========================================================================


class TestExhaustion:
    @pytest.mark.asyncio
    async def test_enters_exhausted_state_after_all_records_served(self) -> None:
        records = [
            make_record(MMSI_A, offset_s=0),
            make_record(MMSI_A, offset_s=5),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0, loop=False)
        await start_manual(adapter)
        assert adapter.is_exhausted is False

        mono.advance(10_000.0)
        await adapter.tick()
        assert adapter.is_exhausted is True
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_last_known_positions_still_returned_when_exhausted(
        self,
    ) -> None:
        records = [make_record(MMSI_A, lat=5.0), make_record(MMSI_B, lat=6.0)]
        adapter, mono, _ = build_adapter(records, speed=1.0, loop=False)
        await start_manual(adapter)
        mono.advance(10_000.0)
        await adapter.tick()

        # Extra ticks after exhaustion must not empty the cache.
        await adapter.tick()
        await adapter.tick()
        positions = await adapter.get_current_positions()
        assert len(positions) == 2  # not empty
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_exhausted_adapter_is_healthy_per_policy(self) -> None:
        """Policy: exhausted-but-serving → is_healthy=True (see module docstring)."""
        records = [make_record(MMSI_A)]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0, loop=False)
        await start_manual(adapter)
        mono.advance(10_000.0)
        await adapter.tick()

        assert adapter.is_exhausted is True
        h = await adapter.health()
        assert h.is_healthy is True
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_messages_received_does_not_grow_after_exhaustion(
        self,
    ) -> None:
        records = [
            make_record(MMSI_A, offset_s=0),
            make_record(MMSI_A, offset_s=5),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0, loop=False)
        await start_manual(adapter)
        mono.advance(10_000.0)
        await adapter.tick()
        count_at_exhaustion = (await adapter.health()).messages_received

        # More ticks should not serve new records.
        await adapter.tick()
        await adapter.tick()
        assert (await adapter.health()).messages_received == count_at_exhaustion
        await adapter.stop()


# ===========================================================================
# 9.  Loop behaviour
# ===========================================================================


class TestLoopBehaviour:
    @pytest.mark.asyncio
    async def test_cursor_resets_after_all_records_served_when_looping(
        self,
    ) -> None:
        records = [
            make_record(MMSI_A, offset_s=0),
            make_record(MMSI_A, offset_s=10),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0, loop=True)
        await start_manual(adapter)

        mono.advance(10_000.0)
        await adapter.tick()  # serves both, triggers loop reset
        assert adapter._state.cursor == 0  # internal: reset after loop
        assert adapter.is_exhausted is False
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_positions_reappear_after_loop_reset(self) -> None:
        records = [
            make_record(MMSI_A, offset_s=0, lat=1.0),
            make_record(MMSI_A, offset_s=10, lat=2.0),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0, loop=True)
        await start_manual(adapter)

        mono.advance(10_000.0)
        await adapter.tick()  # full pass + reset; cursor=0

        # Second pass: the first record is at virtual_anchor (exactly due).
        served = await adapter.tick()
        assert served >= 1
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_messages_received_monotonic_across_loops(self) -> None:
        records = [
            make_record(MMSI_A, offset_s=0),
            make_record(MMSI_A, offset_s=5),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0, loop=True)
        await start_manual(adapter)

        mono.advance(100_000.0)
        await adapter.tick()  # first full pass (2 records) + loop
        first = (await adapter.health()).messages_received
        assert first == 2

        mono.advance(100_000.0)
        await adapter.tick()  # second full pass (2 more records)
        second = (await adapter.health()).messages_received
        assert second == 4  # monotonic, never resets
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_loop_never_enters_exhausted_state(self) -> None:
        records = [make_record(MMSI_A, offset_s=0)]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0, loop=True)
        await start_manual(adapter)
        for _ in range(10):
            mono.advance(10_000.0)
            await adapter.tick()
            assert adapter.is_exhausted is False
        await adapter.stop()


# ===========================================================================
# 10.  Lifecycle idempotency
# ===========================================================================


class TestLifecycleIdempotency:
    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await adapter.stop()  # must not raise

    @pytest.mark.asyncio
    async def test_stop_twice_is_safe(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await adapter.start()
        await adapter.stop()
        await adapter.stop()  # second stop — must not raise or hang

    @pytest.mark.asyncio
    async def test_start_twice_does_not_create_second_task(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await adapter.start()
        task_id_1 = id(adapter._state._task)
        await adapter.start()  # idempotent
        task_id_2 = id(adapter._state._task)
        assert task_id_1 == task_id_2
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_tick_loop_and_clears_task(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await adapter.start()
        task = adapter._state._task
        assert task is not None and not task.done()
        await adapter.stop()
        assert task.done()
        assert adapter._state._task is None

    @pytest.mark.asyncio
    async def test_stop_releases_timeline_memory(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await adapter.start()
        await adapter.stop()
        assert adapter._state.timeline == []

    @pytest.mark.asyncio
    async def test_tick_after_stop_returns_zero(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await adapter.start()
        await adapter.stop()
        assert await adapter.tick() == 0

    @pytest.mark.asyncio
    async def test_tick_loop_uses_injected_sleep(self) -> None:
        slp = SleepRecorder()
        adapter, _, _ = build_adapter([make_record(MMSI_A)], slp=slp)
        await adapter.start()
        # Let the live tick loop run for a few iterations.
        for _ in range(6):
            await asyncio.sleep(0)
        await adapter.stop()
        assert len(slp.delays) > 0
        assert all(d == pytest.approx(0.1) for d in slp.delays)

    @pytest.mark.asyncio
    async def test_no_task_warning_after_clean_stop(self) -> None:
        """Stopping must not leave a pending asyncio Task."""
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await adapter.start()
        task = adapter._state._task
        await adapter.stop()
        # A cancelled-and-awaited task is in the 'done' state.
        assert task is not None and task.done()


# ===========================================================================
# 11.  Health accuracy
# ===========================================================================


class TestHealthAccuracy:
    @pytest.mark.asyncio
    async def test_health_before_start_is_unhealthy_with_zero_counts(
        self,
    ) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        h = await adapter.health()
        assert h.is_healthy is False
        assert h.last_success_at is None
        assert h.messages_received == 0
        assert h.adapter_name == ADAPTER_NAME

    @pytest.mark.asyncio
    async def test_health_after_start_before_tick_is_healthy(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await start_manual(adapter)
        h = await adapter.health()
        assert h.is_healthy is True
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_last_success_at_updated_on_serving_tick(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)], mmsis=[MMSI_A])
        await start_manual(adapter)
        await adapter.tick()

        h = await adapter.health()
        assert h.last_success_at == WALL_NOW
        assert h.messages_received == 1
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_last_success_at_not_updated_on_empty_tick(self) -> None:
        """A tick that serves zero records must not update last_success_at."""
        records = [make_record(MMSI_A, offset_s=1_000_000)]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0, start_at=T0)
        await start_manual(adapter)

        # Clock at T0; record is at T0 + 1,000,000 s — not yet due.
        await adapter.tick()

        h = await adapter.health()
        assert h.last_success_at is None
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_messages_received_counts_records_not_ticks(self) -> None:
        """One tick that serves 3 records → messages_received == 3."""
        records = [
            make_record(MMSI_A, offset_s=0),
            make_record(MMSI_A, offset_s=1),
            make_record(MMSI_A, offset_s=2),
        ]
        adapter, mono, _ = build_adapter(records, mmsis=[MMSI_A], speed=1.0)
        await start_manual(adapter)
        mono.advance(10.0)
        served = await adapter.tick()
        assert served == 3
        assert (await adapter.health()).messages_received == 3
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_health_after_stop_is_unhealthy(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        await start_manual(adapter)
        await adapter.tick()
        await adapter.stop()

        h = await adapter.health()
        assert h.is_healthy is False
        # Counters are preserved for post-mortem inspection.
        assert h.messages_received == 1

    @pytest.mark.asyncio
    async def test_health_dto_is_frozen(self) -> None:
        adapter, _, _ = build_adapter([make_record(MMSI_A)])
        h = await adapter.health()
        with pytest.raises((AttributeError, TypeError)):
            h.is_healthy = True  # type: ignore[misc]


# ===========================================================================
# 12.  Invalid configuration
# ===========================================================================


class TestInvalidConfiguration:
    def test_empty_mmsi_list_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            ReplayFeedAdapter(data_path=FAKE_PATH, mmsis=[])

    def test_malformed_mmsi_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid MMSI"):
            ReplayFeedAdapter(data_path=FAKE_PATH, mmsis=["not-an-mmsi"])

    def test_too_short_mmsi_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid MMSI"):
            ReplayFeedAdapter(data_path=FAKE_PATH, mmsis=["12345"])

    def test_zero_speed_multiplier_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ReplayFeedAdapter(data_path=FAKE_PATH, mmsis=[MMSI_A], speed_multiplier=0.0)

    def test_negative_speed_multiplier_raises(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            ReplayFeedAdapter(data_path=FAKE_PATH, mmsis=[MMSI_A], speed_multiplier=-1.0)

    def test_infinite_speed_multiplier_raises(self) -> None:
        with pytest.raises(ValueError):
            ReplayFeedAdapter(
                data_path=FAKE_PATH,
                mmsis=[MMSI_A],
                speed_multiplier=float("inf"),
            )

    def test_naive_start_at_raises(self) -> None:
        naive = datetime(2024, 1, 1, 0, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            ReplayFeedAdapter(data_path=FAKE_PATH, mmsis=[MMSI_A], start_at=naive)

    def test_zero_tick_interval_raises(self) -> None:
        with pytest.raises(ValueError, match="tick_interval_s"):
            ReplayFeedAdapter(data_path=FAKE_PATH, mmsis=[MMSI_A], tick_interval_s=0.0)

    def test_validate_speed_multiplier_accepts_valid_values(self) -> None:
        assert _validate_speed_multiplier(1) == pytest.approx(1.0)
        assert _validate_speed_multiplier(60.0) == pytest.approx(60.0)
        assert _validate_speed_multiplier(0.001) == pytest.approx(0.001)


# ===========================================================================
# 13.  AIS sentinel and coercion helpers (unit-level)
# ===========================================================================


class TestHelpers:
    # Speed
    def test_sog_sentinel_becomes_none(self) -> None:
        assert _norm_speed(102.3) is None

    def test_sog_above_sentinel_becomes_none(self) -> None:
        assert _norm_speed(200.0) is None

    def test_sog_valid_returned(self) -> None:
        assert _norm_speed(14.5) == pytest.approx(14.5)

    def test_sog_none_becomes_none(self) -> None:
        assert _norm_speed(None) is None

    def test_sog_non_numeric_becomes_none(self) -> None:
        assert _norm_speed("fast") is None

    def test_sog_negative_becomes_none(self) -> None:
        assert _norm_speed(-1.0) is None

    # Heading
    def test_heading_sentinel_511_becomes_none(self) -> None:
        assert _norm_heading(511, 360.0) is None

    def test_cog_sentinel_360_not_used_as_fallback(self) -> None:
        assert _norm_heading(511, 360.0) is None

    def test_cog_fallback_used_when_heading_511(self) -> None:
        assert _norm_heading(511, 275.0) == pytest.approx(275.0)

    def test_true_heading_preferred_over_cog(self) -> None:
        assert _norm_heading(180, 275.0) == pytest.approx(180.0)

    def test_both_absent_returns_none(self) -> None:
        assert _norm_heading(None, None) is None

    # Coordinates
    def test_valid_coordinates(self) -> None:
        assert _coords_valid(51.5, 0.1) is True

    def test_lat_sentinel_91_rejected(self) -> None:
        assert _coords_valid(91.0, 0.0) is False

    def test_lon_sentinel_181_rejected(self) -> None:
        assert _coords_valid(0.0, 181.0) is False

    def test_exact_boundary_lat_90_accepted(self) -> None:
        assert _coords_valid(90.0, 0.0) is True

    def test_exact_boundary_lon_180_accepted(self) -> None:
        assert _coords_valid(0.0, 180.0) is True

    # Timestamp coercion
    def test_naive_datetime_assumed_utc(self) -> None:
        naive = datetime(2024, 1, 1, 12, 0, 0)
        result = _coerce_timestamp(naive)
        assert result is not None
        assert result.tzinfo is UTC
        assert result.year == 2024

    def test_aware_datetime_converted_to_utc(self) -> None:
        import datetime as dt_mod

        offset = dt_mod.timezone(timedelta(hours=5))
        aware = datetime(2024, 1, 1, 17, 0, 0, tzinfo=offset)
        result = _coerce_timestamp(aware)
        assert result is not None
        assert result.tzinfo is UTC
        assert result.hour == 12  # 17:00+05 = 12:00 UTC

    def test_iso_string_parsed(self) -> None:
        result = _coerce_timestamp("2024-03-01T00:00:00+00:00")
        assert result == T0

    def test_none_returns_none(self) -> None:
        assert _coerce_timestamp(None) is None

    def test_invalid_string_returns_none(self) -> None:
        assert _coerce_timestamp("not-a-date") is None

    # MMSI coercion
    def test_integer_mmsi_zero_padded(self) -> None:
        assert _coerce_mmsi(1234) == "000001234"

    def test_nine_digit_integer_preserved(self) -> None:
        assert _coerce_mmsi(111111111) == "111111111"

    def test_none_mmsi_returns_none(self) -> None:
        assert _coerce_mmsi(None) is None

    def test_negative_mmsi_returns_none(self) -> None:
        assert _coerce_mmsi(-1) is None

    def test_string_mmsi_coerced(self) -> None:
        assert _coerce_mmsi("111111111") == "111111111"

    # MMSI set normalisation
    def test_normalise_mmsi_set_deduplicates(self) -> None:
        result = _normalise_mmsi_set([MMSI_A, MMSI_A, MMSI_A])
        assert len(result) == 1

    def test_normalise_mmsi_set_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            _normalise_mmsi_set([])


# ===========================================================================
# 14.  Interchangeability with AISStreamAdapter (T-026)
# ===========================================================================


class TestInterchangeability:
    @pytest.mark.asyncio
    async def test_both_adapters_satisfy_same_protocol(self) -> None:
        """
        Assign both adapters to a PositionFeedAdapter-typed variable and
        exercise the full protocol surface identically.  Neither branch
        needs to know which concrete type it holds.
        """
        from nexafreight.adapters.feed.aisstream import AISStreamAdapter

        # Minimal fake WebSocket so AISStreamAdapter needs no network.
        class _FakeWS:
            def __aiter__(self) -> _FakeWS:
                return self

            async def __anext__(self) -> str:
                raise StopAsyncIteration

            async def send(self, data: str) -> None:
                return None

        @asynccontextmanager  # type: ignore[arg-type]
        async def _fake_ws_factory(url: str, **kwargs: Any):
            yield _FakeWS()

        async def _no_sleep(d: float) -> None:
            await asyncio.sleep(0)

        replay_adapter, _, _ = build_adapter([make_record(MMSI_A)], mmsis=[MMSI_A])
        live_adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key="test-key-not-real",
            connection_factory=_fake_ws_factory,
            sleep_fn=_no_sleep,
        )

        adapters: list[PositionFeedAdapter] = [replay_adapter, live_adapter]

        for adapter in adapters:
            # Runtime protocol check.
            assert isinstance(adapter, PositionFeedAdapter)

            # get_current_positions() returns list[AssetPosition].
            positions = await adapter.get_current_positions()
            assert isinstance(positions, list)
            for pos in positions:
                assert isinstance(pos, AssetPosition)

            # health() returns FeedHealth.
            h = await adapter.health()
            assert isinstance(h, FeedHealth)
            assert isinstance(h.adapter_name, str)
            assert isinstance(h.is_healthy, bool)
            assert isinstance(h.messages_received, int)
            assert h.last_success_at is None or isinstance(h.last_success_at, datetime)

    @pytest.mark.asyncio
    async def test_adapters_carry_correct_distinct_provenance(self) -> None:
        """
        The only observable difference between the two adapters is provenance:
        REPLAYED for replay, REAL for live.  This is correct and expected.
        """
        from nexafreight.adapters.feed.aisstream import AISStreamAdapter

        class _FakeWS:
            def __aiter__(self) -> _FakeWS:
                return self

            async def __anext__(self) -> str:
                raise StopAsyncIteration

            async def send(self, data: str) -> None:
                return None

        @asynccontextmanager  # type: ignore[arg-type]
        async def _fake_ws_factory(url: str, **kwargs: Any):
            yield _FakeWS()

        async def _no_sleep(d: float) -> None:
            await asyncio.sleep(0)

        replay, _, _ = build_adapter([make_record(MMSI_A)], mmsis=[MMSI_A])
        live = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key="test-key",
            connection_factory=_fake_ws_factory,
            sleep_fn=_no_sleep,
        )

        assert (await replay.health()).provenance == Provenance.REPLAYED
        assert (await live.health()).provenance == Provenance.REAL
