"""
ML constants for the NexaFreight delay classifier.

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

from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

DB_PATH = _PROJECT_ROOT / "data" / "nexafreight.db"

DATACO_CSV_PATH = (
    _PROJECT_ROOT
    / "data"
    / "raw"
    / "dataco"
    / "DataCoSupplyChainDataset.csv"
)


# ---------------------------------------------------------------------------
# Label and time axis
# ---------------------------------------------------------------------------
LABEL_COLUMN = "historical_late_delivery"
TIME_AXIS_COLUMN = "sla_deadline"


# ---------------------------------------------------------------------------
# Time-based split dates
# ---------------------------------------------------------------------------
SPLIT_DATES = {
    "train_end": "2017-04-01",
    "val_end": "2017-10-01",
}


# ---------------------------------------------------------------------------
# Feature contract
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

    # --- Derived from historical sla_deadline -----------------------------
    "sla_month",
    "sla_weekday",
    "sla_quarter",

    # --- Derived from route-leg aggregation -------------------------------
    # Includes zero-distance port dwell legs correctly: they affect leg_count
    # but not total route distance.
    "total_distance_km",
    "leg_count",
]


# ---------------------------------------------------------------------------
# Categorical features
#
# Every field here MUST also appear in FEATURE_COLUMNS.
# features.py casts these to object/string dtype.
# ---------------------------------------------------------------------------
CATEGORICAL_COLUMNS = [
    "shipping_mode",
    "cargo_class",
    "order_country",
    "customer_country",
]


# Every active non-categorical feature is numeric.
NUMERIC_COLUMNS = [
    column
    for column in FEATURE_COLUMNS
    if column not in CATEGORICAL_COLUMNS
]


# ---------------------------------------------------------------------------
# Excluded features — documented deliberately, not silently discarded
# ---------------------------------------------------------------------------
EXCLUDED_V1_FEATURES = {
    "container_count": (
        "Constant value of 1 across current training rows. "
        "Likely set as a consolidation default; provides no model signal."
    ),
    "primary_transport_mode": (
        "Perfect one-to-one duplicate of shipping_mode. "
        "Verified by crosstab: AIR->AIR, RAIL->RAIL, SEA->SEA only."
    ),
    "has_air_leg": (
        "Deterministic one-hot representation of shipping_mode in current "
        "route templates. Zero additional variance."
    ),
    "has_sea_leg": (
        "Deterministic one-hot representation of shipping_mode in current "
        "route templates. Zero additional variance."
    ),
    "has_rail_leg": (
        "Deterministic one-hot representation of shipping_mode in current "
        "route templates. Zero additional variance."
    ),
    "origin_congestion": (
        "Constant calibrated value of 1.0. IMF port activity data is 2019-2024 "
        "while DataCo training history is 2015-2018; no valid time overlap. "
        "Shipment endpoints also do not directly match IMF port locations."
    ),
    "dest_congestion": (
        "Constant calibrated value of 1.0. Same limitation as origin_congestion."
    ),
}


# ---------------------------------------------------------------------------
# Banned columns — must never enter X
# ---------------------------------------------------------------------------
BANNED_COLUMNS = {
    # --- Post-prediction / operational timeline leakage -------------------
    "planned_departure": (
        "LEAKY — simulated 2026 operational schedule, not historical DataCo time."
    ),
    "planned_arrival": (
        "LEAKY — simulated 2026 operational schedule, not historical DataCo time."
    ),
    "actual_departure": (
        "LEAKY — known only after departure; currently null for planned shipments."
    ),
    "actual_arrival": (
        "LEAKY — known only after arrival; currently null for planned shipments."
    ),
    "created_at": (
        "LEAKY — database ingestion timestamp, not a historical business timestamp."
    ),
    "updated_at": (
        "LEAKY — later operational/database update timestamp."
    ),

    # --- Outcome / target leakage -----------------------------------------
    "days_for_shipping_real": (
        "LEAKY — actual shipping duration; valid future ETA target, not delay feature."
    ),
    "late_delivery_risk": (
        "LEAKY — raw DataCo version of the same delay outcome."
    ),
    "delivery_status": (
        "LEAKY — delivery outcome/status known after shipment progression."
    ),
    "shipping_date_actual": (
        "LEAKY — actual event timestamp."
    ),
    "sla_status": (
        "LABEL PROXY — operational outcome status, not valid at prediction time."
    ),

    # --- IDs / structural fields ------------------------------------------
    "id": "IDENTIFIER — primary key, no generalizable predictive meaning.",
    "order_number": "IDENTIFIER — surrogate business key.",
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

    # --- Target and time axis ---------------------------------------------
    LABEL_COLUMN: "LABEL — prediction target; must never be included in X.",
    TIME_AXIS_COLUMN: (
        "TIME AXIS — used for chronological splitting and derived calendar features only."
    ),
}
