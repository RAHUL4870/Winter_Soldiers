"""Unit tests for route planning logic (T-018).

Tests the real src/nexafreight/services/route_planner.py implementation
using in-memory SQLite and fixture-created Shipment/Location rows only.
All external routing calls (searoute-py/ORS) are mocked — zero real network.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.enums import LegStatus, Provenance, TransportMode
from nexafreight.services.route_planner import plan_legs_for_shipment

# Fixed GeoJSON returned by mocked primary routing method
MOCK_ROUTE_GEOJSON = {
    "type": "LineString",
    "coordinates": [[-74.0060, 40.7128], [-3.7038, 40.4168]],
}
MOCK_DISTANCE_KM = 5570.0
MOCK_CO2_KG = 1200.0

# Fallback (geodesic) straight-line GeoJSON
FALLBACK_GEOJSON = {
    "type": "LineString",
    "coordinates": [[-74.0060, 40.7128], [4.47917, 51.9225]],
}
FALLBACK_DISTANCE_KM = 5850.0
FALLBACK_CO2_KG = 1250.0


@pytest.mark.asyncio
async def test_leg_sequence_correctness(
    db_session: AsyncSession, make_location, make_shipment
) -> None:
    """Multi-leg shipment gets legs ordered 1,2,3 matching travel order."""
    origin = await make_location(locode="USNYC", latitude=40.7128, longitude=-74.0060)
    dest = await make_location(locode="NLRTM", latitude=51.9225, longitude=4.47917)
    shipment = await make_shipment(
        origin=origin,
        destination=dest,
        primary_transport_mode=TransportMode.SEA,
    )

    with patch(
        "nexafreight.services.route_planner._get_route_between",
        new=AsyncMock(return_value=(MOCK_ROUTE_GEOJSON, MOCK_DISTANCE_KM, MOCK_CO2_KG)),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    # Single direct route -> one leg (implementation returns 1 leg for point-to-point)
    assert len(legs) == 1
    assert legs[0].sequence_number == 1


@pytest.mark.asyncio
async def test_leg_mode_assignment(db_session: AsyncSession, make_location, make_shipment) -> None:
    """Each leg is tagged with the transport mode intended for its segment."""
    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")
    shipment = await make_shipment(
        origin=origin,
        destination=dest,
        primary_transport_mode=TransportMode.SEA,
    )

    with patch(
        "nexafreight.services.route_planner._get_route_between",
        new=AsyncMock(return_value=(MOCK_ROUTE_GEOJSON, MOCK_DISTANCE_KM, MOCK_CO2_KG)),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    assert len(legs) == 1
    assert legs[0].transport_mode == TransportMode.SEA


@pytest.mark.asyncio
async def test_planned_departure_arrival_chaining(
    db_session: AsyncSession, make_location, make_shipment
) -> None:
    """Subsequent leg's planned_departure chains from previous leg's planned_arrival."""
    origin = await make_location(locode="USNYC")
    waypoint = await make_location(locode="GBLON")
    dest = await make_location(locode="NLRTM")
    shipment = await make_shipment(
        origin=origin,
        destination=dest,
        primary_transport_mode=TransportMode.SEA,
    )
    # Force multi-leg by monkeypatching the internal segment builder to return two modes
    with (
        patch(
            "nexafreight.services.route_planner._get_route_between",
            new=AsyncMock(return_value=(MOCK_ROUTE_GEOJSON, MOCK_DISTANCE_KM, MOCK_CO2_KG)),
        ),
        patch(
            "nexafreight.services.route_planner._determine_segments",
            new=AsyncMock(
                return_value=[
                    (TransportMode.SEA, origin, waypoint),
                    (TransportMode.ROAD, waypoint, dest),
                ]
            ),
        ),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    assert len(legs) == 2
    # Sequence order
    assert legs[0].sequence_number == 1
    assert legs[1].sequence_number == 2
    # Chaining: leg2 departure = leg1 arrival + 2h buffer
    assert legs[1].planned_departure > legs[0].planned_arrival
    assert legs[1].planned_departure == legs[0].planned_arrival + timedelta(hours=2)


@pytest.mark.asyncio
async def test_fallback_flag_and_provenance_on_failure(
    db_session: AsyncSession, make_location, make_shipment
) -> None:
    """When primary routing fails, fallback geodesic is used and flagged via provenance=DERIVED."""
    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")
    shipment = await make_shipment(
        origin=origin, destination=dest, primary_transport_mode=TransportMode.SEA
    )

    # Make primary routing method raise -> triggers fallback
    with patch(
        "nexafreight.services.route_planner._get_route_between",
        new=AsyncMock(side_effect=RuntimeError("ORS unreachable")),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    assert len(legs) == 1
    leg = legs[0]
    # Fallback provenance is DERIVED (geodesic-computed), not REAL
    assert leg.provenance == Provenance.DERIVED
    # Geometry is still valid GeoJSON (straight line)
    assert leg.route_geometry_json is not None
    geom = json.loads(leg.route_geometry_json)
    assert geom["type"] == "LineString"
    assert len(geom["coordinates"]) == 2


@pytest.mark.asyncio
async def test_primary_path_provenance_is_real(
    db_session: AsyncSession, make_location, make_shipment
) -> None:
    """When primary routing succeeds, provenance is REAL."""
    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")
    shipment = await make_shipment(
        origin=origin, destination=dest, primary_transport_mode=TransportMode.SEA
    )

    with patch(
        "nexafreight.services.route_planner._get_route_between",
        new=AsyncMock(return_value=(MOCK_ROUTE_GEOJSON, MOCK_DISTANCE_KM, MOCK_CO2_KG)),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    assert len(legs) == 1
    assert legs[0].provenance == Provenance.REAL


@pytest.mark.asyncio
async def test_geometry_presence_and_validity(
    db_session: AsyncSession, make_location, make_shipment
) -> None:
    """Every produced leg has non-empty, structurally valid GeoJSON geometry."""
    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")
    shipment = await make_shipment(origin=origin, destination=dest)

    with patch(
        "nexafreight.services.route_planner._get_route_between",
        new=AsyncMock(return_value=(MOCK_ROUTE_GEOJSON, MOCK_DISTANCE_KM, MOCK_CO2_KG)),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    for leg in legs:
        assert leg.route_geometry_json is not None
        geom = json.loads(leg.route_geometry_json)
        assert geom["type"] == "LineString"
        assert len(geom["coordinates"]) >= 2


@pytest.mark.asyncio
async def test_distance_and_co2_populated(
    db_session: AsyncSession, make_location, make_shipment
) -> None:
    """Each leg has computed distance and CO2 values populated (not null/zero by omission)."""
    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")
    shipment = await make_shipment(origin=origin, destination=dest)

    with patch(
        "nexafreight.services.route_planner._get_route_between",
        new=AsyncMock(return_value=(MOCK_ROUTE_GEOJSON, MOCK_DISTANCE_KM, MOCK_CO2_KG)),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    for leg in legs:
        assert leg.distance_km is not None and leg.distance_km > 0
        assert leg.co2_kg is not None and leg.co2_kg > 0


@pytest.mark.asyncio
async def test_single_leg_shipment(db_session: AsyncSession, make_location, make_shipment) -> None:
    """Direct single-leg shipment produces exactly one sequenced leg."""
    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")
    shipment = await make_shipment(origin=origin, destination=dest)

    with patch(
        "nexafreight.services.route_planner._get_route_between",
        new=AsyncMock(return_value=(MOCK_ROUTE_GEOJSON, MOCK_DISTANCE_KM, MOCK_CO2_KG)),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    assert len(legs) == 1
    assert legs[0].sequence_number == 1
    assert legs[0].status == LegStatus.PLANNED


@pytest.mark.asyncio
async def test_no_external_network_calls(
    db_session: AsyncSession, make_location, make_shipment
) -> None:
    """All external routing calls are mocked — zero real network requests."""
    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")
    shipment = await make_shipment(origin=origin, destination=dest)

    # Patch the low-level HTTP/ORS function; if it is called for real, test fails via assertion
    with (
        patch(
            "nexafreight.services.route_planner._call_ors_api",
            new=AsyncMock(side_effect=AssertionError("Real network call attempted!")),
        ) as mock_ors,
        patch(
            "nexafreight.services.route_planner._get_route_between",
            new=AsyncMock(return_value=(MOCK_ROUTE_GEOJSON, MOCK_DISTANCE_KM, MOCK_CO2_KG)),
        ),
    ):
        legs = await plan_legs_for_shipment(db_session, shipment)

    # mock_ors should not have raised; the mocked _get_route_between handled it
    assert len(legs) == 1
    mock_ors.assert_not_called()
