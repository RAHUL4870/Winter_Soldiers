"""Integration tests for shipment detail/route/events endpoints."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from nexafreight.enums import (
    CargoClass,
    LegStatus,
    OrderSlaStatus,
    Provenance,
    ShipmentStatus,
    TransportMode,
)
from nexafreight.models import AuditLog, Leg, Order, Shipment, User

# ============================================================================
# GET /api/shipments/{id} Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_shipment_detail_with_legs_and_orders(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_location,
    db_session,
) -> None:
    """Get shipment detail returns full data with ordered legs and orders."""
    headers = auth_headers_factory(seed_admin_user)

    # Create locations
    origin = await make_location(locode="USNYC", name="New York")
    dest = await make_location(locode="NLRTM", name="Rotterdam")
    waypoint = await make_location(locode="GBLON", name="London")

    # Create shipment
    shipment = Shipment(
        origin_id=origin.id,
        destination_id=dest.id,
        primary_transport_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
        status=ShipmentStatus.IN_TRANSIT,
        container_count=2,
        route_version=1,
    )
    db_session.add(shipment)
    await db_session.flush()

    # Create legs (ordered by sequence)
    leg1 = Leg(
        shipment_id=shipment.id,
        sequence_number=1,
        route_version=1,
        transport_mode=TransportMode.SEA,
        status=LegStatus.COMPLETED,
        origin_id=origin.id,
        destination_id=waypoint.id,
        planned_departure=datetime.now(UTC),
        planned_arrival=datetime.now(UTC) + timedelta(days=5),
        distance_km=5850.0,
        co2_kg=1200.0,
        provenance=Provenance.REAL,
    )
    leg2 = Leg(
        shipment_id=shipment.id,
        sequence_number=2,
        route_version=1,
        transport_mode=TransportMode.SEA,
        status=LegStatus.IN_PROGRESS,
        origin_id=waypoint.id,
        destination_id=dest.id,
        planned_departure=datetime.now(UTC) + timedelta(days=5),
        planned_arrival=datetime.now(UTC) + timedelta(days=10),
        distance_km=320.0,
        co2_kg=65.0,
        provenance=Provenance.REAL,
    )
    db_session.add_all([leg1, leg2])

    # Create orders
    order1 = Order(
        order_number="ORD-001",
        shipment_id=shipment.id,
        sla_deadline=datetime.now(UTC) + timedelta(days=15),
        revenue=10000.0,
        shipping_cost=1000.0,
        sla_status=OrderSlaStatus.ON_TIME,
        shipping_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
    )
    order2 = Order(
        order_number="ORD-002",
        shipment_id=shipment.id,
        sla_deadline=datetime.now(UTC) + timedelta(days=12),
        revenue=15000.0,
        shipping_cost=1500.0,
        sla_status=OrderSlaStatus.AT_RISK,
        shipping_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
    )
    db_session.add_all([order1, order2])

    await db_session.commit()

    # Request detail
    response = await client.get(f"/api/shipments/{shipment.id}", headers=headers)

    assert response.status_code == 200
    data = response.json()

    # Verify shipment fields
    assert data["id"] == shipment.id
    assert data["origin"] == "USNYC"
    assert data["destination"] == "NLRTM"
    assert data["status"] == "IN_TRANSIT"
    assert data["container_count"] == 2

    # Verify legs (should be ordered by sequence)
    assert len(data["legs"]) == 2
    assert data["legs"][0]["sequence_number"] == 1
    assert data["legs"][0]["origin"] == "USNYC"
    assert data["legs"][0]["destination"] == "GBLON"
    assert data["legs"][0]["provenance"] == "REAL"
    assert data["legs"][1]["sequence_number"] == 2

    # Verify orders
    assert len(data["orders"]) == 2
    order_numbers = {o["order_number"] for o in data["orders"]}
    assert order_numbers == {"ORD-001", "ORD-002"}


@pytest.mark.asyncio
async def test_get_shipment_detail_nonexistent_id(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
) -> None:
    """Non-existent shipment ID returns 404."""
    headers = auth_headers_factory(seed_admin_user)

    response = await client.get(
        "/api/shipments/00000000-0000-0000-0000-000000000000",
        headers=headers,
    )

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_shipment_detail_empty_legs_and_orders(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Shipment with zero legs and orders returns valid empty lists."""
    headers = auth_headers_factory(seed_admin_user)

    # Create shipment with no legs or orders
    shipment = await make_shipment()

    response = await client.get(f"/api/shipments/{shipment.id}", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["legs"] == []
    assert data["orders"] == []


@pytest.mark.asyncio
async def test_get_shipment_detail_requires_auth(client: AsyncClient, make_shipment) -> None:
    """Endpoint requires authentication."""
    shipment = await make_shipment()

    response = await client.get(f"/api/shipments/{shipment.id}")
    assert response.status_code == 401


# ============================================================================
# GET /api/shipments/{id}/route Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_shipment_route_multiple_legs(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_location,
    db_session,
) -> None:
    """Route with multiple legs returns valid FeatureCollection."""
    headers = auth_headers_factory(seed_admin_user)

    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")
    waypoint = await make_location(locode="GBLON")

    shipment = Shipment(
        origin_id=origin.id,
        destination_id=dest.id,
        primary_transport_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
    )
    db_session.add(shipment)
    await db_session.flush()

    # Leg 1 with geometry
    leg1 = Leg(
        shipment_id=shipment.id,
        sequence_number=1,
        route_version=1,
        transport_mode=TransportMode.SEA,
        origin_id=origin.id,
        destination_id=waypoint.id,
        planned_departure=datetime.now(UTC),
        planned_arrival=datetime.now(UTC) + timedelta(days=5),
        route_geometry_json=json.dumps(
            {
                "type": "LineString",
                "coordinates": [[-74.006, 40.7128], [-0.1278, 51.5074]],
            }
        ),
        provenance=Provenance.REAL,
    )

    # Leg 2 with geometry
    leg2 = Leg(
        shipment_id=shipment.id,
        sequence_number=2,
        route_version=1,
        transport_mode=TransportMode.SEA,
        origin_id=waypoint.id,
        destination_id=dest.id,
        planned_departure=datetime.now(UTC) + timedelta(days=5),
        planned_arrival=datetime.now(UTC) + timedelta(days=10),
        route_geometry_json=json.dumps(
            {
                "type": "LineString",
                "coordinates": [[-0.1278, 51.5074], [4.47917, 51.9225]],
            }
        ),
        provenance=Provenance.CALIBRATED,
    )

    db_session.add_all([leg1, leg2])
    await db_session.commit()

    response = await client.get(f"/api/shipments/{shipment.id}/route", headers=headers)

    assert response.status_code == 200
    data = response.json()

    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 2

    # Verify first feature
    feature1 = data["features"][0]
    assert feature1["type"] == "Feature"
    assert feature1["geometry"]["type"] == "LineString"
    assert feature1["properties"]["mode"] == "SEA"
    assert feature1["properties"]["provenance"] == "REAL"
    assert feature1["properties"]["route_quality"] == "high"  # REAL -> high

    # Verify second feature
    feature2 = data["features"][1]
    assert feature2["properties"]["provenance"] == "CALIBRATED"
    assert feature2["properties"]["route_quality"] == "high"  # CALIBRATED -> high


