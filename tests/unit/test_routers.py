from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_stub_endpoints(client: AsyncClient, test_user, auth_headers_factory, make_shipment):
    headers = auth_headers_factory(test_user)
    shipment = await make_shipment()
    # Shipments
    res = await client.get("/api/shipments", headers=headers)
    assert res.status_code == 200
    assert "items" in res.json()

    res = await client.get(f"/api/shipments/{shipment.id}", headers=headers)
    assert res.status_code == 200
    assert res.json()["id"] == shipment.id

    # Map
    res = await client.get("/api/map/positions/snapshot")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # Alerts
    res = await client.get("/api/alerts")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    res = await client.patch("/api/alerts/1/acknowledge")
    assert res.status_code == 200
    assert res.json()["status"] == "ACKNOWLEDGED"

    # Disruptions
    res = await client.get("/api/disruptions")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # Decisions
    res = await client.get("/api/decisions")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # Analytics
    res = await client.get("/api/analytics/scorecard")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # Copilot
    res = await client.post("/api/copilot/ask")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
