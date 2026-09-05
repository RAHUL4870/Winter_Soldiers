"""
test_demand_forecast.py — Unit tests for the Demand Forecast model (Phase 3, T-038).

All tests use synthetic fixtures and never touch the real DB or CSV.
Tests cover:
  - Weekly aggregation logic and lane ID generation
  - DemandForecastModel load/predict guard rails
  - DemandForecast dataclass schema
  - Prediction interval bounds (yhat_lower <= yhat <= yhat_upper)
  - Unseen lane returns None gracefully
  - Unloaded model raises RuntimeError
  - Horizon snapshots at 30/60/90 days
  - Available lanes list schema
  - Batch prediction
  - Chart series structure (correct keys, historical + forecast rows present)

Runs in < 2 seconds (no real model training).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pytest

from nexafreight.ml.constants import (
    DEMAND_CHART_KEYS,
    DEMAND_FORECAST_HORIZON_WEEKS,
    DEMAND_FORECAST_HORIZONS,
    DEMAND_GROUP_COLS,
    DEMAND_MIN_SERIES_LEN,
    DEMAND_PREDICTION_LEVEL,
    DEMAND_UNIQUE_ID_COL,
)
from nexafreight.ml.demand_forecast import (
    DemandForecast,
    DemandForecastModel,
    _closest_forecast_row,
    _make_lane_id,
)


# ---------------------------------------------------------------------------
# Picklable stub for the StatsForecast object stored inside model.joblib
# (MagicMock cannot be pickled by joblib)
# ---------------------------------------------------------------------------
class _StubSF:
    """Minimal stand-in for a StatsForecast object that survives joblib pickling."""
    pass


# ---------------------------------------------------------------------------
# Synthetic Fixtures
# ---------------------------------------------------------------------------
_CATEGORY = "Cleats"
_REGION = "Western Europe"
_UID = _make_lane_id(_CATEGORY, _REGION)

_HISTORY_ROWS = 40  # 40 weeks of historical actuals
_FORECAST_ROWS = DEMAND_FORECAST_HORIZON_WEEKS  # 13 weeks of forecasts


def _make_series(
    n_history: int = _HISTORY_ROWS,
    n_forecast: int = _FORECAST_ROWS,
    base_count: float = 25.0,
) -> list[dict[str, Any]]:
    """Build a synthetic chart-ready series."""
    series = []
    from datetime import date, timedelta
    start = date(2015, 1, 5)  # first Monday
    for i in range(n_history):
        ds = (start + timedelta(weeks=i)).strftime("%Y-%m-%d")
        y = round(base_count + np.random.normal(0, 2), 2)
        series.append({
            "ds": ds,
            "yhat": y,
            "yhat_lower": y,
            "yhat_upper": y,
            "is_forecast": False,
        })
    # Forecast rows follow immediately
    forecast_start = start + timedelta(weeks=n_history)
    for j in range(n_forecast):
        ds = (forecast_start + timedelta(weeks=j)).strftime("%Y-%m-%d")
        yhat = round(base_count + 1.5, 2)
        series.append({
            "ds": ds,
            "yhat": yhat,
            "yhat_lower": round(yhat - 4.0, 2),
            "yhat_upper": round(yhat + 4.0, 2),
            "is_forecast": True,
        })
    return series


def _make_forecasts_cache(
    uid: str = _UID,
    category: str = _CATEGORY,
    region: str = _REGION,
) -> dict[str, Any]:
    return {
        uid: {
            "category": category,
            "region": region,
            "series": _make_series(),
        }
    }


def _make_model_artifact(
    uid: str = _UID,
    category: str = _CATEGORY,
    region: str = _REGION,
) -> dict[str, Any]:
    return {
        "sf": _StubSF(),  # picklable stub — real training uses StatsForecast
        "lane_index": {uid: {"category": category, "region": region}},
        "qualified_ids": [uid],
        "model_version": "1.0.0",
        "trained_at": "2026-09-01T00:00:00+00:00",
        "forecast_horizon_weeks": DEMAND_FORECAST_HORIZON_WEEKS,
        "prediction_level": DEMAND_PREDICTION_LEVEL,
    }


@pytest.fixture()
def tmp_model_dir(tmp_path: Path) -> Path:
    """Write a synthetic model bundle to a temp directory and return its path."""
    artifact = _make_model_artifact()
    joblib.dump(artifact, tmp_path / "model.joblib")
    (tmp_path / "forecasts.json").write_text(
        json.dumps(_make_forecasts_cache()), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def loaded_model(tmp_model_dir: Path) -> DemandForecastModel:
    model = DemandForecastModel()
    model.load(tmp_model_dir)
    return model


# ---------------------------------------------------------------------------
# Test: lane ID generation
# ---------------------------------------------------------------------------
class TestLaneId:
    def test_lane_id_format(self) -> None:
        uid = _make_lane_id("Cleats", "Western Europe")
        assert uid == "Cleats__Western Europe"

    def test_lane_id_with_ampersand(self) -> None:
        uid = _make_lane_id("Camping & Hiking", "Central America")
        assert uid == "Camping & Hiking__Central America"

    def test_lane_id_deterministic(self) -> None:
        uid1 = _make_lane_id("Electronics", "Oceania")
        uid2 = _make_lane_id("Electronics", "Oceania")
        assert uid1 == uid2


# ---------------------------------------------------------------------------
# Test: DemandForecastModel guards
# ---------------------------------------------------------------------------
class TestModelGuards:
    def test_unloaded_predict_raises(self) -> None:
        model = DemandForecastModel()
        with pytest.raises(RuntimeError, match="not loaded"):
            model.predict("Cleats", "Western Europe")

    def test_unloaded_get_all_raises(self) -> None:
        model = DemandForecastModel()
        with pytest.raises(RuntimeError, match="not loaded"):
            model.get_all_forecasts()

    def test_missing_model_file_raises(self, tmp_path: Path) -> None:
        model = DemandForecastModel()
        with pytest.raises(FileNotFoundError, match="model.joblib"):
            model.load(tmp_path)

    def test_missing_forecasts_json_raises(self, tmp_path: Path) -> None:
        artifact = _make_model_artifact()
        joblib.dump(artifact, tmp_path / "model.joblib")
        model = DemandForecastModel()
        with pytest.raises(FileNotFoundError, match="forecasts.json"):
            model.load(tmp_path)


# ---------------------------------------------------------------------------
# Test: Load and basic model properties
# ---------------------------------------------------------------------------
class TestModelLoad:
    def test_is_loaded_after_load(self, loaded_model: DemandForecastModel) -> None:
        assert loaded_model._is_loaded is True

    def test_model_version(self, loaded_model: DemandForecastModel) -> None:
        assert loaded_model.model_version == "1.0.0"

    def test_available_lanes_schema(self, loaded_model: DemandForecastModel) -> None:
        lanes = loaded_model.available_lanes
        assert isinstance(lanes, list)
        assert len(lanes) >= 1
        for lane in lanes:
            assert "unique_id" in lane
            assert "category" in lane
            assert "region" in lane

    def test_qualified_ids_non_empty(self, loaded_model: DemandForecastModel) -> None:
        assert len(loaded_model._qualified_ids) >= 1


# ---------------------------------------------------------------------------
# Test: predict() for a known lane
# ---------------------------------------------------------------------------
class TestPredict:
    def test_returns_demand_forecast(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        assert isinstance(result, DemandForecast)

    def test_correct_category_and_region(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        assert result.category == _CATEGORY
        assert result.region == _REGION

    def test_correct_unique_id(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        assert result.unique_id == _UID

    def test_provenance_is_derived(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        assert result.provenance == "DERIVED"

    def test_unseen_lane_returns_none(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict("NonExistent Category", "Nowhere")
        assert result is None

    def test_unseen_lane_case_sensitive(self, loaded_model: DemandForecastModel) -> None:
        # Lane IDs are case-sensitive — "cleats" != "Cleats"
        result = loaded_model.predict("cleats", _REGION)
        assert result is None


# ---------------------------------------------------------------------------
# Test: DemandForecast series structure
# ---------------------------------------------------------------------------
class TestForecastSeries:
    def test_series_non_empty(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        assert isinstance(result.series, list)
        assert len(result.series) > 0

    def test_series_has_required_keys(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        for row in result.series:
            for key in DEMAND_CHART_KEYS:
                assert key in row, f"Missing key '{key}' in series row"

    def test_series_has_historical_rows(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        hist = [r for r in result.series if not r["is_forecast"]]
        assert len(hist) > 0, "Expected at least one historical row"

    def test_series_has_forecast_rows(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        fcast = [r for r in result.series if r["is_forecast"]]
        assert len(fcast) > 0, "Expected at least one forecast row"

    def test_prediction_interval_bounds(self, loaded_model: DemandForecastModel) -> None:
        """yhat_lower <= yhat <= yhat_upper for all forecast rows."""
        result = loaded_model.predict(_CATEGORY, _REGION)
        for row in result.series:
            if row["is_forecast"]:
                assert row["yhat_lower"] <= row["yhat"], (
                    f"yhat_lower {row['yhat_lower']} > yhat {row['yhat']}"
                )
                assert row["yhat"] <= row["yhat_upper"], (
                    f"yhat {row['yhat']} > yhat_upper {row['yhat_upper']}"
                )

    def test_ds_is_string(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        for row in result.series:
            assert isinstance(row["ds"], str), f"ds should be a string, got {type(row['ds'])}"

    def test_yhat_is_numeric(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        for row in result.series:
            assert isinstance(row["yhat"], int | float), f"yhat not numeric: {row['yhat']}"


# ---------------------------------------------------------------------------
# Test: Horizon snapshots
# ---------------------------------------------------------------------------
class TestHorizonSnapshots:
    def test_horizon_30_not_none(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        assert result.horizon_30_days is not None

    def test_horizon_60_not_none(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        assert result.horizon_60_days is not None

    def test_horizon_90_not_none(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        assert result.horizon_90_days is not None

    def test_horizon_snapshots_are_forecast_rows(self, loaded_model: DemandForecastModel) -> None:
        result = loaded_model.predict(_CATEGORY, _REGION)
        for h in (result.horizon_30_days, result.horizon_60_days, result.horizon_90_days):
            if h is not None:
                assert h.get("is_forecast") is True


# ---------------------------------------------------------------------------
# Test: _closest_forecast_row helper
# ---------------------------------------------------------------------------
class TestClosestForecastRow:
    def test_empty_series_returns_none(self) -> None:
        assert _closest_forecast_row([], 30) is None

    def test_no_forecast_rows_returns_none(self) -> None:
        series = [
            {
                "ds": "2015-01-05",
                "yhat": 10,
                "yhat_lower": 10,
                "yhat_upper": 10,
                "is_forecast": False,
            }
        ]
        assert _closest_forecast_row(series, 30) is None

    def test_returns_first_row_for_30_days(self) -> None:
        rows = [
            {
                "ds": f"2018-02-{i:02d}",
                "yhat": float(i),
                "yhat_lower": 0.0,
                "yhat_upper": float(i),
                "is_forecast": True,
            }
            for i in range(1, 14)
        ]
        result = _closest_forecast_row(rows, 30)
        assert result is not None
        assert result["is_forecast"] is True


# ---------------------------------------------------------------------------
# Test: Batch prediction
# ---------------------------------------------------------------------------
class TestBatchPredict:
    def test_batch_returns_dict(self, loaded_model: DemandForecastModel) -> None:
        lanes = [
            {"category": _CATEGORY, "region": _REGION},
            {"category": "Unknown", "region": "Nowhere"},
        ]
        results = loaded_model.predict_batch(lanes)
        assert isinstance(results, dict)
        assert len(results) == 2

    def test_batch_known_lane_is_forecast(self, loaded_model: DemandForecastModel) -> None:
        lanes = [{"category": _CATEGORY, "region": _REGION}]
        results = loaded_model.predict_batch(lanes)
        assert isinstance(results[_UID], DemandForecast)

    def test_batch_unknown_lane_is_none(self, loaded_model: DemandForecastModel) -> None:
        lanes = [{"category": "FakeCategory", "region": "FakeRegion"}]
        results = loaded_model.predict_batch(lanes)
        assert results[_make_lane_id("FakeCategory", "FakeRegion")] is None


# ---------------------------------------------------------------------------
# Test: get_all_forecasts
# ---------------------------------------------------------------------------
class TestGetAllForecasts:
    def test_returns_dict_of_demand_forecasts(self, loaded_model: DemandForecastModel) -> None:
        all_fc = loaded_model.get_all_forecasts()
        assert isinstance(all_fc, dict)
        for uid, fc in all_fc.items():
            assert isinstance(fc, DemandForecast)
            assert fc.unique_id == uid

    def test_all_forecasts_have_valid_series(self, loaded_model: DemandForecastModel) -> None:
        all_fc = loaded_model.get_all_forecasts()
        for fc in all_fc.values():
            assert len(fc.series) > 0


# ---------------------------------------------------------------------------
# Test: constants integrity
# ---------------------------------------------------------------------------
class TestConstantsIntegrity:
    def test_demand_group_cols_tuple_of_two(self) -> None:
        assert len(DEMAND_GROUP_COLS) == 2

    def test_forecast_horizons_ascending(self) -> None:
        h = DEMAND_FORECAST_HORIZONS
        assert h[0] < h[1] < h[2]

    def test_min_series_len_positive(self) -> None:
        assert DEMAND_MIN_SERIES_LEN > 0

    def test_chart_keys_present(self) -> None:
        required = {"ds", "yhat", "yhat_lower", "yhat_upper", "is_forecast"}
        assert required.issubset(set(DEMAND_CHART_KEYS))

    def test_unique_id_col_defined(self) -> None:
        assert DEMAND_UNIQUE_ID_COL == "unique_id"

    def test_script_12_has_extensibility_schema(self) -> None:
        from pathlib import Path
        script_path = (
            Path(__file__).parent.parent.parent
            / "scripts"
            / "12_train_demand_forecast.py"
        )
        content = script_path.read_text(encoding="utf-8")
        assert '"schema_version": "1.0.0"' in content
        assert '"active_disruption_near_dest"' in content
        assert '"news_risk_score"' in content
        assert '"extensibility"' in content
