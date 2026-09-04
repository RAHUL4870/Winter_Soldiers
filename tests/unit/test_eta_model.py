"""
test_eta_model.py — Unit tests for the LightGBM Quantile ETA Regressor (Phase 3, T-037).

All tests use synthetic fixtures and test pinball loss, coverage metrics,
SLA risk assessment, prediction schema, and monotonicity guarantees.
Never touches real DB or raw CSV. Runs in < 2 seconds.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from nexafreight.ml.constants import (
    BANNED_COLUMNS,
    CATEGORICAL_COLUMNS,
    FEATURE_COLUMNS,
    NUMERIC_COLUMNS,
)
from nexafreight.ml.eta_model import (
    EtaPrediction,
    EtaQuantileModel,
    interval_coverage,
    pinball_loss,
)


# ---------------------------------------------------------------------------
# Synthetic Booster Mock for Unit Tests
# ---------------------------------------------------------------------------
class _MockBooster:
    def __init__(self, offset: float) -> None:
        self.offset = offset

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        n = len(X)
        sched = (
            X["scheduled_shipping_days"].to_numpy(dtype=float)
            if "scheduled_shipping_days" in X
            else np.full(n, 4.0)
        )
        return np.full(n, self.offset) + (sched * 0.05)


def _make_mock_model(
    p10_offset: float = -1.5,
    p50_offset: float = 0.2,
    p85_offset: float = 2.5,
) -> EtaQuantileModel:
    model = EtaQuantileModel()
    model.models = {
        "p10": _MockBooster(p10_offset),
        "p50": _MockBooster(p50_offset),
        "p85": _MockBooster(p85_offset),
    }
    model.features = list(FEATURE_COLUMNS)
    model.cat_cols = list(CATEGORICAL_COLUMNS)
    model.num_cols = list(NUMERIC_COLUMNS)
    model.cat_levels = {
        "shipping_mode": ["AIR", "SEA", "RAIL"],
        "cargo_class": ["STANDARD", "FRAGILE", "HAZMAT"],
        "order_country": ["USA", "China", "Germany"],
        "customer_country": ["USA", "UK", "India"],
    }
    model._is_loaded = True
    return model


@pytest.fixture()
def mock_eta_model() -> EtaQuantileModel:
    return _make_mock_model()


@pytest.fixture()
def synthetic_row() -> dict:
    return {
        "shipping_mode": "SEA",
        "cargo_class": "STANDARD",
        "revenue": 350.0,
        "shipping_cost": 45.0,
        "scheduled_shipping_days": 4.0,
        "order_country": "USA",
        "customer_country": "UK",
        "product_price": 50.0,
        "order_profit": 75.0,
        "sla_month": 5,
        "sla_weekday": 2,
        "sla_quarter": 2,
        "total_distance_km": 6500.0,
        "leg_count": 3,
    }


# ---------------------------------------------------------------------------
# Test Pinball Loss
# ---------------------------------------------------------------------------
class TestPinballLoss:
    def test_pinball_loss_underprediction(self) -> None:
        loss = pinball_loss([5.0], [3.0], 0.85)
        assert loss == pytest.approx(1.70, rel=1e-4)

    def test_pinball_loss_overprediction(self) -> None:
        loss = pinball_loss([3.0], [5.0], 0.85)
        assert loss == pytest.approx(0.30, rel=1e-4)

    def test_pinball_loss_zero_error(self) -> None:
        loss = pinball_loss([4.0, 7.0], [4.0, 7.0], 0.50)
        assert loss == pytest.approx(0.0, abs=1e-9)

    def test_pinball_loss_symmetry_at_median(self) -> None:
        loss_under = pinball_loss([5.0], [3.0], 0.50)
        loss_over = pinball_loss([3.0], [5.0], 0.50)
        assert loss_under == pytest.approx(1.0, rel=1e-4)
        assert loss_over == pytest.approx(1.0, rel=1e-4)

    def test_invalid_alpha_raises(self) -> None:
        with pytest.raises(ValueError):
            pinball_loss([1.0], [1.0], 0.0)
        with pytest.raises(ValueError):
            pinball_loss([1.0], [1.0], 1.0)
        with pytest.raises(ValueError):
            pinball_loss([1.0], [1.0], -0.1)


# ---------------------------------------------------------------------------
# Test Interval Coverage
# ---------------------------------------------------------------------------
class TestIntervalCoverage:
    def test_full_coverage(self) -> None:
        assert interval_coverage([1.0, 2.0], [0.0, 1.0], [2.0, 3.0]) == 1.0

    def test_partial_coverage(self) -> None:
        cov = interval_coverage([1.0, 10.0, 3.0, -5.0],
                                [0.0, 0.0, 0.0, 0.0],
                                [5.0, 5.0, 5.0, 5.0])
        assert cov == 0.50

    def test_zero_coverage(self) -> None:
        assert interval_coverage([10.0, 20.0], [0.0, 0.0], [5.0, 5.0]) == 0.0

    def test_boundary_inclusive(self) -> None:
        assert interval_coverage([0.0, 5.0], [0.0, 0.0], [5.0, 5.0]) == 1.0


# ---------------------------------------------------------------------------
# Test SLA Risk Classification
# ---------------------------------------------------------------------------
class TestSlaRiskClassification:
    NOW = datetime(2026, 9, 1, 10, 0)

    def test_on_time(self) -> None:
        risk = EtaQuantileModel.assess_sla_risk(
            self.NOW + timedelta(days=2),
            self.NOW + timedelta(days=4),
            self.NOW + timedelta(days=5),
            self.NOW + timedelta(days=6),
        )
        assert risk == "ON_TIME"

    def test_medium_risk(self) -> None:
        risk = EtaQuantileModel.assess_sla_risk(
            self.NOW + timedelta(days=2),
            self.NOW + timedelta(days=4),
            self.NOW + timedelta(days=6),
            self.NOW + timedelta(days=4, hours=12),
        )
        assert risk == "MEDIUM"

    def test_high_risk(self) -> None:
        risk = EtaQuantileModel.assess_sla_risk(
            self.NOW + timedelta(days=2),
            self.NOW + timedelta(days=4),
            self.NOW + timedelta(days=6),
            self.NOW + timedelta(days=3),
        )
        assert risk == "HIGH"

    def test_breach(self) -> None:
        risk = EtaQuantileModel.assess_sla_risk(
            self.NOW + timedelta(days=2),
            self.NOW + timedelta(days=4),
            self.NOW + timedelta(days=6),
            self.NOW + timedelta(days=1),
        )
        assert risk == "BREACH"

    def test_exact_boundary_p85_equals_sla(self) -> None:
        risk = EtaQuantileModel.assess_sla_risk(
            self.NOW + timedelta(days=2),
            self.NOW + timedelta(days=4),
            self.NOW + timedelta(days=6),
            self.NOW + timedelta(days=6),
        )
        assert risk == "ON_TIME"


# ---------------------------------------------------------------------------
# Test EtaQuantileModel Inference
# ---------------------------------------------------------------------------
class TestEtaQuantileModelInference:
    dep_time = datetime(2026, 9, 1, 12, 0)
    sla_time = datetime(2026, 9, 7, 12, 0)

    def test_single_prediction_schema(self, mock_eta_model, synthetic_row) -> None:
        pred: EtaPrediction = mock_eta_model.predict(
            synthetic_row,
            departure_time=self.dep_time,
            sla_deadline=self.sla_time,
        )

        assert isinstance(pred, EtaPrediction)
        assert pred.p10_eta_days <= pred.p50_eta_days <= pred.p85_eta_days
        assert pred.p10_residual <= pred.p50_residual <= pred.p85_residual
        assert pred.confidence_interval_width >= 0.0
        assert pred.sla_risk in ("ON_TIME", "MEDIUM", "HIGH", "BREACH")
        assert pred.provenance == "DERIVED"
        assert pred.p10_eta_date is not None
        assert pred.p50_eta_date is not None
        assert pred.p85_eta_date is not None
        assert pred.p10_eta_date <= pred.p50_eta_date <= pred.p85_eta_date

    def test_batch_prediction(self, mock_eta_model, synthetic_row) -> None:
        df = pd.DataFrame([synthetic_row, synthetic_row, synthetic_row])
        preds = mock_eta_model.predict(df)

        assert isinstance(preds, list)
        assert len(preds) == 3
        for p in preds:
            assert p.p10_eta_days <= p.p50_eta_days <= p.p85_eta_days

    def test_days_elapsed(self, mock_eta_model, synthetic_row) -> None:
        pred_start: EtaPrediction = mock_eta_model.predict(synthetic_row, days_elapsed=0.0)
        pred_mid: EtaPrediction = mock_eta_model.predict(synthetic_row, days_elapsed=2.0)

        assert pred_start.p50_eta_days == pred_mid.p50_eta_days
        assert pred_mid.p50_remaining_days == pytest.approx(
            max(0.0, pred_start.p50_eta_days - 2.0), rel=1e-2
        )

    def test_monotonicity_even_on_crossing_predictions(self, synthetic_row) -> None:
        bad_model = _make_mock_model(p10_offset=3.0, p50_offset=1.0, p85_offset=-1.0)

        pred: EtaPrediction = bad_model.predict(synthetic_row)
        assert pred.p10_residual <= pred.p50_residual <= pred.p85_residual
        assert pred.p10_eta_days <= pred.p50_eta_days <= pred.p85_eta_days

    def test_unseen_category_degraded(self, mock_eta_model) -> None:
        row = {
            "shipping_mode": "TELEPORT",
            "cargo_class": "STANDARD",
            "revenue": 100.0,
            "shipping_cost": 20.0,
            "scheduled_shipping_days": 3.0,
            "order_country": "USA",
            "customer_country": "UK",
            "product_price": 30.0,
            "order_profit": 50.0,
            "sla_month": 6,
            "sla_weekday": 3,
            "sla_quarter": 2,
            "total_distance_km": 1000.0,
            "leg_count": 1,
        }
        pred: EtaPrediction = mock_eta_model.predict(row)
        assert isinstance(pred, EtaPrediction)

    def test_unloaded_model_raises(self) -> None:
        model = EtaQuantileModel()
        with pytest.raises(RuntimeError):
            model.predict({"shipping_mode": "AIR"})

    def test_negative_days_elapsed_raises(self, mock_eta_model, synthetic_row) -> None:
        with pytest.raises(ValueError):
            mock_eta_model.predict(synthetic_row, days_elapsed=-1.0)


# ---------------------------------------------------------------------------
# Test Feature Integrity and Leakage
# ---------------------------------------------------------------------------
class TestFeatureIntegrityAndLeakage:
    def test_banned_columns_not_in_features(self, mock_eta_model) -> None:
        for col in BANNED_COLUMNS:
            assert col not in mock_eta_model.features, f"Banned '{col}' in features"

    def test_congestion_features_excluded(self, mock_eta_model) -> None:
        assert "origin_congestion" not in mock_eta_model.features
        assert "dest_congestion" not in mock_eta_model.features

    def test_target_not_in_features(self, mock_eta_model) -> None:
        assert "days_for_shipping_real" not in mock_eta_model.features
        assert "transit_delay_residual" not in mock_eta_model.features

    def test_banned_column_injection_raises(self, mock_eta_model, synthetic_row) -> None:
        features = list(mock_eta_model.features)
        mock_eta_model.features = features + ["planned_departure"]
        with pytest.raises(RuntimeError, match="BANNED"):
            mock_eta_model.predict(synthetic_row)
        mock_eta_model.features = features

    def test_quantile_keys_match_constants(self, mock_eta_model) -> None:
        from nexafreight.ml.constants import QUANTILE_KEYS
        for key in QUANTILE_KEYS:
            assert key in mock_eta_model.models
        assert list(mock_eta_model.models.keys()) == list(QUANTILE_KEYS)

    def test_script_11_has_extensibility_schema(self) -> None:
        from pathlib import Path
        script_path = Path(__file__).parent.parent.parent / "scripts" / "11_train_eta_model.py"
        content = script_path.read_text(encoding="utf-8")
        assert '"schema_version": "1.0.0"' in content
        assert '"active_disruption_near_dest"' in content
        assert '"news_risk_score"' in content
        assert '"extensibility"' in content
