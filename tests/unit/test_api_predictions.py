"""
Unit tests for ML prediction API endpoints (T-039).

Tests validate:
- Happy path for all 3 endpoints (200 with required fields)
- provenance="DERIVED" on all success responses
- model_version and schema_version present
- No raw feature vectors in responses
- p10 <= p50 <= p85 for ETA
- Unknown demand lane → 404 UNKNOWN_LANE with provenance="SYSTEM"
- Malformed input → 422 validation error
- context field present (None) in all responses
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nexafreight.ml.constants import DELAY_MODEL_DIR, DEMAND_MODEL_DIR, ETA_MODEL_DIR
from nexafreight.ml.registry import ModelRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def registry() -> ModelRegistry:
    """Load real models once for the entire test module."""
    return ModelRegistry(
        delay_dir=DELAY_MODEL_DIR,
        eta_dir=ETA_MODEL_DIR,
        demand_dir=DEMAND_MODEL_DIR,
    )


@pytest.fixture(scope="module")
def client(registry: ModelRegistry) -> TestClient:
    """Create a test client with the registry wired into app state."""
    from fastapi import FastAPI

    from nexafreight.api.routes.predictions import router

    app = FastAPI()
    app.state.ml_registry = registry
    app.include_router(router)
    return TestClient(app)


@pytest.fixture()
def valid_features() -> dict:
    """A valid feature payload that both delay and ETA endpoints accept."""
    return {
        "shipping_mode": "SEA",
        "cargo_class": "STANDARD",
        "revenue": 500.0,
        "shipping_cost": 30.0,
        "scheduled_shipping_days": 5.0,
        "order_country": "Francia",
        "customer_country": "EE. UU.",
        "product_price": 100.0,
        "order_profit": 40.0,
        "sla_month": 6,
        "sla_weekday": 2,
        "sla_quarter": 2,
        "total_distance_km": 8000.0,
        "leg_count": 3,
    }


# ---------------------------------------------------------------------------
# POST /predict/delay
# ---------------------------------------------------------------------------
class TestDelayEndpoint:
    """Tests for POST /predict/delay."""

    def test_happy_path(self, client: TestClient, valid_features: dict) -> None:
        resp = client.post("/predict/delay", json=valid_features)
        assert resp.status_code == 200
        data = resp.json()
        assert "probability" in data
        assert "risk_band" in data
        assert data["risk_band"] in ("LOW", "MEDIUM", "HIGH")

    def test_provenance_is_derived(self, client: TestClient, valid_features: dict) -> None:
        data = client.post("/predict/delay", json=valid_features).json()
        assert data["provenance"] == "DERIVED"

    def test_model_version_present(self, client: TestClient, valid_features: dict) -> None:
        data = client.post("/predict/delay", json=valid_features).json()
        assert data["model_version"] == "1.0.0"
        assert data["schema_version"] == "1.0.0"

    def test_no_raw_features_in_response(self, client: TestClient, valid_features: dict) -> None:
        data = client.post("/predict/delay", json=valid_features).json()
        # None of the input feature names should appear as response keys
        feature_keys = {
            "shipping_mode",
            "cargo_class",
            "revenue",
            "shipping_cost",
            "scheduled_shipping_days",
            "order_country",
            "customer_country",
            "product_price",
            "order_profit",
            "sla_month",
            "sla_weekday",
            "sla_quarter",
            "total_distance_km",
            "leg_count",
        }
        response_keys = set(data.keys())
        leaked = feature_keys & response_keys
        assert not leaked, f"Raw features leaked into response: {leaked}"

    def test_context_field_present(self, client: TestClient, valid_features: dict) -> None:
        data = client.post("/predict/delay", json=valid_features).json()
        assert "context" in data
        assert data["context"] is None

    def test_malformed_input_returns_422(self, client: TestClient) -> None:
        resp = client.post("/predict/delay", json={"shipping_mode": "SEA"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /predict/eta
# ---------------------------------------------------------------------------
class TestEtaEndpoint:
    """Tests for POST /predict/eta."""

    def test_happy_path(self, client: TestClient, valid_features: dict) -> None:
        resp = client.post("/predict/eta", json=valid_features)
        assert resp.status_code == 200
        data = resp.json()
        assert "p10_eta_days" in data
        assert "p50_eta_days" in data
        assert "p85_eta_days" in data
        assert "confidence_interval_width" in data

    def test_quantile_ordering(self, client: TestClient, valid_features: dict) -> None:
        """P10 <= P50 <= P85 must always hold."""
        data = client.post("/predict/eta", json=valid_features).json()
        assert data["p10_eta_days"] <= data["p50_eta_days"]
        assert data["p50_eta_days"] <= data["p85_eta_days"]

    def test_provenance_is_derived(self, client: TestClient, valid_features: dict) -> None:
        data = client.post("/predict/eta", json=valid_features).json()
        assert data["provenance"] == "DERIVED"

    def test_model_version_present(self, client: TestClient, valid_features: dict) -> None:
        data = client.post("/predict/eta", json=valid_features).json()
        assert data["model_version"] == "1.0.0"
        assert data["schema_version"] == "1.0.0"

    def test_context_field_present(self, client: TestClient, valid_features: dict) -> None:
        data = client.post("/predict/eta", json=valid_features).json()
        assert "context" in data
        assert data["context"] is None


# ---------------------------------------------------------------------------
# GET /demand/forecast
# ---------------------------------------------------------------------------
class TestDemandEndpoint:
    """Tests for GET /demand/forecast."""

    def test_happy_path(self, client: TestClient, registry: ModelRegistry) -> None:
        """Hit a known lane from the available lanes list."""
        dm = registry.get_demand_model()
        lanes = dm.available_lanes
        assert len(lanes) > 0, "No lanes available for testing"

        # Pick the first available lane
        lane = lanes[0]
        resp = client.get(
            "/demand/forecast",
            params={
                "category": lane["category"],
                "region": lane["region"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "series" in data
        assert "horizon_snapshot" in data
        assert len(data["series"]) > 0

    def test_provenance_is_derived(self, client: TestClient, registry: ModelRegistry) -> None:
        dm = registry.get_demand_model()
        lane = dm.available_lanes[0]
        data = client.get(
            "/demand/forecast",
            params={"category": lane["category"], "region": lane["region"]},
        ).json()
        assert data["provenance"] == "DERIVED"

    def test_model_version_present(self, client: TestClient, registry: ModelRegistry) -> None:
        dm = registry.get_demand_model()
        lane = dm.available_lanes[0]
        data = client.get(
            "/demand/forecast",
            params={"category": lane["category"], "region": lane["region"]},
        ).json()
        assert data["model_version"] == "1.0.0"
        assert data["schema_version"] == "1.0.0"

    def test_unknown_lane_returns_404(self, client: TestClient) -> None:
        """Unknown (category, region) pair → 404 UNKNOWN_LANE."""
        resp = client.get(
            "/demand/forecast",
            params={
                "category": "NONEXISTENT_PRODUCT",
                "region": "NONEXISTENT_REGION",
            },
        )
        assert resp.status_code == 404
        data = resp.json()
        assert data["detail"]["error_code"] == "UNKNOWN_LANE"
        assert data["detail"]["provenance"] == "SYSTEM"

    def test_context_field_present(self, client: TestClient, registry: ModelRegistry) -> None:
        dm = registry.get_demand_model()
        lane = dm.available_lanes[0]
        data = client.get(
            "/demand/forecast",
            params={"category": lane["category"], "region": lane["region"]},
        ).json()
        assert "context" in data
        assert data["context"] is None
