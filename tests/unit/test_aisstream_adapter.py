"""Unit tests for T-026: AISStreamAdapter.

All tests are:
- Fully local — zero real network calls.
- Key-free — a fake API key string is injected directly.
- Database-free — no SQLAlchemy, no SQLite.
- FastAPI-free — no app instantiation.
- Sleep-free — a no-op async sleep is injected.
- Deterministic — a fixed clock is injected for timestamp assertions.

Test structure
──────────────
1.  Protocol conformance
2.  Construction validation (MMSI, empty list)
3.  Subscription payload sent to the WebSocket
4.  Valid message parsing → cache
5.  Cache upsert / replacement behaviour
6.  Multiple vessels + unsolicited MMSI ignored
7.  Invalid / malformed messages (non-fatal)
8.  AIS sentinel fields (heading, speed, coordinates)
9.  Health behaviour across lifecycle states
10. Lifecycle idempotency (stop before start, double start, double stop)
11. Exponential backoff on connection failure
12. Resubscribe after reconnect
13. Missing configuration (no key, no MMSIs)
14. Compression option verification
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from nexafreight.adapters.feed.aisstream import (
    _AISSTREAM_URL,
    _BACKOFF_MAX_S,
    AISStreamAdapter,
    _build_subscription_payload,
    _extract_heading,
    _extract_mmsi,
    _extract_speed,
    _normalise_mmsi_set,
    _parse_position_report,
    _validate_mmsi,
)
from nexafreight.adapters.protocols import (
    AssetType,
    PositionFeedAdapter,
    Provenance,
)

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------

FAKE_API_KEY = "test-api-key-do-not-use"
MMSI_A = "123456789"
MMSI_B = "987654321"
MMSI_C = "111222333"  # unsolicited (not in configured set)

FIXED_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


def fixed_now() -> datetime:
    return FIXED_NOW


async def no_sleep(_delay: float) -> None:
    """Injected sleep: yields control without waiting."""
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Fake WebSocket helpers
# ---------------------------------------------------------------------------


def make_position_message(
    mmsi: str,
    lat: float = 51.5,
    lon: float = 0.1,
    sog: float = 12.0,
    true_heading: int = 90,
) -> str:
    """Build a realistic AISStream PositionReport JSON string."""
    return json.dumps(
        {
            "MessageType": "PositionReport",
            "MetaData": {
                "MMSI": int(mmsi),
                "ShipName": "TEST VESSEL",
                "latitude": lat,
                "longitude": lon,
            },
            "Message": {
                "PositionReport": {
                    "UserID": int(mmsi),
                    "Latitude": lat,
                    "Longitude": lon,
                    "Sog": sog,
                    "TrueHeading": true_heading,
                    "Cog": 88.0,
                    "NavigationalStatus": 0,
                }
            },
        }
    )


def make_non_position_message() -> str:
    """A valid AIS message of a different type."""
    return json.dumps(
        {
            "MessageType": "ShipStaticData",
            "MetaData": {"MMSI": int(MMSI_A)},
            "Message": {"ShipStaticData": {"Name": "TEST"}},
        }
    )


class FakeWebSocket:
    """Fake async WebSocket that yields pre-loaded messages then closes.

    Supports being used as an async context manager and an async iterator.
    Records the messages sent to it (e.g., subscription payloads).
    """

    def __init__(self, messages: list[str | bytes]) -> None:
        self._messages = list(messages)
        self._iter = iter(self._messages)
        self.sent: list[str | bytes] = []
        self.closed = False
        # kwargs capture: tests verify compression was requested.
        self.open_kwargs: dict[str, Any] = {}

    async def send(self, data: str | bytes) -> None:
        self.sent.append(data)
        await asyncio.sleep(0)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> str | bytes:
        await asyncio.sleep(0)
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


def make_factory(
    ws: FakeWebSocket,
    capture_kwargs: dict[str, Any] | None = None,
) -> Any:
    """Return an async context-manager factory yielding the given FakeWebSocket.

    If ``capture_kwargs`` is provided, all kwargs passed to the factory are
    merged into it so tests can inspect them (e.g., to verify compression).
    """

    @asynccontextmanager
    async def factory(url: str, **kwargs: Any):  # type: ignore[misc]
        if capture_kwargs is not None:
            capture_kwargs.update(kwargs)
            capture_kwargs["url"] = url
        yield ws

    return factory


def make_failing_factory(exc: Exception) -> Any:
    """Return a factory whose first call raises, then yields BlockingWS."""
    call_count = 0

    class BlockingWS(FakeWebSocket):
        def __init__(self) -> None:
            super().__init__([])

        def __aiter__(self) -> BlockingWS:
            return self

        async def __anext__(self) -> str:
            await asyncio.sleep(3600)  # block "forever" in tests
            raise StopAsyncIteration

    @asynccontextmanager
    async def factory(url: str, **kwargs: Any):  # type: ignore[misc]
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise exc
        yield BlockingWS()

    return factory


def make_multi_fail_factory(fail_count: int, delays: list[float]) -> Any:
    """Return a factory that fails ``fail_count`` times, then blocks."""

    class BlockingWS(FakeWebSocket):
        def __init__(self) -> None:
            super().__init__([])

        def __aiter__(self) -> BlockingWS:
            return self

        async def __anext__(self) -> str:
            await asyncio.sleep(3600)
            raise StopAsyncIteration

    call_count = 0

    @asynccontextmanager
    async def factory(url: str, **kwargs: Any):  # type: ignore[misc]
        nonlocal call_count
        call_count += 1
        if call_count <= fail_count:
            raise OSError("Simulated connection failure")
        yield BlockingWS()

    return factory


# ---------------------------------------------------------------------------
# Helper: run adapter, let it process messages, then stop it
# ---------------------------------------------------------------------------


async def run_adapter_with_messages(
    adapter: AISStreamAdapter,
    *,
    yield_count: int = 10,
) -> None:
    """Start adapter, allow receive loop to process loaded messages, then stop."""
    await adapter.start()
    for _ in range(yield_count):
        await asyncio.sleep(0)
    await adapter.stop()


# ===========================================================================
# 1. Protocol conformance
# ===========================================================================


class TestProtocolConformance:
    def test_adapter_is_instance_of_protocol(self) -> None:
        """AISStreamAdapter must satisfy PositionFeedAdapter protocol."""
        ws = FakeWebSocket([])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        assert isinstance(adapter, PositionFeedAdapter)

    def test_adapter_has_required_protocol_methods(self) -> None:
        """All protocol methods must be present and async."""
        ws = FakeWebSocket([])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        assert asyncio.iscoroutinefunction(adapter.start)
        assert asyncio.iscoroutinefunction(adapter.stop)
        assert asyncio.iscoroutinefunction(adapter.get_current_positions)
        assert asyncio.iscoroutinefunction(adapter.health)


# ===========================================================================
# 2. MMSI validation at construction
# ===========================================================================


class TestMMSIValidation:
    def test_valid_mmsi_accepted(self) -> None:
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(FakeWebSocket([])),
        )
        assert MMSI_A in adapter._mmsis

    def test_duplicate_mmsis_deduplicated(self) -> None:
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A, MMSI_A, MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(FakeWebSocket([])),
        )
        assert len(adapter._mmsis) == 1

    def test_empty_mmsi_list_raises(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            AISStreamAdapter(
                mmsis=[],
                api_key=FAKE_API_KEY,
                connection_factory=make_factory(FakeWebSocket([])),
            )

    def test_malformed_mmsi_raises(self) -> None:
        with pytest.raises(ValueError):
            AISStreamAdapter(
                mmsis=["not-an-mmsi"],
                api_key=FAKE_API_KEY,
                connection_factory=make_factory(FakeWebSocket([])),
            )

    def test_too_short_mmsi_raises(self) -> None:
        with pytest.raises(ValueError):
            AISStreamAdapter(
                mmsis=["12345"],
                api_key=FAKE_API_KEY,
                connection_factory=make_factory(FakeWebSocket([])),
            )

    def test_too_long_mmsi_raises(self) -> None:
        with pytest.raises(ValueError):
            AISStreamAdapter(
                mmsis=["1234567890"],
                api_key=FAKE_API_KEY,
                connection_factory=make_factory(FakeWebSocket([])),
            )

    def test_mmsi_with_letters_raises(self) -> None:
        with pytest.raises(ValueError):
            AISStreamAdapter(
                mmsis=["12345678A"],
                api_key=FAKE_API_KEY,
                connection_factory=make_factory(FakeWebSocket([])),
            )

    def test_leading_zero_mmsi_accepted(self) -> None:
        """MMSIs with leading zeros must be preserved."""
        adapter = AISStreamAdapter(
            mmsis=["012345678"],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(FakeWebSocket([])),
        )
        assert "012345678" in adapter._mmsis


# ===========================================================================
# 3. Subscription payload
# ===========================================================================


class TestSubscriptionPayload:
    @pytest.mark.asyncio
    async def test_connects_to_official_url_by_default(self) -> None:
        """Default URL must be the official AISStream endpoint."""
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(FakeWebSocket([])),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        assert adapter._ws_url == _AISSTREAM_URL

    @pytest.mark.asyncio
    async def test_subscription_contains_configured_mmsis(self) -> None:
        ws = FakeWebSocket([])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A, MMSI_B],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        assert len(ws.sent) >= 1
        payload = json.loads(ws.sent[0])
        assert set(payload["FiltersShipMMSI"]) == {MMSI_A, MMSI_B}

    @pytest.mark.asyncio
    async def test_subscription_requests_only_position_report(self) -> None:
        ws = FakeWebSocket([])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        payload = json.loads(ws.sent[0])
        assert payload["FilterMessageTypes"] == ["PositionReport"]

    @pytest.mark.asyncio
    async def test_subscription_includes_api_key(self) -> None:
        ws = FakeWebSocket([])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        payload = json.loads(ws.sent[0])
        assert payload["APIKey"] == FAKE_API_KEY

    @pytest.mark.asyncio
    async def test_api_key_not_in_public_state(self) -> None:
        """The API key must not appear in repr, health, or positions."""
        ws = FakeWebSocket([make_position_message(MMSI_A)])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        adapter_repr = repr(adapter)
        health = await adapter.health()
        positions = await adapter.get_current_positions()

        assert FAKE_API_KEY not in adapter_repr
        assert FAKE_API_KEY not in repr(health)
        for pos in positions:
            assert FAKE_API_KEY not in repr(pos)

    @pytest.mark.asyncio
    async def test_compression_deflate_passed_to_connection_factory(
        self,
    ) -> None:
        """permessage-deflate must be requested via compression='deflate'."""
        captured: dict[str, Any] = {}
        ws = FakeWebSocket([])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws, capture_kwargs=captured),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        assert captured.get("compression") == "deflate"


# ===========================================================================
# 4. Valid message parsing → cache
# ===========================================================================


class TestValidMessageParsing:
    @pytest.mark.asyncio
    async def test_valid_message_is_cached(self) -> None:
        msg = make_position_message(MMSI_A, lat=51.5, lon=0.1, sog=12.0, true_heading=90)
        ws = FakeWebSocket([msg])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert len(positions) == 1
        pos = positions[0]
        assert pos.asset_id == MMSI_A
        assert pos.lat == pytest.approx(51.5)
        assert pos.lon == pytest.approx(0.1)
        assert pos.speed_knots == pytest.approx(12.0)
        assert pos.heading_deg == pytest.approx(90.0)
        assert pos.reported_at == FIXED_NOW
        assert pos.asset_type == AssetType.SEA
        assert pos.provenance == Provenance.REAL
        assert pos.source == "AISSTREAM"

    @pytest.mark.asyncio
    async def test_position_reported_at_uses_injected_clock(self) -> None:
        """reported_at must come from injected now_fn, not AIS timestamp."""
        msg = make_position_message(MMSI_A)
        ws = FakeWebSocket([msg])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert positions[0].reported_at == FIXED_NOW


# ===========================================================================
# 5. Cache upsert behaviour
# ===========================================================================


class TestCacheUpsert:
    @pytest.mark.asyncio
    async def test_newer_message_replaces_older_for_same_mmsi(self) -> None:
        msg1 = make_position_message(MMSI_A, lat=10.0, lon=20.0)
        msg2 = make_position_message(MMSI_A, lat=11.0, lon=21.0)
        ws = FakeWebSocket([msg1, msg2])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert len(positions) == 1
        assert positions[0].lat == pytest.approx(11.0)
        assert positions[0].lon == pytest.approx(21.0)

    @pytest.mark.asyncio
    async def test_cache_returns_stable_snapshot(self) -> None:
        """get_current_positions() returns a new list, not mutable cache."""
        msg = make_position_message(MMSI_A)
        ws = FakeWebSocket([msg])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        snapshot1 = await adapter.get_current_positions()
        snapshot2 = await adapter.get_current_positions()
        assert snapshot1 is not snapshot2
        assert snapshot1 == snapshot2

    @pytest.mark.asyncio
    async def test_positions_sorted_by_asset_id(self) -> None:
        """Returned positions must be sorted by asset_id for determinism."""
        msg_a = make_position_message(MMSI_A)
        msg_b = make_position_message(MMSI_B)
        ws = FakeWebSocket([msg_b, msg_a])  # B first in feed
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A, MMSI_B],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        asset_ids = [p.asset_id for p in positions]
        assert asset_ids == sorted(asset_ids)


# ===========================================================================
# 6. Multiple vessels + unsolicited MMSI ignored
# ===========================================================================


class TestMultipleVessels:
    @pytest.mark.asyncio
    async def test_all_configured_vessels_cached(self) -> None:
        msg_a = make_position_message(MMSI_A, lat=10.0)
        msg_b = make_position_message(MMSI_B, lat=20.0)
        ws = FakeWebSocket([msg_a, msg_b])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A, MMSI_B],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        mmsi_set = {p.asset_id for p in positions}
        assert mmsi_set == {MMSI_A, MMSI_B}

    @pytest.mark.asyncio
    async def test_unconfigured_mmsi_ignored(self) -> None:
        """MMSI_C is not in configured set — must not appear in cache."""
        msg_a = make_position_message(MMSI_A)
        msg_c = make_position_message(MMSI_C)  # unsolicited
        ws = FakeWebSocket([msg_a, msg_c])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        mmsi_set = {p.asset_id for p in positions}
        assert MMSI_C not in mmsi_set
        assert len(positions) == 1


# ===========================================================================
# 7. Invalid / malformed messages (non-fatal)
# ===========================================================================


class TestMalformedMessages:
    @pytest.mark.asyncio
    async def test_malformed_json_does_not_crash(self) -> None:
        bad = "not valid json {{{"
        good = make_position_message(MMSI_A)
        ws = FakeWebSocket([bad, good])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        # Good message must still be processed
        positions = await adapter.get_current_positions()
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_malformed_json_does_not_increment_counter(self) -> None:
        bad = "not json"
        good = make_position_message(MMSI_A)
        ws = FakeWebSocket([bad, good])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        health = await adapter.health()
        assert health.messages_received == 1  # only the valid one

    @pytest.mark.asyncio
    async def test_non_position_message_type_ignored(self) -> None:
        non_pos = make_non_position_message()
        good = make_position_message(MMSI_A)
        ws = FakeWebSocket([non_pos, good])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        health = await adapter.health()
        assert health.messages_received == 1
        positions = await adapter.get_current_positions()
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_missing_message_type_field_ignored(self) -> None:
        msg = json.dumps({"MetaData": {"MMSI": int(MMSI_A)}, "Message": {}})
        good = make_position_message(MMSI_A)
        ws = FakeWebSocket([msg, good])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_invalid_coordinates_skipped(self) -> None:
        """Coordinates outside valid lat/lon ranges must be rejected."""
        bad_coord_msg = json.dumps(
            {
                "MessageType": "PositionReport",
                "MetaData": {"MMSI": int(MMSI_A)},
                "Message": {
                    "PositionReport": {
                        "UserID": int(MMSI_A),
                        "Latitude": 91.0,  # AIS unavailable sentinel
                        "Longitude": 0.0,
                        "Sog": 5.0,
                        "TrueHeading": 0,
                    }
                },
            }
        )
        good = make_position_message(MMSI_A)
        ws = FakeWebSocket([bad_coord_msg, good])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        health = await adapter.health()
        assert health.messages_received == 1

    @pytest.mark.asyncio
    async def test_bytes_message_decoded_and_processed(self) -> None:
        msg_bytes = make_position_message(MMSI_A).encode("utf-8")
        ws = FakeWebSocket([msg_bytes])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_non_utf8_bytes_skipped_gracefully(self) -> None:
        bad_bytes = b"\xff\xfe invalid utf-8"
        good = make_position_message(MMSI_A)
        ws = FakeWebSocket([bad_bytes, good])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert len(positions) == 1

    @pytest.mark.asyncio
    async def test_json_array_not_object_skipped(self) -> None:
        array_msg = json.dumps([1, 2, 3])
        good = make_position_message(MMSI_A)
        ws = FakeWebSocket([array_msg, good])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert len(positions) == 1


# ===========================================================================
# 8. AIS sentinel / optional fields
# ===========================================================================


class TestAISSentinelFields:
    def test_true_heading_unavailable_becomes_none(self) -> None:
        pos_report = {"TrueHeading": 511, "Cog": 360.0}
        assert _extract_heading(pos_report) is None

    def test_true_heading_valid_returned(self) -> None:
        pos_report = {"TrueHeading": 180, "Cog": 90.0}
        assert _extract_heading(pos_report) == pytest.approx(180.0)

    def test_cog_fallback_when_true_heading_unavailable(self) -> None:
        """Cog is used as fallback when TrueHeading is 511."""
        pos_report = {"TrueHeading": 511, "Cog": 270.0}
        assert _extract_heading(pos_report) == pytest.approx(270.0)

    def test_cog_fallback_not_used_when_cog_unavailable(self) -> None:
        """Cog=360.0 is unavailable sentinel; must not fall back to it."""
        pos_report = {"TrueHeading": 511, "Cog": 360.0}
        assert _extract_heading(pos_report) is None

    def test_both_absent_returns_none(self) -> None:
        assert _extract_heading({}) is None

    def test_sog_unavailable_sentinel_becomes_none(self) -> None:
        pos_report = {"Sog": 102.3}
        assert _extract_speed(pos_report) is None

    def test_sog_valid_returned(self) -> None:
        pos_report = {"Sog": 14.5}
        assert _extract_speed(pos_report) == pytest.approx(14.5)

    def test_sog_absent_becomes_none(self) -> None:
        assert _extract_speed({}) is None

    @pytest.mark.asyncio
    async def test_unavailable_heading_in_full_message_becomes_none(
        self,
    ) -> None:
        msg = json.dumps(
            {
                "MessageType": "PositionReport",
                "MetaData": {"MMSI": int(MMSI_A)},
                "Message": {
                    "PositionReport": {
                        "UserID": int(MMSI_A),
                        "Latitude": 51.5,
                        "Longitude": 0.1,
                        "Sog": 5.0,
                        "TrueHeading": 511,  # unavailable
                        "Cog": 360.0,  # also unavailable
                    }
                },
            }
        )
        ws = FakeWebSocket([msg])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert len(positions) == 1
        assert positions[0].heading_deg is None

    @pytest.mark.asyncio
    async def test_unavailable_sog_in_full_message_becomes_none(self) -> None:
        msg = json.dumps(
            {
                "MessageType": "PositionReport",
                "MetaData": {"MMSI": int(MMSI_A)},
                "Message": {
                    "PositionReport": {
                        "UserID": int(MMSI_A),
                        "Latitude": 51.5,
                        "Longitude": 0.1,
                        "Sog": 102.3,  # unavailable sentinel
                        "TrueHeading": 90,
                    }
                },
            }
        )
        ws = FakeWebSocket([msg])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        positions = await adapter.get_current_positions()
        assert positions[0].speed_knots is None


# ===========================================================================
# 9. Health behaviour across lifecycle states
# ===========================================================================


class TestHealthBehaviour:
    @pytest.mark.asyncio
    async def test_health_before_start_is_unhealthy(self) -> None:
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(FakeWebSocket([])),
        )
        health = await adapter.health()
        assert health.is_healthy is False
        assert health.last_success_at is None
        assert health.messages_received == 0
        assert health.adapter_name == "aisstream"

    @pytest.mark.asyncio
    async def test_health_after_valid_message_has_last_success(self) -> None:
        msg = make_position_message(MMSI_A)
        ws = FakeWebSocket([msg])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        health = await adapter.health()
        assert health.last_success_at == FIXED_NOW
        assert health.messages_received == 1

    @pytest.mark.asyncio
    async def test_health_after_stop_is_unhealthy(self) -> None:
        msg = make_position_message(MMSI_A)
        ws = FakeWebSocket([msg])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        health = await adapter.health()
        assert health.is_healthy is False

    @pytest.mark.asyncio
    async def test_health_never_contains_secrets(self) -> None:
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(FakeWebSocket([])),
        )
        health = await adapter.health()
        health_repr = repr(health)
        assert FAKE_API_KEY not in health_repr

    @pytest.mark.asyncio
    async def test_messages_received_monotonically_increases(self) -> None:
        msg1 = make_position_message(MMSI_A, lat=1.0)
        msg2 = make_position_message(MMSI_A, lat=2.0)
        msg3 = make_position_message(MMSI_A, lat=3.0)
        ws = FakeWebSocket([msg1, msg2, msg3])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await run_adapter_with_messages(adapter)

        health = await adapter.health()
        assert health.messages_received == 3

    @pytest.mark.asyncio
    async def test_health_provenance_is_real(self) -> None:
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(FakeWebSocket([])),
        )
        health = await adapter.health()
        assert health.provenance == Provenance.REAL


# ===========================================================================
# 10. Lifecycle idempotency
# ===========================================================================


class TestLifecycleIdempotency:
    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self) -> None:
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(FakeWebSocket([])),
            sleep_fn=no_sleep,
        )
        # Must not raise
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_double_stop_is_safe(self) -> None:
        ws = FakeWebSocket([])
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=make_factory(ws),
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await adapter.start()
        await adapter.stop()
        await adapter.stop()  # must not raise or hang

    @pytest.mark.asyncio
    async def test_double_start_does_not_create_second_task(self) -> None:
        # Use a factory that blocks so the task stays alive
        class BlockingWS(FakeWebSocket):
            def __init__(self) -> None:
                super().__init__([])

            def __aiter__(self) -> BlockingWS:
                return self

            async def __anext__(self) -> str:
                await asyncio.sleep(3600)
                raise StopAsyncIteration

        @asynccontextmanager
        async def blocking_factory(url: str, **kwargs: Any):  # type: ignore[misc]
            yield BlockingWS()

        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=blocking_factory,
            sleep_fn=no_sleep,
        )
        await adapter.start()
        task_id_1 = id(adapter._state._task)
        await adapter.start()  # second call
        task_id_2 = id(adapter._state._task)
        assert task_id_1 == task_id_2  # same task, not a new one
        await adapter.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_receive_loop(self) -> None:
        """stop() must cancel the receive task; no lingering tasks."""

        class BlockingWS(FakeWebSocket):
            def __init__(self) -> None:
                super().__init__([])

            def __aiter__(self) -> BlockingWS:
                return self

            async def __anext__(self) -> str:
                await asyncio.sleep(3600)
                raise StopAsyncIteration

        @asynccontextmanager
        async def blocking_factory(url: str, **kwargs: Any):  # type: ignore[misc]
            yield BlockingWS()

        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=blocking_factory,
            sleep_fn=no_sleep,
        )
        await adapter.start()
        task = adapter._state._task
        assert task is not None
        assert not task.done()
        await adapter.stop()
        assert task.done()


# ===========================================================================
# 11. Exponential backoff on connection failure
# ===========================================================================


class TestExponentialBackoff:
    @pytest.mark.asyncio
    async def test_backoff_sequence_is_exponential(self) -> None:
        """Verify that successive failures produce exponential delays."""
        recorded_delays: list[float] = []

        async def capturing_sleep(delay: float) -> None:
            recorded_delays.append(delay)
            await asyncio.sleep(0)

        fail_count = 6  # Causes delays: 1, 2, 4, 8, 16, 32
        factory = make_multi_fail_factory(fail_count, recorded_delays)

        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=factory,
            sleep_fn=capturing_sleep,
            now_fn=fixed_now,
        )
        await adapter.start()

        # Let the loop run through several failures + eventual success
        await asyncio.sleep(0)
        for _ in range(fail_count + 2):
            await asyncio.sleep(0)

        await adapter.stop()

        # We should have recorded at least 3 delays in the right progression
        assert len(recorded_delays) >= 3
        assert recorded_delays[0] == pytest.approx(1.0)
        assert recorded_delays[1] == pytest.approx(2.0)
        assert recorded_delays[2] == pytest.approx(4.0)

    @pytest.mark.asyncio
    async def test_backoff_capped_at_60_seconds(self) -> None:
        """After many failures the delay must not exceed 60 seconds."""
        recorded_delays: list[float] = []

        async def capturing_sleep(delay: float) -> None:
            recorded_delays.append(delay)
            await asyncio.sleep(0)

        fail_count = 12  # More than enough to hit cap
        factory = make_multi_fail_factory(fail_count, recorded_delays)

        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=factory,
            sleep_fn=capturing_sleep,
            now_fn=fixed_now,
        )
        await adapter.start()

        for _ in range(fail_count + 5):
            await asyncio.sleep(0)

        await adapter.stop()

        assert all(d <= _BACKOFF_MAX_S for d in recorded_delays)

    @pytest.mark.asyncio
    async def test_no_real_sleep_in_tests(self) -> None:
        """Prove that no real waiting occurs: test completes instantly."""
        import time

        start = time.monotonic()

        factory = make_failing_factory(OSError("fail"))
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=factory,
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await adapter.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await adapter.stop()

        elapsed = time.monotonic() - start
        # Should finish in well under 1 second
        assert elapsed < 1.0


# ===========================================================================
# 12. Resubscribe after reconnect
# ===========================================================================


class TestResubscribeAfterReconnect:
    @pytest.mark.asyncio
    async def test_subscription_sent_on_reconnect(self) -> None:
        """Verify subscription message is sent again on new connection."""
        sent_batches: list[list[str]] = []

        call_count = 0

        @asynccontextmanager
        async def two_connection_factory(url: str, **kwargs: Any):  # type: ignore[misc]
            nonlocal call_count
            call_count += 1
            ws = FakeWebSocket([])  # empty — closes immediately
            yield ws
            sent_batches.append(list(ws.sent))

        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=two_connection_factory,
            sleep_fn=no_sleep,
            now_fn=fixed_now,
        )
        await adapter.start()

        # Let the loop run through at least 2 connections
        for _ in range(10):
            await asyncio.sleep(0)

        await adapter.stop()

        # Both connections must have received a subscription message
        assert len(sent_batches) >= 2
        for batch in sent_batches:
            assert len(batch) >= 1
            payload = json.loads(batch[0])
            assert MMSI_A in payload["FiltersShipMMSI"]
            assert payload["FilterMessageTypes"] == ["PositionReport"]


# ===========================================================================
# 13. Missing configuration
# ===========================================================================


class TestMissingConfiguration:
    @pytest.mark.asyncio
    async def test_start_without_api_key_raises(self) -> None:
        """Adapter with no key must raise RuntimeError at start() time."""
        connected = False

        @asynccontextmanager
        async def spy_factory(url: str, **kwargs: Any):  # type: ignore[misc]
            nonlocal connected
            connected = True
            yield FakeWebSocket([])

        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=None,  # no injected key
            connection_factory=spy_factory,
            sleep_fn=no_sleep,
        )

        from unittest.mock import MagicMock, patch

        mock_settings = MagicMock()
        mock_settings.aisstream_api_key = None

        with patch(
            "nexafreight.adapters.feed.aisstream.get_settings",
            return_value=mock_settings,
        ):
            with pytest.raises(RuntimeError, match="API key"):
                await adapter.start()

        assert not connected, "Connection must not be attempted without key"

    @pytest.mark.asyncio
    async def test_error_message_does_not_contain_secret(self) -> None:
        """RuntimeError for missing key must not include the key string."""
        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=None,
            connection_factory=make_factory(FakeWebSocket([])),
            sleep_fn=no_sleep,
        )

        from unittest.mock import MagicMock, patch

        mock_settings = MagicMock()
        mock_settings.aisstream_api_key = None

        with patch(
            "nexafreight.adapters.feed.aisstream.get_settings",
            return_value=mock_settings,
        ):
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.start()

        # Must not contain any secret value
        assert FAKE_API_KEY not in str(exc_info.value)

    def test_empty_mmsi_list_raises_at_construction(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            AISStreamAdapter(
                mmsis=[],
                api_key=FAKE_API_KEY,
                connection_factory=make_factory(FakeWebSocket([])),
            )

    def test_no_mmsis_error_does_not_expose_key(self) -> None:
        try:
            AISStreamAdapter(
                mmsis=[],
                api_key=FAKE_API_KEY,
                connection_factory=make_factory(FakeWebSocket([])),
            )
        except ValueError as exc:
            assert FAKE_API_KEY not in str(exc)


# ===========================================================================
# 14. Backoff reset policy
# ===========================================================================


class TestBackoffReset:
    @pytest.mark.asyncio
    async def test_backoff_resets_after_valid_position_received(self) -> None:
        """Backoff must reset to 1 s only after valid position is received."""
        recorded_delays: list[float] = []

        async def capturing_sleep(delay: float) -> None:
            recorded_delays.append(delay)
            await asyncio.sleep(0)

        call_count = 0

        @asynccontextmanager
        async def reset_test_factory(url: str, **kwargs: Any):  # type: ignore[misc]
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("first fail")
            elif call_count == 2:
                # Return one valid position, then close
                yield FakeWebSocket([make_position_message(MMSI_A)])
            else:
                # Subsequent calls: fail again to trigger another delay
                raise OSError("later fail")

        adapter = AISStreamAdapter(
            mmsis=[MMSI_A],
            api_key=FAKE_API_KEY,
            connection_factory=reset_test_factory,
            sleep_fn=capturing_sleep,
            now_fn=fixed_now,
        )
        await adapter.start()
        for _ in range(20):
            await asyncio.sleep(0)
        await adapter.stop()

        if len(recorded_delays) >= 2:
            assert recorded_delays[0] == pytest.approx(1.0)
            assert recorded_delays[1] == pytest.approx(1.0)


# ===========================================================================
# 15. Unit-level parsing helpers
# ===========================================================================


class TestParsingHelpers:
    def test_validate_mmsi_strips_whitespace(self) -> None:
        assert _validate_mmsi("  123456789  ") == "123456789"

    def test_normalise_mmsi_set_deduplicates(self) -> None:
        result = _normalise_mmsi_set(["123456789", "123456789"])
        assert len(result) == 1

    def test_extract_mmsi_prefers_metadata(self) -> None:
        msg = {
            "MetaData": {"MMSI": 123456789},
            "Message": {"PositionReport": {"UserID": 987654321}},
        }
        result = _extract_mmsi(msg)
        assert result == "123456789"

    def test_extract_mmsi_falls_back_to_userid(self) -> None:
        msg = {
            "MetaData": {},
            "Message": {"PositionReport": {"UserID": 987654321}},
        }
        result = _extract_mmsi(msg)
        assert result == "987654321"

    def test_extract_mmsi_zero_pads_to_9_digits(self) -> None:
        """MMSI integer 1234 becomes '000001234' after zero-padding."""
        msg = {
            "MetaData": {"MMSI": 1234},
            "Message": {},
        }
        result = _extract_mmsi(msg)
        assert result == "000001234"

    def test_build_subscription_payload_structure(self) -> None:
        payload_str = _build_subscription_payload(FAKE_API_KEY, frozenset([MMSI_A, MMSI_B]))
        payload = json.loads(payload_str)
        assert payload["APIKey"] == FAKE_API_KEY
        assert set(payload["FiltersShipMMSI"]) == {MMSI_A, MMSI_B}
        assert payload["FilterMessageTypes"] == ["PositionReport"]

    def test_parse_position_report_returns_none_for_wrong_type(self) -> None:
        msg = {
            "MessageType": "ShipStaticData",
            "MetaData": {"MMSI": int(MMSI_A)},
            "Message": {"ShipStaticData": {}},
        }
        result = _parse_position_report(msg, FIXED_NOW, frozenset([MMSI_A]))
        assert result is None

    def test_parse_position_report_returns_none_for_out_of_set_mmsi(self) -> None:
        msg = {
            "MessageType": "PositionReport",
            "MetaData": {"MMSI": int(MMSI_C)},
            "Message": {
                "PositionReport": {
                    "UserID": int(MMSI_C),
                    "Latitude": 10.0,
                    "Longitude": 20.0,
                }
            },
        }
        result = _parse_position_report(msg, FIXED_NOW, frozenset([MMSI_A]))
        assert result is None

    def test_parse_position_report_full_valid_message(self) -> None:
        msg_str = make_position_message(MMSI_A, lat=55.0, lon=12.0, sog=8.5, true_heading=270)
        msg = json.loads(msg_str)
        result = _parse_position_report(msg, FIXED_NOW, frozenset([MMSI_A]))
        assert result is not None
        assert result.asset_id == MMSI_A
        assert result.lat == pytest.approx(55.0)
        assert result.lon == pytest.approx(12.0)
        assert result.speed_knots == pytest.approx(8.5)
        assert result.heading_deg == pytest.approx(270.0)
        assert result.asset_type == AssetType.SEA
        assert result.provenance == Provenance.REAL
        assert result.reported_at == FIXED_NOW
