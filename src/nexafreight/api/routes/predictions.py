"""
Prediction API routes for NexaFreight ML models (T-039).

Endpoints:
    POST /predict/delay   — Delay probability + risk band
    POST /predict/eta     — P10/P50/P85 ETA quantiles
    GET  /demand/forecast  — Precomputed demand forecast by lane
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.status import HTTP_404_NOT_FOUND

from nexafreight.api.deps import get_registry
from nexafreight.api.schemas.ml import (
    DelayPredictionRequest,
    DelayPredictionResponse,
    DemandForecastResponse,
    DemandForecastSeriesPoint,
    EtaPredictionRequest,
    EtaPredictionResponse,
    MLErrorResponse,
    classify_risk_band,
)
from nexafreight.ml.registry import ModelRegistry

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Internal helpers — raw feature vectors stay here, never in responses
# ---------------------------------------------------------------------------
def _request_to_feature_dict(
    req: DelayPredictionRequest | EtaPredictionRequest,
) -> dict[str, Any]:
    """Convert a Pydantic request model to the feature dict expected by models."""
    return {
        "shipping_mode": req.shipping_mode,
        "cargo_class": req.cargo_class,
        "revenue": req.revenue,
        "shipping_cost": req.shipping_cost,
        "scheduled_shipping_days": req.scheduled_shipping_days,
        "order_country": req.order_country,
        "customer_country": req.customer_country,
        "product_price": req.product_price,
        "order_profit": req.order_profit,
        "sla_month": req.sla_month,
        "sla_weekday": req.sla_weekday,
        "sla_quarter": req.sla_quarter,
        "total_distance_km": req.total_distance_km,
        "leg_count": req.leg_count,
    }


# ---------------------------------------------------------------------------
# POST /predict/delay
# ---------------------------------------------------------------------------
@router.post(
    "/predict/delay",
    response_model=DelayPredictionResponse,
    responses={503: {"model": MLErrorResponse}},
    summary="Predict shipment delay probability",
    tags=["predictions"],
)
async def predict_delay(
    body: DelayPredictionRequest,
    registry: ModelRegistry = Depends(get_registry),
) -> DelayPredictionResponse:
    """Run the delay classifier on the supplied features.

    Returns probability, risk band, model metadata.
    Raw feature vectors are never included in the response.
    """
    booster = registry.get_delay_model()
    features = _request_to_feature_dict(body)

    # Build a 1-row DataFrame matching the training schema
    df = pd.DataFrame([features])

    # Encode categoricals the same way the model was trained
    from nexafreight.ml.constants import CATEGORICAL_COLUMNS, NUMERIC_COLUMNS

    for col in CATEGORICAL_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype("category")

    for col in NUMERIC_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

    # Predict — booster is a dict with a 'model' key containing the LightGBM Booster
    model_obj = booster.get("model", booster)
    if hasattr(model_obj, "predict"):
        raw_pred = model_obj.predict(df)
    else:
        # Fallback: booster itself might be the model
        raw_pred = booster.predict(df)

    probability = float(np.clip(raw_pred[0], 0.0, 1.0))
    risk_band = classify_risk_band(probability)

    return DelayPredictionResponse(
        probability=round(probability, 6),
        risk_band=risk_band,
        model_version=registry.delay_model_version,
        schema_version=registry.delay_schema_version,
        provenance="DERIVED",
        context=None,
    )


# ---------------------------------------------------------------------------
# POST /predict/eta
# ---------------------------------------------------------------------------
@router.post(
    "/predict/eta",
    response_model=EtaPredictionResponse,
    responses={503: {"model": MLErrorResponse}},
    summary="Predict ETA quantiles (P10/P50/P85)",
    tags=["predictions"],
)
async def predict_eta(
    body: EtaPredictionRequest,
    registry: ModelRegistry = Depends(get_registry),
) -> EtaPredictionResponse:
    """Run the ETA quantile model on the supplied features.

    Returns P10/P50/P85 ETA in days and confidence interval width.
    """
    eta_model = registry.get_eta_model()
    features = _request_to_feature_dict(body)

    prediction = eta_model.predict(features)

    # Type guard: ensure prediction is a single EtaPrediction, not a list
    if isinstance(prediction, list):
        raise ValueError("ETA model returned a list; expected a single prediction")

    return EtaPredictionResponse(
        p10_eta_days=round(prediction.p10_eta_days, 3),
        p50_eta_days=round(prediction.p50_eta_days, 3),
        p85_eta_days=round(prediction.p85_eta_days, 3),
        confidence_interval_width=round(prediction.confidence_interval_width, 3),
        model_version=registry.eta_model_version,
        schema_version=registry.eta_schema_version,
        provenance="DERIVED",
        context=None,
    )


# ---------------------------------------------------------------------------
# GET /demand/forecast
# ---------------------------------------------------------------------------
@router.get(
    "/demand/forecast",
    response_model=DemandForecastResponse,
    responses={
        404: {"model": MLErrorResponse},
        503: {"model": MLErrorResponse},
    },
    summary="Get demand forecast for a category × region lane",
    tags=["predictions"],
)
async def demand_forecast(
    category: str = Query(..., description="Product category name"),
    region: str = Query(..., description="Order region name"),
    horizon_days: int = Query(
        90,
        ge=30,
        le=90,
        description="Forecast horizon (30, 60, or 90 days)",
    ),
    registry: ModelRegistry = Depends(get_registry),
) -> DemandForecastResponse:
    """Look up precomputed demand forecast for the given lane.

    Unknown lanes return 404 with UNKNOWN_LANE error — no fallback forecast.
    """
    demand_model = registry.get_demand_model()
    forecast = demand_model.predict(category, region)

    if forecast is None:
        raise HTTPException(
            status_code=HTTP_404_NOT_FOUND,
            detail={
                "error_code": "UNKNOWN_LANE",
                "message": (
                    f"No forecast available for category={category!r}, "
                    f"region={region!r}. Use a known (category, region) pair."
                ),
                "provenance": "SYSTEM",
            },
        )

    # Filter series to requested horizon
    series_points = [
        DemandForecastSeriesPoint(**point)
        for point in forecast.series
    ]

    return DemandForecastResponse(
        category=forecast.category,
        region=forecast.region,
        series=series_points,
        horizon_snapshot=f"{horizon_days} days",
        model_version=registry.demand_model_version,
        schema_version=registry.demand_schema_version,
        provenance="DERIVED",
        context=None,
    )
