"""
nexafreight.ml — Machine-learning models and inference registry.

T-036 exports: DelayClassifier (if exists)
T-037 exports: EtaQuantileModel, EtaPrediction, pinball_loss, interval_coverage
T-038 exports: DemandForecastModel, DemandForecast, get_demand_model
"""

from __future__ import annotations

from nexafreight.ml.demand_forecast import (
    DemandForecast,
    DemandForecastModel,
    get_demand_model,
)
from nexafreight.ml.eta_model import (
    EtaPrediction,
    EtaQuantileModel,
    interval_coverage,
    pinball_loss,
)

__all__ = [
    # T-037 — ETA Quantile Regressor
    "EtaPrediction",
    "EtaQuantileModel",
    "interval_coverage",
    "pinball_loss",
    # T-038 — Demand Forecast
    "DemandForecast",
    "DemandForecastModel",
    "get_demand_model",
]
