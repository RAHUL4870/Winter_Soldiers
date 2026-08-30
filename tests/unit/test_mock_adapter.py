"""Unit tests for deterministic MockFeedAdapter."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from nexafreight.adapters.mock import MockFeedAdapter
from nexafreight.adapters.protocols import PositionFeedAdapter
from nexafreight.enums import Provenance, TransportMode

SEED_ASSETS: list[tuple[str, TransportMode, float, float]] = [
    ("VESSEL-001", TransportMode.SEA, 40.7128, -74.0060),
    ("TRUCK-042", TransportMode.ROAD, 34.0522, -118.2437),
    ("FLIGHT-777", TransportMode.AIR, 51.5074, -0.1278),
]

REFERENCE_TIME = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_determinism_across_instances() -> None:
    """Two adapters with identical config produce byte-for-byte identical output."""
    adapter1 = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)
    adapter2 = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)

    await adapter1.start()
    await adapter2.start()

    deltas = [timedelta(seconds=10), timedelta(seconds=30), timedelta(minutes=5)]

    for delta in deltas:
        adapter1.advance_simulated_time(delta)
        adapter2.advance_simulated_time(delta)

        positions1 = await adapter1.get_current_positions()
        positions2 = await adapter2.get_current_positions()

        assert positions1 == positions2


@pytest.mark.asyncio
async def test_provenance_is_always_mock() -> None:
    """Every produced position has provenance == Provenance.MOCK."""
    adapter = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)
    await adapter.start()

    for _ in range(5):
        adapter.advance_simulated_time(timedelta(seconds=15))
        positions = await adapter.get_current_positions()
        for position in positions:
            assert position.provenance == Provenance.MOCK


@pytest.mark.asyncio
async def test_asset_set_and_ranges_correct() -> None:
    """Returned positions match configured assets, valid ranges, increasing timestamps."""
    adapter = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)
    await adapter.start()

    expected_asset_ids = {asset_id for asset_id, _, _, _ in SEED_ASSETS}
    previous_timestamps: dict[str, datetime] = {}

    for _ in range(4):
        adapter.advance_simulated_time(timedelta(seconds=20))
        positions = await adapter.get_current_positions()

        returned_asset_ids = {p.asset_id for p in positions}
        assert returned_asset_ids == expected_asset_ids

        for position in positions:
            assert -90.0 <= position.latitude <= 90.0
            assert -180.0 <= position.longitude <= 180.0

            if position.asset_id in previous_timestamps:
                assert position.recorded_at > previous_timestamps[position.asset_id]
            previous_timestamps[position.asset_id] = position.recorded_at


@pytest.mark.asyncio
async def test_start_stop_idempotency() -> None:
    """start()/stop() are safe to call in any order, any number of times."""
    adapter = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)

    # stop() before start() must not raise
    await adapter.stop()

    # get_current_positions() before start() returns []
    positions = await adapter.get_current_positions()
    assert positions == []

    # start(), advance, capture positions
    await adapter.start()
    adapter.advance_simulated_time(timedelta(seconds=60))
    positions_after_first_advance = await adapter.get_current_positions()

    # second start() call must be a no-op — no reset of simulated time/state
    await adapter.start()
    positions_after_second_start = await adapter.get_current_positions()

    assert positions_after_first_advance == positions_after_second_start


@pytest.mark.asyncio
async def test_health_accuracy() -> None:
    """Health reporting reflects started/force_unhealthy state correctly."""
    adapter = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)

    health_before_start = await adapter.get_health()
    assert health_before_start.is_healthy is False
    assert health_before_start.last_success_at is None
    assert health_before_start.messages_received == 0

    await adapter.start()
    adapter.advance_simulated_time(timedelta(seconds=10))
    health_after_start = await adapter.get_health()

    assert health_after_start.is_healthy is True
    assert health_after_start.messages_received >= 1
    assert health_after_start.last_success_at == REFERENCE_TIME + timedelta(seconds=10)

    unhealthy_adapter = MockFeedAdapter(
        seed_assets=SEED_ASSETS,
        reference_time=REFERENCE_TIME,
        force_unhealthy=True,
    )
    await unhealthy_adapter.start()
    unhealthy_adapter.advance_simulated_time(timedelta(seconds=5))
    unhealthy_health = await unhealthy_adapter.get_health()
    assert unhealthy_health.is_healthy is False


def test_protocol_conformance() -> None:
    """MockFeedAdapter structurally satisfies the PositionFeedAdapter protocol."""
    adapter = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)
    assert isinstance(adapter, PositionFeedAdapter)


@pytest.mark.asyncio
async def test_no_wall_clock_dependency() -> None:
    """Mock output is unaffected by real elapsed wall-clock time between calls."""
    adapter1 = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)
    adapter2 = MockFeedAdapter(seed_assets=SEED_ASSETS, reference_time=REFERENCE_TIME)

    await adapter1.start()
    await adapter2.start()

    adapter1.advance_simulated_time(timedelta(seconds=42))
    positions1 = await adapter1.get_current_positions()

    # Real wall-clock delay — must have zero effect on adapter2's output
    time.sleep(0.05)

    adapter2.advance_simulated_time(timedelta(seconds=42))
    positions2 = await adapter2.get_current_positions()

    assert positions1 == positions2
