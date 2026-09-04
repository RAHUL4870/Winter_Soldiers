"""
ML constants for the NexaFreight delay classifier (T-035/T-036),
quantile ETA regressor (T-037), and demand forecast model (T-038).

SPLIT DATE DERIVATION
---------------------
sla_deadline spans 2015-01-02 to 2018-02-04. The empirical split below gives
approximately 71 / 16 / 13 percent train / validation / test rows while
preserving the historical late-delivery positive rate (~54.9%) in every split.

    TRAIN_END = 2017-04-01
    VAL_END   = 2017-10-01

Important architecture:
- Historical DataCo timeline (2015-2018) is used for model training.
- Operational planned leg dates are anchored to the 2026 demo/live-map timeline.
  They must never be used as ML dates or feature inputs.
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths — restored exactly from original working version
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

DATA_DIR = _PROJECT_ROOT / "data"
PROCESSED_DIR = DATA_DIR / "processed"
MODELS_DIR = _PROJECT_ROOT / "models"

DB_PATH = _PROJECT_ROOT / "data" / "nexafreight.db"

DATACO_CSV_PATH = (
    _PROJECT_ROOT
    / "data"
    / "raw"
    / "dataco"
    / "DataCoSupplyChainDataset.csv"
)

# Model artifact directories
DELAY_MODEL_DIR = str(MODELS_DIR / "delay_classifier")
ETA_MODEL_DIR = str(MODELS_DIR / "eta_quantile")
DEMAND_MODEL_DIR = str(MODELS_DIR / "demand_forecast")


# ---------------------------------------------------------------------------
# Label and time axis — restored exactly from original
# ---------------------------------------------------------------------------
LABEL_COLUMN = "historical_late_delivery"
TIME_AXIS_COLUMN = "sla_deadline"


# ---------------------------------------------------------------------------
# Time-based split dates — restored exactly from original
# ---------------------------------------------------------------------------
SPLIT_DATES = {
    "train_end": "2017-04-01",
    "val_end": "2017-10-01",
}


# ---------------------------------------------------------------------------
# Feature contract — restored from original (list, not tuple)
#
# This is the AUTHORITATIVE ordered list.
# Training, model registry, inference, and tests must all respect this order.
#
# v1 active features: 14
# ---------------------------------------------------------------------------
FEATURE_COLUMNS = [
    # --- Operational DB: orders -------------------------------------------
    "shipping_mode",            # AIR / SEA / RAIL
    "cargo_class",
    "revenue",
    "shipping_cost",

    # --- Raw historical DataCo CSV ----------------------------------------
    "scheduled_shipping_days",  # Days for shipment (scheduled)
    "order_country",
    "customer_country",
    "product_price",
    "order_profit",

    # --- Derived calendar features from sla_deadline -----------------------
    "sla_month",
    "sla_weekday",
    "sla_quarter",

    # --- Leg aggregates ----------------------------------------------------
    "total_distance_km",
    "leg_count",
]

# Categorical subset of FEATURE_COLUMNS
CATEGORICAL_COLUMNS = [
    "shipping_mode",
    "cargo_class",
    "order_country",
    "customer_country",
]

# Numeric features (everything not categorical)
NUMERIC_COLUMNS = [
    col for col in FEATURE_COLUMNS if col not in CATEGORICAL_COLUMNS
]


# ---------------------------------------------------------------------------
# Banned columns — restored exactly from original (dict form)
# ---------------------------------------------------------------------------
BANNED_COLUMNS = {
    # --- Target leakages --------------------------------------------------
    "days_for_shipping_real": "ACTUAL transit days — leakage: outcome known only post-hoc.",
    "late_delivery_risk": "TARGET — the exact binary label used in v0 baseline.",
    "historical_late_delivery": "LABEL — prediction target; must never be included in X.",

    # --- Time-travel (future information) ---------------------------------
    "created_at": "TIMESTAMP — ordering/censoring artifact, not a feature.",
    "planned_departure": "TIMESTAMP — less granular than sla_deadline; redundant.",
    "planned_arrival": "TIMESTAMP — post-hoc; only computed after sla_deadline.",
    "actual_departure": "TIMESTAMP — post-hoc; unknown at prediction time.",
    "actual_arrival": "TIMESTAMP — post-hoc; unknown at prediction time.",
    "sla_deadline": (
        "TIME AXIS — used for chronological splitting and derived"
        " calendar features only."
    ),

    # --- Identifiers -------------------------------------------------------
    "order_id": "IDENTIFIER — row-level key; adds zero predictive value.",
    "shipment_id": "IDENTIFIER — foreign key.",
    "origin_id": "IDENTIFIER — use engineered country/location features instead.",
    "destination_id": "IDENTIFIER — use engineered country/location features instead.",

    # --- Operational routing metadata -------------------------------------
    "route_version": (
        "OPERATIONAL META — may encode rerouting/history not known at booking time."
    ),
    "status": (
        "OPERATIONAL META — currently planned state; not a historical predictive input."
    ),
}


# ---------------------------------------------------------------------------
# ETA-specific constants (T-037 additions — do NOT touch existing values above)
# ---------------------------------------------------------------------------

# The raw columns required from load_raw() — these are NOT used as features;
# they only exist so the training script can construct the target.
ETA_ACTUAL_DAYS_COLUMN: str = "days_for_shipping_real"
ETA_SCHEDULED_DAYS_COLUMN: str = "scheduled_shipping_days"

# The target residual computed at training time
ETA_TARGET_COLUMN: str = "transit_delay_residual"

# Quantile configuration — must match the exported model keys
ETA_QUANTILES: list[float] = [0.10, 0.50, 0.85]
QUANTILE_KEYS: tuple[str, str, str] = ("p10", "p50", "p85")

# SLA risk bands (used by test assertions and inference)
SLA_RISK_BANDS: tuple[str, str, str, str] = ("ON_TIME", "MEDIUM", "HIGH", "BREACH")

# Sentinel value for unseen categorical levels
MISSING_SENTINEL: str = "__MISSING__"

# Minimum transit days (prevents negative/zero ETA predictions)
MIN_TRANSIT_DAYS: float = 0.5


# ---------------------------------------------------------------------------
# Demand forecasting (T-038)
# ---------------------------------------------------------------------------

# The weekly count column produced by the aggregation step
DEMAND_TARGET_COLUMN: str = "order_count"

# Grouping dimensions used to define a "lane"
# (product_category × origin_region)
# dest_region is folded into origin_region via Order Region in DataCo
DEMAND_GROUP_COLS: tuple[str, str] = ("category_name", "order_region")

# StatsForecast unique_id column (lane identifier string)
DEMAND_UNIQUE_ID_COL: str = "unique_id"

# Forecast horizons in days (30 / 60 / 90)
DEMAND_FORECAST_HORIZONS: tuple[int, int, int] = (30, 60, 90)

# Maximum forecast horizon in weeks (must match longest horizon above)
DEMAND_FORECAST_HORIZON_WEEKS: int = 13   # 90 days ≈ 13 weeks

# Holdout weeks for MAPE evaluation
DEMAND_HOLDOUT_WEEKS: int = 13

# Minimum number of observations (weeks) required to fit a lane series
DEMAND_MIN_SERIES_LEN: int = 26

# Confidence level for prediction intervals (used by AutoETS)
DEMAND_PREDICTION_LEVEL: int = 80  # 80% CI → lower/upper bands

# Frontend chart-ready keys produced by the inference wrapper
DEMAND_CHART_KEYS: tuple[str, ...] = (
    "ds",          # ISO date string (week start)
    "yhat",        # point forecast
    "yhat_lower",  # CI lower bound
    "yhat_upper",  # CI upper bound
    "is_forecast", # True for horizon rows, False for history
)
