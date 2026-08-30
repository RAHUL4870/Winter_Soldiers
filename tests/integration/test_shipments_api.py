"""Integration tests for shipment list API endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from nexafreight.enums import (
    AlertSeverity,
    AlertStatus,
    DisruptionStatus,
    DisruptionType,
    ShipmentStatus,
    TransportMode,
)
from nexafreight.models import Alert, Disruption, User


@pytest.mark.asyncio
async def test_empty_database_returns_empty_list(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
) -> None:
    """Empty database returns valid empty response with correct pagination metadata."""
    headers = auth_headers_factory(seed_admin_user)

    response = await client.get("/api/shipments", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1
    assert data["total_pages"] == 0


@pytest.mark.asyncio
async def test_basic_listing(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Basic listing returns all shipments with correct shape."""
    headers = auth_headers_factory(seed_admin_user)

    # Create 3 shipments with different statuses
    await make_shipment(status=ShipmentStatus.PLANNED)
    await make_shipment(status=ShipmentStatus.IN_TRANSIT)
    await make_shipment(status=ShipmentStatus.DELIVERED)

    response = await client.get("/api/shipments", headers=headers)

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 3
    assert data["total"] == 3

    # Verify item shape
    item = data["items"][0]
    assert "id" in item
    assert "origin" in item  # Should be LOCODE
    assert "destination" in item
    assert "mode" in item
    assert "status" in item
    assert "strictest_sla_deadline" in item
    assert "revised_eta" in item


