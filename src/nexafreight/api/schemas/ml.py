"""
Pydantic request/response schemas for ML prediction endpoints (T-039).

Contract non-negotiables:
- Every response carries model_version, schema_version, provenance.
- Raw feature vectors NEVER appear in responses.
- ``context`` field reserved for T-049D OSINT forward-compat (always None now).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------
class MLErrorResponse(BaseModel):
    """Standard error envelope for ML endpoints."""

    error_code: str
    message: str
    provenance: str = "SYSTEM"


class _MLResponseBase(BaseModel):
    """Common fields on every ML prediction response."""

    model_config = {"protected_namespaces": ()}

    model_version: str
    schema_version: str
    provenance: str = "DERIVED"
    context: dict[str, Any] | None = Field(
        default=None,
        description="Reserved for T-049D OSINT disruption-flag payloads.",
    )


# ---------------------------------------------------------------------------
# Delay Classifier
# ---------------------------------------------------------------------------
class DelayPredictionRequest(BaseModel):
    """Input features for the delay classifier (14 features)."""

    shipping_mode: str = Field(..., description="AIR | SEA | RAIL")
    cargo_class: str = Field(..., description="STANDARD | HIGH_VALUE | REFRIGERATED")
    revenue: float
    shipping_cost: float
    scheduled_shipping_days: float
    order_country: str
    customer_country: str
    product_price: float
    order_profit: float
    sla_month: int = Field(..., ge=1, le=12)
    sla_weekday: int = Field(..., ge=0, le=6)
    sla_quarter: int = Field(..., ge=1, le=4)
    total_distance_km: float = Field(..., ge=0)
    leg_count: int = Field(..., ge=1)


class DelayPredictionResponse(_MLResponseBase):
    """Delay classifier prediction result."""

    probability: float = Field(..., ge=0.0, le=1.0, description="Delay probability [0, 1]")
    risk_band: str = Field(..., description="LOW (<0.3) | MEDIUM (<0.6) | HIGH (≥0.6)")


# ---------------------------------------------------------------------------
# ETA Quantile Regressor
# ---------------------------------------------------------------------------
class EtaPredictionRequest(BaseModel):
    """Input features for the ETA quantile model (same 14 features)."""

    shipping_mode: str
    cargo_class: str
    revenue: float
    shipping_cost: float
    scheduled_shipping_days: float
    order_country: str
    customer_country: str
    product_price: float
    order_profit: float
    sla_month: int = Field(..., ge=1, le=12)
    sla_weekday: int = Field(..., ge=0, le=6)
    sla_quarter: int = Field(..., ge=1, le=4)
    total_distance_km: float = Field(..., ge=0)
    leg_count: int = Field(..., ge=1)


class EtaPredictionResponse(_MLResponseBase):
    """ETA quantile prediction result (P10/P50/P85 in days)."""

    p10_eta_days: float = Field(..., description="Optimistic ETA (days)")
    p50_eta_days: float = Field(..., description="Median ETA (days)")
    p85_eta_days: float = Field(..., description="Conservative ETA (days)")
    confidence_interval_width: float = Field(..., ge=0.0, description="P85 − P10 spread (days)")


# ---------------------------------------------------------------------------
# Demand Forecast
# ---------------------------------------------------------------------------
class DemandForecastSeriesPoint(BaseModel):
    """One point in a demand forecast time series."""

    ds: str = Field(..., description="Date string (ISO)")
    yhat: float = Field(..., description="Point forecast")
    yhat_lower: float | None = Field(None, description="Lower bound")
    yhat_upper: float | None = Field(None, description="Upper bound")
    is_forecast: bool = Field(..., description="True if projected, False if historical")


class DemandForecastResponse(_MLResponseBase):
    """Demand forecast for a single (category × region) lane."""

    category: str
    region: str
    series: list[DemandForecastSeriesPoint]
    horizon_snapshot: str = Field(..., description="Horizon description, e.g. '30 days'")


# ---------------------------------------------------------------------------
# Risk band helper
# ---------------------------------------------------------------------------
def classify_risk_band(probability: float) -> str:
    """Map delay probability to risk band string."""
    if probability < 0.3:
        return "LOW"
    if probability < 0.6:
        return "MEDIUM"
    return "HIGH"
