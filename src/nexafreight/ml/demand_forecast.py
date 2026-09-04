"""
demand_forecast.py — Demand Forecasting inference module (Phase 3, T-038 / T-039).

Public API
----------
DemandForecast           Frozen dataclass returned by predict().
DemandForecastModel      Load trained AutoETS bundle and produce forecasts.
get_demand_model()       Convenience loader.

Design rules
------------
* Forecasts are keyed by (category_name, order_region) — same grouping as training.
* Unseen lanes return None gracefully (no crash).
* Chart-ready output is a list of dicts with keys:
    ds, yhat, yhat_lower, yhat_upper, is_forecast
  (historical actuals are included as is_forecast=False rows).
* Raw model objects NEVER appear in API responses.
* Every result carries provenance = "DERIVED".
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib

from nexafreight.ml.constants import (
    DEMAND_MODEL_DIR,
)


# ---------------------------------------------------------------------------
# Prediction dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DemandForecast:
    """
    Immutable forecast envelope for one (category × region) lane.

    Attributes
    ----------
    category        : Product category name (e.g. "Cleats").
    region          : Order region (e.g. "Western Europe").
    unique_id       : Stable lane identifier string.
    series          : Chart-ready list of dicts with keys
                      {ds, yhat, yhat_lower, yhat_upper, is_forecast}.
    horizon_30_days : Forecast dict for the closest week to +30 days.
    horizon_60_days : Forecast dict for the closest week to +60 days.
    horizon_90_days : Forecast dict for the closest week to +90 days.
    provenance      : Always "DERIVED".
    """
    category: str
    region: str
    unique_id: str

    series: list[dict[str, Any]]

    horizon_30_days: dict[str, Any] | None
    horizon_60_days: dict[str, Any] | None
    horizon_90_days: dict[str, Any] | None

    provenance: str = "DERIVED"


def _make_lane_id(category: str, region: str) -> str:
    """Match the lane key format used during training."""
    return f"{category}__{region}"


def _closest_forecast_row(
    series: list[dict[str, Any]],
    target_days: int,
) -> dict[str, Any] | None:
    """Return the forecast row whose ds is closest to history_end + target_days."""
    forecast_rows = [r for r in series if r.get("is_forecast")]
    if not forecast_rows:
        return None
    # Sort by ds and pick by index approximation (7 days per week)
    target_idx = target_days // 7 - 1  # 0-indexed
    target_idx = max(0, min(target_idx, len(forecast_rows) - 1))
    return forecast_rows[target_idx]


# ---------------------------------------------------------------------------
# Model class
# ---------------------------------------------------------------------------
class DemandForecastModel:
    """
    Inference wrapper around the trained StatsForecast AutoETS bundle
    (artifact produced by 12_train_demand_forecast.py).

    Usage
    -----
    model = DemandForecastModel()
    model.load("models/demand_forecast")
    forecast = model.predict("Cleats", "Western Europe")
    """

    def __init__(self) -> None:
        self._sf: Any = None
        self._lane_index: dict[str, dict[str, str]] = {}
        self._qualified_ids: list[str] = []
        self._forecasts_cache: dict[str, dict[str, Any]] = {}
        self._model_version: str | None = None
        self._is_loaded: bool = False

    # ------------------------------------------------------------------
    # Load from disk
    # ------------------------------------------------------------------
    def load(self, model_dir: str | Path = DEMAND_MODEL_DIR) -> None:
        """Load a trained artifact bundle produced by 12_train_demand_forecast.py."""
        model_dir = Path(model_dir)
        model_path = model_dir / "model.joblib"
        forecasts_path = model_dir / "forecasts.json"

        if not model_path.exists():
            raise FileNotFoundError(f"Demand model artifact not found: {model_path}")
        if not forecasts_path.exists():
            raise FileNotFoundError(f"Demand forecasts JSON not found: {forecasts_path}")

        artifact = joblib.load(model_path)
        self._sf = artifact["sf"]
        self._lane_index = artifact.get("lane_index", {})
        self._qualified_ids = artifact.get("qualified_ids", [])
        self._model_version = artifact.get("model_version", "unknown")

        import json
        self._forecasts_cache = json.loads(forecasts_path.read_text(encoding="utf-8"))

        self._is_loaded = True

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------
    @property
    def available_lanes(self) -> list[dict[str, str]]:
        """Return list of {unique_id, category, region} for all trained lanes."""
        return [
            {"unique_id": uid, **meta}
            for uid, meta in self._lane_index.items()
        ]

    @property
    def model_version(self) -> str | None:
        return self._model_version

    # ------------------------------------------------------------------
    # Predict
    # ------------------------------------------------------------------
    def predict(
        self,
        category: str,
        region: str,
    ) -> DemandForecast | None:
        """
        Return the forecast for a (category, region) lane.

        Parameters
        ----------
        category : str   Product category name (must match training values).
        region   : str   Order region name (must match training values).

        Returns
        -------
        DemandForecast if the lane exists, None if unseen.
        """
        if not self._is_loaded:
            raise RuntimeError(
                "Model not loaded — call load() or use get_demand_model()."
            )

        uid = _make_lane_id(category.strip(), region.strip())

        if uid not in self._forecasts_cache:
            return None

        lane_data = self._forecasts_cache[uid]
        series = lane_data.get("series", [])

        return DemandForecast(
            category=lane_data.get("category", category),
            region=lane_data.get("region", region),
            unique_id=uid,
            series=series,
            horizon_30_days=_closest_forecast_row(series, 30),
            horizon_60_days=_closest_forecast_row(series, 60),
            horizon_90_days=_closest_forecast_row(series, 90),
        )

    def predict_batch(
        self,
        lanes: list[dict[str, str]],
    ) -> dict[str, DemandForecast | None]:
        """
        Batch prediction for a list of {category, region} dicts.

        Returns a dict keyed by unique_id.
        """
        return {
            _make_lane_id(lane["category"], lane["region"]): self.predict(
                lane["category"], lane["region"]
            )
            for lane in lanes
        }

    def get_all_forecasts(self) -> dict[str, DemandForecast]:
        """
        Return DemandForecast objects for every trained lane.
        Useful for the Analytics page which renders all lanes at once.
        """
        if not self._is_loaded:
            raise RuntimeError("Model not loaded.")

        results: dict[str, DemandForecast] = {}
        for uid, lane_data in self._forecasts_cache.items():
            series = lane_data.get("series", [])
            results[uid] = DemandForecast(
                category=lane_data.get("category", ""),
                region=lane_data.get("region", ""),
                unique_id=uid,
                series=series,
                horizon_30_days=_closest_forecast_row(series, 30),
                horizon_60_days=_closest_forecast_row(series, 60),
                horizon_90_days=_closest_forecast_row(series, 90),
            )
        return results


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------
def get_demand_model(
    model_dir: str | Path = DEMAND_MODEL_DIR,
) -> DemandForecastModel:
    """Load and return a ready-to-predict DemandForecastModel."""
    model = DemandForecastModel()
    model.load(model_dir)
    return model