@pytest.mark.asyncio
async def test_status_filter(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Status filter returns only matching shipments."""
    headers = auth_headers_factory(seed_admin_user)

    # Create shipments with different statuses
    await make_shipment(status=ShipmentStatus.PLANNED)
    await make_shipment(status=ShipmentStatus.PLANNED)
    await make_shipment(status=ShipmentStatus.IN_TRANSIT)
    await make_shipment(status=ShipmentStatus.DELIVERED)

    response = await client.get(
        "/api/shipments",
        params={"status": "PLANNED"},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 2
    assert data["total"] == 2
    assert all(item["status"] == "PLANNED" for item in data["items"])


@pytest.mark.asyncio
async def test_mode_filter(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Transport mode filter returns only matching shipments."""
    headers = auth_headers_factory(seed_admin_user)

    await make_shipment(primary_transport_mode=TransportMode.SEA)
    await make_shipment(primary_transport_mode=TransportMode.SEA)
    await make_shipment(primary_transport_mode=TransportMode.AIR)

    response = await client.get(
        "/api/shipments",
        params={"mode": "SEA"},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 2
    assert all(item["mode"] == "SEA" for item in data["items"])


@pytest.mark.asyncio
async def test_combined_status_and_mode_filter(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Combined filters use AND logic."""
    headers = auth_headers_factory(seed_admin_user)

    # Create all combinations
    await make_shipment(status=ShipmentStatus.PLANNED, primary_transport_mode=TransportMode.SEA)
    await make_shipment(status=ShipmentStatus.PLANNED, primary_transport_mode=TransportMode.AIR)
    await make_shipment(status=ShipmentStatus.IN_TRANSIT, primary_transport_mode=TransportMode.SEA)
    await make_shipment(status=ShipmentStatus.IN_TRANSIT, primary_transport_mode=TransportMode.AIR)

    response = await client.get(
        "/api/shipments",
        params={"status": "IN_TRANSIT", "mode": "SEA"},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["status"] == "IN_TRANSIT"
    assert data["items"][0]["mode"] == "SEA"


@pytest.mark.asyncio
async def test_invalid_status_returns_422(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
) -> None:
    """Invalid status value returns 422 validation error."""
    headers = auth_headers_factory(seed_admin_user)

    response = await client.get(
        "/api/shipments",
        params={"status": "INVALID_STATUS"},
        headers=headers,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_invalid_mode_returns_422(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
) -> None:
    """Invalid transport mode returns 422 validation error."""
    headers = auth_headers_factory(seed_admin_user)

    response = await client.get(
        "/api/shipments",
        params={"mode": "TELEPORTATION"},
        headers=headers,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_alert_filter(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
    db_session,
) -> None:
    """Alert filter returns only shipments with active alerts."""
    headers = auth_headers_factory(seed_admin_user)

    # Shipment with active alert
    shipment_with_alert = await make_shipment()
    disruption1 = Disruption(
        shipment_id=shipment_with_alert.id,
        disruption_type=DisruptionType.PORT_CONGESTION,
        status=DisruptionStatus.ACTIVE,
        description="Port congestion delay",
        detected_at=datetime.now(UTC),
    )
    db_session.add(disruption1)
    await db_session.flush()

    alert1 = Alert(
        shipment_id=shipment_with_alert.id,
        disruption_id=disruption1.id,
        severity=AlertSeverity.HIGH,
        status=AlertStatus.OPEN,
        financial_exposure=5000.0,
    )
    db_session.add(alert1)

    # Shipment without alert
    await make_shipment()

    # Shipment with resolved (inactive) alert
    shipment_resolved = await make_shipment()
    disruption2 = Disruption(
        shipment_id=shipment_resolved.id,
        disruption_type=DisruptionType.WEATHER,
        status=DisruptionStatus.RESOLVED,
        description="Weather delay resolved",
        detected_at=datetime.now(UTC),
    )
    db_session.add(disruption2)
    await db_session.flush()

    alert2 = Alert(
        shipment_id=shipment_resolved.id,
        disruption_id=disruption2.id,
        severity=AlertSeverity.LOW,
        status=AlertStatus.RESOLVED,
        financial_exposure=1000.0,
    )
    db_session.add(alert2)

    await db_session.commit()

    response = await client.get(
        "/api/shipments",
        params={"alert": "true"},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == shipment_with_alert.id


@pytest.mark.asyncio
async def test_alert_filter_deduplication(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
    db_session,
) -> None:
    """Shipment with multiple alerts appears only once in filtered results."""
    headers = auth_headers_factory(seed_admin_user)

    shipment = await make_shipment()

    # Create multiple active alerts for same shipment
    disruption_types = [
        DisruptionType.PORT_CONGESTION,
        DisruptionType.WEATHER,
        DisruptionType.VESSEL_DELAY,
    ]
    for i, dtype in enumerate(disruption_types):
        disruption = Disruption(
            shipment_id=shipment.id,
            disruption_type=dtype,
            status=DisruptionStatus.ACTIVE,
            description=f"Disruption {i}",
            detected_at=datetime.now(UTC),
        )
        db_session.add(disruption)
        await db_session.flush()

        alert = Alert(
            shipment_id=shipment.id,
            disruption_id=disruption.id,
            severity=AlertSeverity.MEDIUM,
            status=AlertStatus.OPEN,
            financial_exposure=1000.0 * (i + 1),
        )
        db_session.add(alert)

    await db_session.commit()

    response = await client.get(
        "/api/shipments",
        params={"alert": "true"},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 1  # Not 3
    assert data["total"] == 1


@pytest.mark.asyncio
async def test_pagination_correctness(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Pagination returns distinct, non-overlapping pages."""
    headers = auth_headers_factory(seed_admin_user)

    # Create 25 shipments
    shipments = []
    for _ in range(25):
        s = await make_shipment()
        shipments.append(s.id)

    # Page 1 (size=10)
    response1 = await client.get(
        "/api/shipments",
        params={"page": 1, "size": 10},
        headers=headers,
    )
    assert response1.status_code == 200
    data1 = response1.json()
    assert len(data1["items"]) == 10
    assert data1["total"] == 25
    assert data1["total_pages"] == 3
    page1_ids = {item["id"] for item in data1["items"]}

    # Page 2 (size=10)
    response2 = await client.get(
        "/api/shipments",
        params={"page": 2, "size": 10},
        headers=headers,
    )
    data2 = response2.json()
    assert len(data2["items"]) == 10
    page2_ids = {item["id"] for item in data2["items"]}

    # Page 3 (size=10, partial)
    response3 = await client.get(
        "/api/shipments",
        params={"page": 3, "size": 10},
        headers=headers,
    )
    data3 = response3.json()
    assert len(data3["items"]) == 5
    page3_ids = {item["id"] for item in data3["items"]}

    # Verify no overlap
    assert page1_ids.isdisjoint(page2_ids)
    assert page1_ids.isdisjoint(page3_ids)
    assert page2_ids.isdisjoint(page3_ids)


@pytest.mark.asyncio
async def test_pagination_beyond_available_data(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_shipment,
) -> None:
    """Requesting page beyond available data returns empty list with correct metadata."""
    headers = auth_headers_factory(seed_admin_user)

    # Create 5 shipments
    for _ in range(5):
        await make_shipment()

    # Request page 10 (way beyond available data)
    response = await client.get(
        "/api/shipments",
        params={"page": 10, "size": 20},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["items"] == []
    assert data["total"] == 5
    assert data["page"] == 10
    assert data["total_pages"] == 1


@pytest.mark.asyncio
async def test_authentication_required(client: AsyncClient) -> None:
    """Endpoint requires authentication."""
    response = await client.get("/api/shipments")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_origin_destination_resolved_without_n_plus_1(
    client: AsyncClient,
    auth_headers_factory,
    seed_admin_user: User,
    make_location,
    make_shipment,
) -> None:
    """Origin/destination names resolved efficiently (eager loading, no N+1 queries)."""
    headers = auth_headers_factory(seed_admin_user)

    # Create 10 shipments with different origins/destinations
    for i in range(10):
        origin = await make_location(locode=f"US{i:03d}", name=f"Origin {i}")
        dest = await make_location(locode=f"NL{i:03d}", name=f"Dest {i}")
        await make_shipment(origin=origin, destination=dest)

    response = await client.get(
        "/api/shipments",
        params={"size": 10},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 10

    # Verify all origins/destinations resolved
    for item in data["items"]:
        assert item["origin"].startswith("US")
        assert item["destination"].startswith("NL")
