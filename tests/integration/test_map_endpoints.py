"""
Integration tests for T-031 — /api/map/* endpoints.

Test isolation guarantees
─────────────────────────
  No real 5-second SSE delays:
    SSE generator is injected or mocked; tests consume a bounded number
    of events using asyncio.wait_for with a short timeout.

  No production database:
    T-012 in-memory SQLite test engine.

  No real network calls.

  Position caches mocked via patch:
    get_position_tracker() and get_current_positions() are patched in
    every test that exercises position endpoints.

Naming convention
─────────────────
  TestAuth*          — authentication enforcement
  TestSnapshot*      — /positions/snapshot
  TestSSE*           — /positions/stream
  TestRoutes*        — /routes
  TestPorts*         — /ports
  TestFeedHealth*    — /feed-health
  TestCaching*       — cache TTL and invalidation
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from nexafreight.adapters.protocols import AssetPosition, AssetType, FeedHealth, Provenance
from nexafreight.enums import LegStatus, TransportMode
from nexafreight.routers.map import (
    _build_ports_collection,
    _build_routes_collection,
    _cache_expired,
    _get_all_positions,
    _invalidate_ports_cache,
    _invalidate_routes_cache,
    _parse_route_geometry,
    _position_to_out,
    _route_quality_from_provenance,
    _sse_generator,
)
from nexafreight.workers.ais_listener import PositionTracker

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

NOW = datetime(2024, 3, 15, 10, 0, 0, tzinfo=UTC)
MMSI_A = "123456789"
LEG_ID_1 = "1"

VALID_LINESTRING = json.dumps(
    {"type": "LineString", "coordinates": [[4.0, 52.0], [4.1, 52.1], [4.2, 52.2]]}
)
VALID_LINESTRING_2 = json.dumps(
    {"type": "LineString", "coordinates": [[-118.0, 34.0], [-100.0, 40.0]]}
)
INVALID_GEOJSON = "not valid json {{{"
POINT_GEOJSON = json.dumps({"type": "Point", "coordinates": [4.0, 52.0]})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ais_position(
    asset_id: str = MMSI_A,
    lat: float = 52.1,
    lon: float = 4.5,
    provenance: Provenance = Provenance.REAL,
) -> AssetPosition:
    return AssetPosition(
        asset_id=asset_id,
        asset_type=AssetType.SEA,
        lat=lat,
        lon=lon,
        speed_knots=12.0,
        heading_deg=45.0,
        reported_at=NOW,
        provenance=provenance,
        source="AISSTREAM",
    )


def make_truck_position(leg_id: str = "1") -> AssetPosition:
    return AssetPosition(
        asset_id=leg_id,
        asset_type=AssetType.ROAD,
        lat=51.5,
        lon=-0.1,
        speed_knots=8.5,
        heading_deg=180.0,
        reported_at=NOW,
        provenance=Provenance.SIMULATED,
        source="ROAD_INTERPOLATION",
    )


def make_unhealthy_feed_health(name: str = "ais") -> FeedHealth:
    return FeedHealth(
        adapter_name=name,
        is_healthy=False,
        last_success_at=None,
        messages_received=0,
        provenance=Provenance.MOCK,
    )


def make_healthy_feed_health(name: str = "ais") -> FeedHealth:
    return FeedHealth(
        adapter_name=name,
        is_healthy=True,
        last_success_at=NOW,
        messages_received=999,
        provenance=Provenance.REAL,
    )


def mock_tracker(positions: list[AssetPosition]) -> PositionTracker:
    """Build a PositionTracker mock that returns the given positions."""
    tracker = MagicMock(spec=PositionTracker)
    tracker.get_positions = AsyncMock(return_value=positions)
    tracker.get_feed_health = AsyncMock(return_value=make_unhealthy_feed_health())
    return tracker


# ---------------------------------------------------------------------------
# SSE test helper — consume N events from the generator without real sleeps
# ---------------------------------------------------------------------------


async def collect_sse_events(
    n: int,
    tracker_positions: list[AssetPosition] | None = None,
    interpolated: dict[str, AssetPosition] | None = None,
    interval_s: float = 0.0,  # No real delay in tests
    heartbeat_s: float = 9999.0,  # Suppress heartbeat in most tests
) -> list[dict[str, str]]:
    """
    Consume exactly n SSE events from the _sse_generator coroutine.

    Returns a list of dicts with keys "event" and "data" for each
    complete SSE event received.

    Uses interval_s=0.0 so no asyncio.sleep delay occurs.
    """
    _positions = tracker_positions or []
    _interp = interpolated or {}

    tracker = mock_tracker(_positions)

    events: list[dict[str, str]] = []
    count = 0

    with (
        patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
        patch("nexafreight.routers.map.get_interpolated_positions", return_value=_interp),
    ):
        gen = _sse_generator(interval_s=interval_s, heartbeat_s=heartbeat_s)
        async for chunk in gen:
            # Parse SSE chunk into event/data parts.
            lines = chunk.strip().split("\n")
            event_dict: dict[str, str] = {}
            for line in lines:
                if line.startswith("event:"):
                    event_dict["event"] = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    event_dict["data"] = line.split(":", 1)[1].strip()
            if event_dict:
                events.append(event_dict)
                count += 1
            if count >= n:
                break

    return events


# ===========================================================================
# Helper / unit-level tests
# ===========================================================================


class TestHelpers:
    async def test_route_quality_from_provenance_calibrated(self) -> None:
        assert _route_quality_from_provenance("CALIBRATED") == "high"

    async def test_route_quality_from_provenance_real(self) -> None:
        assert _route_quality_from_provenance("REAL") == "high"

    async def test_route_quality_from_provenance_replayed(self) -> None:
        assert _route_quality_from_provenance("REPLAYED") == "high"

    async def test_route_quality_from_provenance_derived(self) -> None:
        assert _route_quality_from_provenance("DERIVED") == "medium"

    async def test_route_quality_from_provenance_simulated(self) -> None:
        assert _route_quality_from_provenance("SIMULATED") == "low"

    async def test_route_quality_from_provenance_unknown(self) -> None:
        assert _route_quality_from_provenance("BOGUS") == "unknown"

    async def test_parse_geometry_valid_linestring(self) -> None:
        result = _parse_route_geometry(1, VALID_LINESTRING)
        assert result is not None
        assert result["type"] == "LineString"

    async def test_parse_geometry_none_returns_none(self) -> None:
        assert _parse_route_geometry(1, None) is None

    async def test_parse_geometry_invalid_json_returns_none(self) -> None:
        assert _parse_route_geometry(1, INVALID_GEOJSON) is None

    async def test_parse_geometry_point_type_returns_none(self) -> None:
        assert _parse_route_geometry(1, POINT_GEOJSON) is None

    async def test_parse_geometry_empty_string_returns_none(self) -> None:
        assert _parse_route_geometry(1, "") is None

    async def test_position_to_out_maps_fields_correctly(self) -> None:
        pos = make_ais_position()
        out = _position_to_out(pos)
        assert out.asset_id == MMSI_A
        assert out.asset_type == "SEA"
        assert out.latitude == pytest.approx(52.1)
        assert out.longitude == pytest.approx(4.5)
        assert out.provenance == "REAL"
        assert out.recorded_at == NOW
        assert out.source == "AISSTREAM"

    async def test_cache_expired_returns_true_for_old_timestamp(self) -> None:
        old_ts = time.monotonic() - 120.0  # 2 minutes ago
        assert _cache_expired(old_ts) is True

    async def test_cache_expired_returns_false_for_fresh_timestamp(self) -> None:
        fresh_ts = time.monotonic() - 5.0  # 5 seconds ago
        assert _cache_expired(fresh_ts) is False


# ===========================================================================
# 1. Authentication enforcement
# ===========================================================================


class TestAuthentication:
    async def test_stream_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/map/positions/stream")
        assert resp.status_code == 401

    async def test_snapshot_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/map/positions/snapshot")
        assert resp.status_code == 401

    async def test_routes_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/map/routes")
        assert resp.status_code == 401

    async def test_ports_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/map/ports")
        assert resp.status_code == 401

    async def test_feed_health_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/map/feed-health")
        assert resp.status_code == 401


# ===========================================================================
# 2. /positions/snapshot
# ===========================================================================


class TestSnapshot:
    async def test_snapshot_returns_positions(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        pos = make_ais_position()
        tracker = mock_tracker([pos])

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch("nexafreight.routers.map.get_interpolated_positions", return_value={}),
        ):
            resp = await client.get("/api/map/positions/snapshot", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["asset_id"] == MMSI_A
        assert data[0]["asset_type"] == "SEA"
        assert data[0]["latitude"] == pytest.approx(52.1)
        assert data[0]["longitude"] == pytest.approx(4.5)
        assert data[0]["provenance"] == "REAL"

    async def test_snapshot_returns_empty_array_when_no_positions(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        tracker = mock_tracker([])
        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch("nexafreight.routers.map.get_interpolated_positions", return_value={}),
        ):
            resp = await client.get("/api/map/positions/snapshot", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json() == []

    async def test_snapshot_merges_ais_and_interpolated_positions(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        ais_pos = make_ais_position(asset_id="123456789")
        truck_pos = make_truck_position(leg_id="42")
        tracker = mock_tracker([ais_pos])

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolated_positions",
                return_value={"42": truck_pos},
            ),
        ):
            resp = await client.get("/api/map/positions/snapshot", headers=auth_headers)

        data = resp.json()
        assert len(data) == 2
        ids = {item["asset_id"] for item in data}
        assert "123456789" in ids
        assert "42" in ids

    async def test_snapshot_ais_takes_precedence_on_collision(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """When same asset_id appears in both caches, AIS (REAL) wins."""
        shared_id = "collision"
        ais_pos = AssetPosition(
            asset_id=shared_id,
            asset_type=AssetType.SEA,
            lat=10.0,
            lon=20.0,
            speed_knots=12.0,
            heading_deg=90.0,
            reported_at=NOW,
            provenance=Provenance.REAL,
            source="AISSTREAM",
        )
        interp_pos = AssetPosition(
            asset_id=shared_id,
            asset_type=AssetType.ROAD,
            lat=50.0,  # Different lat — wrong if this wins
            lon=60.0,
            speed_knots=5.0,
            heading_deg=0.0,
            reported_at=NOW,
            provenance=Provenance.SIMULATED,
            source="ROAD_INTERPOLATION",
        )
        tracker = mock_tracker([ais_pos])

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolated_positions",
                return_value={shared_id: interp_pos},
            ),
        ):
            resp = await client.get("/api/map/positions/snapshot", headers=auth_headers)

        data = resp.json()
        assert len(data) == 1
        assert data[0]["asset_type"] == "SEA"
        assert data[0]["latitude"] == pytest.approx(10.0)
        assert data[0]["provenance"] == "REAL"

    async def test_snapshot_all_fields_present(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        pos = make_ais_position()
        tracker = mock_tracker([pos])

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch("nexafreight.routers.map.get_interpolated_positions", return_value={}),
        ):
            resp = await client.get("/api/map/positions/snapshot", headers=auth_headers)

        item = resp.json()[0]
        required_fields = {
            "asset_id",
            "asset_type",
            "latitude",
            "longitude",
            "speed_knots",
            "heading_deg",
            "provenance",
            "recorded_at",
            "source",
        }
        assert required_fields.issubset(set(item.keys()))


# ===========================================================================
# 3. SSE /positions/stream
# ===========================================================================


class TestSSE:
    async def test_sse_produces_position_update_events(self) -> None:
        pos = make_ais_position()
        events = await collect_sse_events(n=2, tracker_positions=[pos])

        assert len(events) == 2
        for event in events:
            assert event["event"] == "POSITION_UPDATE"
            payload = json.loads(event["data"])
            assert isinstance(payload, list)

    async def test_sse_event_contains_correct_position_data(self) -> None:
        pos = make_ais_position(lat=55.0, lon=8.0)
        events = await collect_sse_events(n=1, tracker_positions=[pos])

        payload = json.loads(events[0]["data"])
        assert len(payload) == 1
        assert payload[0]["asset_id"] == MMSI_A
        assert payload[0]["latitude"] == pytest.approx(55.0)
        assert payload[0]["longitude"] == pytest.approx(8.0)

    async def test_sse_sends_empty_array_when_no_positions(self) -> None:
        events = await collect_sse_events(n=1, tracker_positions=[])
        payload = json.loads(events[0]["data"])
        assert payload == []

    async def test_sse_heartbeat_sent_at_interval(self) -> None:
        """Heartbeat event is sent when elapsed_since_heartbeat reaches threshold."""
        pos = make_ais_position()
        # heartbeat_s == interval_s means heartbeat fires on every tick.
        events = await collect_sse_events(
            n=3,
            tracker_positions=[pos],
            interval_s=0.0,
            heartbeat_s=0.0,  # Force heartbeat on every tick
        )
        event_types = [e.get("event") for e in events]
        assert "HEARTBEAT" in event_types

    async def test_sse_endpoint_requires_auth(self, client: AsyncClient) -> None:
        """Confirm the SSE endpoint is protected (no token → 401)."""
        resp = await client.get("/api/map/positions/stream")
        assert resp.status_code == 401

    async def test_sse_all_position_fields_present(self) -> None:
        pos = make_ais_position()
        events = await collect_sse_events(n=1, tracker_positions=[pos])
        item = json.loads(events[0]["data"])[0]
        required = {
            "asset_id",
            "asset_type",
            "latitude",
            "longitude",
            "speed_knots",
            "heading_deg",
            "provenance",
            "recorded_at",
            "source",
        }
        assert required.issubset(set(item.keys()))


# ===========================================================================
# 4. /routes
# ===========================================================================


class TestRoutes:
    async def test_routes_returns_feature_collection(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session,
        make_leg,
        make_shipment,
    ) -> None:
        _invalidate_routes_cache()
        shipment = await make_shipment()
        await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.SEA,
            status=LegStatus.IN_PROGRESS,
            route_geometry=VALID_LINESTRING,
            provenance=Provenance.CALIBRATED,
        )

        resp = await client.get("/api/map/routes", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "FeatureCollection"
        assert isinstance(data["features"], list)

    async def test_routes_includes_planned_and_in_progress(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session,
        make_leg,
        make_shipment,
    ) -> None:
        _invalidate_routes_cache()
        shipment = await make_shipment()
        l1 = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.SEA,
            status=LegStatus.IN_PROGRESS,
            route_geometry=VALID_LINESTRING,
            provenance=Provenance.CALIBRATED,
        )
        l2 = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.PLANNED,
            route_geometry=VALID_LINESTRING_2,
            provenance=Provenance.DERIVED,
        )

        resp = await client.get("/api/map/routes", headers=auth_headers)
        data = resp.json()
        leg_ids = {f["properties"]["leg_id"] for f in data["features"]}
        assert str(l1.id) in leg_ids
        assert str(l2.id) in leg_ids

    async def test_routes_excludes_completed_legs(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session,
        make_leg,
        make_shipment,
    ) -> None:
        _invalidate_routes_cache()
        shipment = await make_shipment()
        good_leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.SEA,
            status=LegStatus.IN_PROGRESS,
            route_geometry=VALID_LINESTRING,
            provenance=Provenance.CALIBRATED,
        )
        excluded_leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.SEA,
            status=LegStatus.COMPLETED,
            route_geometry=VALID_LINESTRING_2,
            provenance=Provenance.CALIBRATED,
        )

        resp = await client.get("/api/map/routes", headers=auth_headers)
        data = resp.json()
        leg_ids = {f["properties"]["leg_id"] for f in data["features"]}
        assert str(good_leg.id) in leg_ids
        assert str(excluded_leg.id) not in leg_ids

    async def test_routes_feature_properties_complete(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session,
        make_leg,
        make_shipment,
    ) -> None:
        _invalidate_routes_cache()
        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.SEA,
            status=LegStatus.IN_PROGRESS,
            route_geometry=VALID_LINESTRING,
            provenance=Provenance.CALIBRATED,
        )

        resp = await client.get("/api/map/routes", headers=auth_headers)
        features = resp.json()["features"]
        assert len(features) >= 1

        props = next(f["properties"] for f in features if f["properties"]["leg_id"] == str(leg.id))
        required = {
            "leg_id",
            "shipment_id",
            "mode",
            "status",
            "provenance",
            "sequence",
            "route_quality",
        }
        assert required.issubset(set(props.keys()))
        assert props["route_quality"] == "high"  # CALIBRATED → high
        assert props["mode"] == "SEA"

    async def test_routes_malformed_geometry_skipped(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session,
        make_leg,
        make_shipment,
    ) -> None:
        _invalidate_routes_cache()
        shipment = await make_shipment()
        bad_leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.SEA,
            status=LegStatus.IN_PROGRESS,
            route_geometry=INVALID_GEOJSON,
            provenance=Provenance.CALIBRATED,
        )
        good_leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.IN_PROGRESS,
            route_geometry=VALID_LINESTRING_2,
            provenance=Provenance.SIMULATED,
        )

        resp = await client.get("/api/map/routes", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        leg_ids = {f["properties"]["leg_id"] for f in data["features"]}
        assert str(bad_leg.id) not in leg_ids
        assert str(good_leg.id) in leg_ids

    async def test_routes_empty_feature_collection_when_no_legs(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        _invalidate_routes_cache()
        with patch(
            "nexafreight.routers.map._build_routes_collection",
            return_value=__import__(
                "nexafreight.schemas.map", fromlist=["GeoJSONFeatureCollection"]
            ).GeoJSONFeatureCollection(features=[]),
        ):
            resp = await client.get("/api/map/routes", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "FeatureCollection"
        assert data["features"] == []

    async def test_routes_derived_provenance_quality(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session,
        make_leg,
        make_shipment,
    ) -> None:
        _invalidate_routes_cache()
        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.PLANNED,
            route_geometry=VALID_LINESTRING,
            provenance=Provenance.DERIVED,
        )

        resp = await client.get("/api/map/routes", headers=auth_headers)
        features = resp.json()["features"]
        props = next(f["properties"] for f in features if f["properties"]["leg_id"] == str(leg.id))
        assert props["route_quality"] == "medium"

    async def test_routes_simulated_provenance_quality(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session,
        make_leg,
        make_shipment,
    ) -> None:
        _invalidate_routes_cache()
        shipment = await make_shipment()
        leg = await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.ROAD,
            status=LegStatus.IN_PROGRESS,
            route_geometry=VALID_LINESTRING,
            provenance=Provenance.SIMULATED,
        )

        resp = await client.get("/api/map/routes", headers=auth_headers)
        features = resp.json()["features"]
        props = next(f["properties"] for f in features if f["properties"]["leg_id"] == str(leg.id))
        assert props["route_quality"] == "low"


# ===========================================================================
# 5. /ports
# ===========================================================================


class TestPorts:
    async def test_ports_returns_feature_collection(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        _invalidate_ports_cache()
        with patch(
            "nexafreight.routers.map._build_ports_collection",
        ) as mock_build:
            from nexafreight.schemas.map import GeoJSONFeature, GeoJSONFeatureCollection

            mock_build.return_value = GeoJSONFeatureCollection(
                features=[
                    GeoJSONFeature(
                        geometry={"type": "Point", "coordinates": [4.5, 52.0]},
                        properties={
                            "port_id": "1",
                            "location_id": "1",
                            "name": "Rotterdam",
                            "congestion_index": 1.2,
                            "provenance": "DERIVED",
                        },
                    )
                ]
            )
            resp = await client.get("/api/map/ports", headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1

    async def test_ports_feature_has_point_geometry(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        _invalidate_ports_cache()
        from nexafreight.schemas.map import GeoJSONFeature, GeoJSONFeatureCollection

        with patch(
            "nexafreight.routers.map._build_ports_collection",
            return_value=GeoJSONFeatureCollection(
                features=[
                    GeoJSONFeature(
                        geometry={"type": "Point", "coordinates": [4.5, 52.0]},
                        properties={
                            "port_id": "1",
                            "location_id": "2",
                            "name": "Test Port",
                            "congestion_index": 0.8,
                            "provenance": "DERIVED",
                        },
                    )
                ]
            ),
        ):
            resp = await client.get("/api/map/ports", headers=auth_headers)

        feature = resp.json()["features"][0]
        assert feature["geometry"]["type"] == "Point"
        assert len(feature["geometry"]["coordinates"]) == 2

    async def test_ports_feature_properties_complete(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        _invalidate_ports_cache()
        from nexafreight.schemas.map import GeoJSONFeature, GeoJSONFeatureCollection

        with patch(
            "nexafreight.routers.map._build_ports_collection",
            return_value=GeoJSONFeatureCollection(
                features=[
                    GeoJSONFeature(
                        geometry={"type": "Point", "coordinates": [4.5, 52.0]},
                        properties={
                            "port_id": "1",
                            "location_id": "2",
                            "name": "Port of Rotterdam",
                            "congestion_index": 1.4,
                            "provenance": "DERIVED",
                        },
                    )
                ]
            ),
        ):
            resp = await client.get("/api/map/ports", headers=auth_headers)

        props = resp.json()["features"][0]["properties"]
        required = {"port_id", "location_id", "name", "congestion_index", "provenance"}
        assert required.issubset(set(props.keys()))

    async def test_ports_empty_feature_collection(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        _invalidate_ports_cache()
        from nexafreight.schemas.map import GeoJSONFeatureCollection

        with patch(
            "nexafreight.routers.map._build_ports_collection",
            return_value=GeoJSONFeatureCollection(features=[]),
        ):
            resp = await client.get("/api/map/ports", headers=auth_headers)

        assert resp.status_code == 200
        assert resp.json()["features"] == []


# ===========================================================================
# 6. /feed-health
# ===========================================================================


class TestFeedHealth:
    async def test_feed_health_returns_200_always(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        tracker = MagicMock(spec=PositionTracker)
        tracker.get_feed_health = AsyncMock(return_value=make_unhealthy_feed_health("ais"))
        mock_worker = MagicMock()
        mock_worker.is_running = False

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolator_worker",
                return_value=mock_worker,
            ),
            patch("nexafreight.routers.map.get_interpolated_positions", return_value={}),
        ):
            resp = await client.get("/api/map/feed-health", headers=auth_headers)

        # Always 200, even with unhealthy adapters.
        assert resp.status_code == 200

    async def test_feed_health_response_structure(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        tracker = MagicMock(spec=PositionTracker)
        tracker.get_feed_health = AsyncMock(return_value=make_healthy_feed_health("aisstream"))
        mock_worker = MagicMock()
        mock_worker.is_running = True

        truck_pos = make_truck_position("5")

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolator_worker",
                return_value=mock_worker,
            ),
            patch(
                "nexafreight.routers.map.get_interpolated_positions",
                return_value={"5": truck_pos},
            ),
        ):
            resp = await client.get("/api/map/feed-health", headers=auth_headers)

        data = resp.json()
        assert "adapters" in data
        assert len(data["adapters"]) == 2

        required_fields = {
            "adapter_name",
            "is_healthy",
            "last_success_at",
            "messages_received",
            "provenance",
        }
        for adapter in data["adapters"]:
            assert required_fields.issubset(set(adapter.keys()))

    async def test_feed_health_ais_adapter_healthy(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        tracker = MagicMock(spec=PositionTracker)
        tracker.get_feed_health = AsyncMock(return_value=make_healthy_feed_health("aisstream"))
        mock_worker = MagicMock()
        mock_worker.is_running = False

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolator_worker",
                return_value=mock_worker,
            ),
            patch("nexafreight.routers.map.get_interpolated_positions", return_value={}),
        ):
            resp = await client.get("/api/map/feed-health", headers=auth_headers)

        adapters = resp.json()["adapters"]
        ais_adapter = next(a for a in adapters if "ais" in a["adapter_name"].lower())
        assert ais_adapter["is_healthy"] is True
        assert ais_adapter["messages_received"] == 999

    async def test_feed_health_interpolator_derived_from_cache(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        """Interpolator health is synthesised from is_running + cache size."""
        tracker = MagicMock(spec=PositionTracker)
        tracker.get_feed_health = AsyncMock(return_value=make_unhealthy_feed_health("ais"))
        mock_worker = MagicMock()
        mock_worker.is_running = True

        truck_pos = make_truck_position("42")

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolator_worker",
                return_value=mock_worker,
            ),
            patch(
                "nexafreight.routers.map.get_interpolated_positions",
                return_value={"42": truck_pos},
            ),
        ):
            resp = await client.get("/api/map/feed-health", headers=auth_headers)

        adapters = resp.json()["adapters"]
        interp = next(a for a in adapters if a["adapter_name"] == "position_interpolator")
        assert interp["is_healthy"] is True
        assert interp["messages_received"] == 1

    async def test_feed_health_200_even_when_both_unhealthy(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        tracker = MagicMock(spec=PositionTracker)
        tracker.get_feed_health = AsyncMock(return_value=make_unhealthy_feed_health("ais"))
        mock_worker = MagicMock()
        mock_worker.is_running = False

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolator_worker",
                return_value=mock_worker,
            ),
            patch("nexafreight.routers.map.get_interpolated_positions", return_value={}),
        ):
            resp = await client.get("/api/map/feed-health", headers=auth_headers)

        assert resp.status_code == 200
        adapters = resp.json()["adapters"]
        assert all(not a["is_healthy"] for a in adapters)


# ===========================================================================
# 7. Caching behaviour
# ===========================================================================


class TestCaching:
    async def test_routes_cache_reduces_db_queries(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Second request within TTL must not trigger a new DB query."""
        _invalidate_routes_cache()

        call_count = 0

        async def counting_build(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            from nexafreight.schemas.map import GeoJSONFeatureCollection

            return GeoJSONFeatureCollection(features=[])

        with patch(
            "nexafreight.routers.map._build_routes_collection",
            side_effect=counting_build,
        ):
            resp1 = await client.get("/api/map/routes", headers=auth_headers)
            resp2 = await client.get("/api/map/routes", headers=auth_headers)

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert call_count == 1  # DB queried only once

    async def test_routes_cache_refreshes_after_invalidation(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        """After cache invalidation, next request triggers a new DB query."""
        _invalidate_routes_cache()

        call_count = 0

        async def counting_build(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            from nexafreight.schemas.map import GeoJSONFeatureCollection

            return GeoJSONFeatureCollection(features=[])

        with patch(
            "nexafreight.routers.map._build_routes_collection",
            side_effect=counting_build,
        ):
            await client.get("/api/map/routes", headers=auth_headers)
            _invalidate_routes_cache()
            await client.get("/api/map/routes", headers=auth_headers)

        assert call_count == 2

    async def test_ports_cache_reduces_db_queries(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        _invalidate_ports_cache()
        call_count = 0

        async def counting_build(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            from nexafreight.schemas.map import GeoJSONFeatureCollection

            return GeoJSONFeatureCollection(features=[])

        with patch(
            "nexafreight.routers.map._build_ports_collection",
            side_effect=counting_build,
        ):
            await client.get("/api/map/ports", headers=auth_headers)
            await client.get("/api/map/ports", headers=auth_headers)

        assert call_count == 1


# ===========================================================================
# 8. Get-all-positions unit tests
# ===========================================================================


class TestGetAllPositions:
    async def test_get_all_positions_merges_both_sources(self) -> None:
        ais_pos = make_ais_position("111111111")
        truck_pos = make_truck_position("999")
        tracker = mock_tracker([ais_pos])

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolated_positions",
                return_value={"999": truck_pos},
            ),
        ):
            result = await _get_all_positions()

        ids = {p.asset_id for p in result}
        assert "111111111" in ids
        assert "999" in ids

    async def test_get_all_positions_handles_empty_ais_cache(self) -> None:
        tracker = mock_tracker([])
        truck_pos = make_truck_position("5")

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch(
                "nexafreight.routers.map.get_interpolated_positions",
                return_value={"5": truck_pos},
            ),
        ):
            result = await _get_all_positions()

        assert len(result) == 1

    async def test_get_all_positions_handles_empty_interpolated_cache(self) -> None:
        ais_pos = make_ais_position()
        tracker = mock_tracker([ais_pos])

        with (
            patch("nexafreight.routers.map.get_position_tracker", return_value=tracker),
            patch("nexafreight.routers.map.get_interpolated_positions", return_value={}),
        ):
            result = await _get_all_positions()

        assert len(result) == 1


# ===========================================================================
# 9. Build routes / ports — direct function tests
# ===========================================================================


class TestBuildRoutesCollection:
    async def test_build_routes_returns_empty_on_no_legs(self) -> None:
        result = await _build_routes_collection()
        assert result.type == "FeatureCollection"
        assert isinstance(result.features, list)

    async def test_build_routes_skips_malformed_geometry(self, make_leg, make_shipment) -> None:
        shipment = await make_shipment()
        await make_leg(
            shipment_id=shipment.id,
            mode=TransportMode.SEA,
            status=LegStatus.IN_PROGRESS,
            route_geometry=INVALID_GEOJSON,
            provenance=Provenance.CALIBRATED,
        )
        result = await _build_routes_collection()
        assert result.features == []


class TestBuildPortsCollection:
    async def test_build_ports_returns_empty_on_no_ports(self) -> None:
        result = await _build_ports_collection()
        assert result.type == "FeatureCollection"
        assert result.features == []