@pytest.mark.asyncio
async def test_get_shipment_route_empty_legs(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Shipment with no legs returns empty FeatureCollection (not error)."""
    headers = auth_headers_factory(seed_admin_user)

    shipment = await make_shipment()

    response = await client.get(f"/api/shipments/{shipment.id}/route", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "FeatureCollection"
    assert data["features"] == []


@pytest.mark.asyncio
async def test_get_shipment_route_nonexistent_shipment(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
) -> None:
    """Non-existent shipment returns 404."""
    headers = auth_headers_factory(seed_admin_user)

    response = await client.get(
        "/api/shipments/00000000-0000-0000-0000-000000000000/route",
        headers=headers,
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_shipment_route_malformed_geometry_skipped(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_location,
    db_session,
) -> None:
    """Leg with malformed geometry is skipped gracefully."""
    headers = auth_headers_factory(seed_admin_user)

    origin = await make_location(locode="USNYC")
    dest = await make_location(locode="NLRTM")

    shipment = Shipment(
        origin_id=origin.id,
        destination_id=dest.id,
        primary_transport_mode=TransportMode.SEA,
        cargo_class=CargoClass.STANDARD,
    )
    db_session.add(shipment)
    await db_session.flush()

    # Good leg
    good_leg = Leg(
        shipment_id=shipment.id,
        sequence_number=1,
        route_version=1,
        transport_mode=TransportMode.SEA,
        origin_id=origin.id,
        destination_id=dest.id,
        planned_departure=datetime.now(UTC),
        planned_arrival=datetime.now(UTC) + timedelta(days=5),
        route_geometry_json=json.dumps({"type": "LineString", "coordinates": [[0, 0], [1, 1]]}),
        provenance=Provenance.REAL,
    )

    # Bad leg (malformed JSON)
    bad_leg = Leg(
        shipment_id=shipment.id,
        sequence_number=2,
        route_version=1,
        transport_mode=TransportMode.SEA,
        origin_id=origin.id,
        destination_id=dest.id,
        planned_departure=datetime.now(UTC) + timedelta(days=5),
        planned_arrival=datetime.now(UTC) + timedelta(days=10),
        route_geometry_json="{invalid json here",  # Malformed
        provenance=Provenance.MOCK,
    )

    db_session.add_all([good_leg, bad_leg])
    await db_session.commit()

    response = await client.get(f"/api/shipments/{shipment.id}/route", headers=headers)

    assert response.status_code == 200
    data = response.json()

    # Should only include the good leg
    assert len(data["features"]) == 1
    assert data["features"][0]["properties"]["sequence"] == 1


# ============================================================================
# GET /api/shipments/{id}/events Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_shipment_events_with_audit_entries(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
    db_session,
) -> None:
    """Shipment with audit log entries returns them in descending order."""
    headers = auth_headers_factory(seed_admin_user)

    shipment = await make_shipment()

    # Create audit log entries (simulating future tasks)
    now = datetime.now(UTC)

    entry1 = AuditLog(
        created_at=now - timedelta(hours=2),
        actor_type="user",
        actor_name="operator@test.local",
        action="shipment_created",
        entity_type="shipment",
        entity_id=shipment.id,
    )
    entry2 = AuditLog(
        created_at=now - timedelta(hours=1),
        actor_type="system",
        actor_name="disruption_detector",
        action="disruption_detected",
        entity_type="shipment",
        entity_id=shipment.id,
    )
    entry3 = AuditLog(
        created_at=now,
        actor_type="user",
        actor_name="admin@test.local",
        action="reroute_approved",
        entity_type="shipment",
        entity_id=shipment.id,
    )

    db_session.add_all([entry1, entry2, entry3])
    await db_session.commit()

    response = await client.get(f"/api/shipments/{shipment.id}/events", headers=headers)

    assert response.status_code == 200
    data = response.json()

    # Should be in descending timestamp order (most recent first)
    assert len(data["items"]) == 3
    assert data["items"][0]["event_type"] == "reroute_approved"
    assert data["items"][1]["event_type"] == "disruption_detected"
    assert data["items"][2]["event_type"] == "shipment_created"

    # Verify actor names preserved
    assert data["items"][0]["actor"] == "admin@test.local"
    assert data["items"][1]["actor"] == "disruption_detector"


@pytest.mark.asyncio
async def test_get_shipment_events_empty_log(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Shipment with no audit entries returns empty list (not error)."""
    headers = auth_headers_factory(seed_admin_user)

    shipment = await make_shipment()

    response = await client.get(f"/api/shipments/{shipment.id}/events", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_get_shipment_events_nonexistent_shipment(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
) -> None:
    """Non-existent shipment returns 404."""
    headers = auth_headers_factory(seed_admin_user)

    response = await client.get(
        "/api/shipments/00000000-0000-0000-0000-000000000000/events",
        headers=headers,
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_shipment_events_pagination(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
    db_session,
) -> None:
    """Events endpoint supports pagination."""
    headers = auth_headers_factory(seed_admin_user)

    shipment = await make_shipment()

    # Create 25 audit entries
    now = datetime.now(UTC)
    for i in range(25):
        entry = AuditLog(
            created_at=now - timedelta(hours=i),
            actor_type="system",
            actor_name="test",
            action=f"event_{i}",
            entity_type="shipment",
            entity_id=shipment.id,
        )
        db_session.add(entry)

    await db_session.commit()

    # Page 1
    response1 = await client.get(
        f"/api/shipments/{shipment.id}/events",
        params={"page": 1, "size": 10},
        headers=headers,
    )
    data1 = response1.json()
    assert len(data1["items"]) == 10
    assert data1["total"] == 25
    assert data1["total_pages"] == 3

    # Page 2
    response2 = await client.get(
        f"/api/shipments/{shipment.id}/events",
        params={"page": 2, "size": 10},
        headers=headers,
    )
    data2 = response2.json()
    assert len(data2["items"]) == 10

    # Verify no overlap
    page1_events = {e["event_type"] for e in data1["items"]}
    page2_events = {e["event_type"] for e in data2["items"]}
    assert page1_events.isdisjoint(page2_events)
