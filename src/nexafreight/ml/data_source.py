"""
data_source.py  — Raw data loading layer for the NexaFreight ML pipeline.

Public API
----------
    load_orders_db()  -> pd.DataFrame   (SQLite orders + joined shipments + legs)
    load_dataco_csv() -> pd.DataFrame   (raw DataCo CSV, deduplicated to order level)
    load_raw()        -> pd.DataFrame   (DB left-join CSV; the single canonical input
                                         for all downstream feature code)

Design note
-----------
The CSV join is isolated here so that when a richer orders schema is available
(with the 53 DataCo fields baked in), only this file needs to change — zero
downstream feature code is affected.

Column name mapping (CSV -> internal)
--------------------------------------
    "Days for shipment (scheduled)"  ->  scheduled_shipping_days
    "Order Country"                  ->  order_country
    "Customer Country"               ->  customer_country
    "Order Item Product Price"       ->  product_price
    "Order Profit Per Order"         ->  order_profit
"""

import logging
from pathlib import Path

import pandas as pd
import sqlalchemy

from nexafreight.ml.constants import DB_PATH, DATACO_CSV_PATH, LABEL_COLUMN

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CSV column mapping  (expected description -> actual CSV header)
# Printed as a mapping table by load_dataco_csv() so the caller always knows
# what was resolved.
# ---------------------------------------------------------------------------
_CSV_COLUMN_MAP: dict[str, str] = {
    "scheduled_shipping_days": "Days for shipment (scheduled)",
    "order_country":           "Order Country",
    "customer_country":        "Customer Country",
    "product_price":           "Order Item Product Price",
    "order_profit":            "Order Profit Per Order",
}

_CSV_DTYPE_MAP: dict[str, type] = {
    "scheduled_shipping_days": float,
    "order_country":           str,
    "customer_country":        str,
    "product_price":           float,
    "order_profit":            float,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _get_engine(db_path: Path | str = DB_PATH) -> sqlalchemy.engine.Engine:
    """Return a read-only SQLAlchemy engine for the SQLite DB."""
    uri = f"sqlite:///{Path(db_path).as_posix()}?mode=ro"
    return sqlalchemy.create_engine(uri, connect_args={"uri": True})


def _load_shipment_aggregates(engine: sqlalchemy.engine.Engine) -> pd.DataFrame:
    """
    Return one row per shipment_id with:
      - container_count, primary_transport_mode (from shipments)
      - total_distance_km, leg_count, has_air_leg, has_sea_leg, has_rail_leg
        (aggregated from legs)
    """
    sql = """
    SELECT
        s.id                              AS shipment_id,
        s.container_count,
        s.primary_transport_mode,
        -- Leg aggregates (SEA zero-distance dwell Points are intentionally included in SUM)
        COALESCE(la.leg_count, 0)         AS leg_count,
        COALESCE(la.total_distance_km, 0) AS total_distance_km,
        COALESCE(la.has_air_leg,  0)      AS has_air_leg,
        COALESCE(la.has_sea_leg,  0)      AS has_sea_leg,
        COALESCE(la.has_rail_leg, 0)      AS has_rail_leg
    FROM shipments s
    LEFT JOIN (
        SELECT
            shipment_id,
            COUNT(*)                                                     AS leg_count,
            SUM(distance_km)                                             AS total_distance_km,
            MAX(CASE WHEN transport_mode = 'AIR'  THEN 1 ELSE 0 END)    AS has_air_leg,
            MAX(CASE WHEN transport_mode = 'SEA'  THEN 1 ELSE 0 END)    AS has_sea_leg,
            MAX(CASE WHEN transport_mode = 'RAIL' THEN 1 ELSE 0 END)    AS has_rail_leg
        FROM legs
        GROUP BY shipment_id
    ) la ON s.id = la.shipment_id
    """
    df = pd.read_sql(sql, engine)
    logger.debug("Loaded shipment aggregates: %d rows", len(df))
    return df


# ---------------------------------------------------------------------------
# Public loaders
# ---------------------------------------------------------------------------
def load_orders_db(db_path: Path | str = DB_PATH) -> pd.DataFrame:
    """
    Load orders from SQLite, joined with shipment-level and leg-level
    aggregates.  Returns one row per order.

    Columns included
    ----------------
    From orders:  order_number, shipment_id, sla_deadline, revenue,
                  shipping_cost, sla_status, shipping_mode, cargo_class,
                  historical_late_delivery, created_at, updated_at
    From shipments (via shipment_id):
                  container_count, primary_transport_mode
    Leg aggregates (via shipment_id):
                  total_distance_km, leg_count, has_air_leg, has_sea_leg,
                  has_rail_leg
    """
    engine = _get_engine(db_path)
    orders = pd.read_sql(
        "SELECT * FROM orders",
        engine,
        parse_dates=["sla_deadline", "created_at", "updated_at"],
    )
    ship_agg = _load_shipment_aggregates(engine)

    df = orders.merge(ship_agg, on="shipment_id", how="left")
    logger.info("load_orders_db: %d orders, %d with shipment data",
                len(df), df["container_count"].notna().sum())
    return df


def load_dataco_csv(csv_path: Path | str = DATACO_CSV_PATH) -> pd.DataFrame:
    """
    Load the DataCo CSV, deduplicate to one row per Order Id, and return
    a DataFrame with the join key 'order_number' and the renamed ML columns.

    Prints a mapping table of expected column names -> actual CSV headers.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"DataCo CSV not found at {csv_path}")

    raw = pd.read_csv(csv_path, encoding="latin-1", low_memory=False)

    # Print mapping table
    print("\n[data_source] CSV column mapping  (expected -> actual CSV header):")
    print(f"  {'Internal name':<30} {'Actual CSV column'}")
    print(f"  {'-'*30} {'-'*35}")
    missing = []
    for internal, csv_col in _CSV_COLUMN_MAP.items():
        found = csv_col in raw.columns
        status = "OK" if found else "MISSING"
        print(f"  {internal:<30} {csv_col!r:<35}  {status}")
        if not found:
            missing.append(internal)
    print()

    if missing:
        logger.warning(
            "CSV columns not found (will be omitted): %s", missing
        )

    # Build join key
    raw["order_number"] = "ORD-" + raw["Order Id"].astype(str)

    # Deduplicate to one row per order  (CSV has one row per order-item)
    # Take first occurrence — all order-level fields repeat identically
    dedup = raw.drop_duplicates(subset=["order_number"], keep="first").copy()

    # Rename and cast
    keep = {"order_number": "order_number"}
    for internal, csv_col in _CSV_COLUMN_MAP.items():
        if csv_col in dedup.columns:
            keep[csv_col] = internal

    dedup = dedup[list(keep.keys())].rename(columns={v: k for k, v in keep.items()
                                                       if v != k and k != "order_number"})
    # Rename internal->internal for order_number is a no-op; fix the rename map
    rename_map = {csv_col: internal
                  for internal, csv_col in _CSV_COLUMN_MAP.items()
                  if csv_col in raw.columns}
    dedup = (
        raw.drop_duplicates(subset=["order_number"], keep="first")
           .rename(columns=rename_map)
    )
    dedup["order_number"] = "ORD-" + dedup["Order Id"].astype(str)

    csv_cols = list(rename_map.values())          # internal names that were found
    keep_cols = ["order_number"] + [c for c in csv_cols if c in dedup.columns]
    dedup = dedup[keep_cols].copy()

    # Cast dtypes — only coerce numeric cols; leave string cols as-is
    for col, dtype in _CSV_DTYPE_MAP.items():
        if col in dedup.columns:
            if dtype == float:
                dedup[col] = pd.to_numeric(dedup[col], errors="coerce")
            else:
                # String column: ensure object dtype, no numeric coercion
                dedup[col] = dedup[col].astype(str).where(dedup[col].notna(), other=None)

    logger.info("load_dataco_csv: %d unique orders from CSV", len(dedup))
    return dedup


def load_raw(
    db_path: Path | str = DB_PATH,
    csv_path: Path | str = DATACO_CSV_PATH,
) -> pd.DataFrame:
    """
    Return the canonical joined DataFrame: DB orders left-join DataCo CSV.

    This is the single entry point for all feature-engineering code.
    To replace the CSV join with a richer orders schema, change only this
    function — downstream code is unaffected.

    Logs join coverage % and warns if < 99 %.
    """
    db_df = load_orders_db(db_path)
    csv_df = load_dataco_csv(csv_path)

    before = len(db_df)
    merged = db_df.merge(csv_df, on="order_number", how="left")

    # Measure coverage using a CSV-only column
    csv_only_col = next(iter(_CSV_COLUMN_MAP.keys()))  # e.g. "scheduled_shipping_days"
    if csv_only_col in merged.columns:
        matched = merged[csv_only_col].notna().sum()
        coverage_pct = matched / before * 100
        msg = f"load_raw: CSV join coverage {matched}/{before} = {coverage_pct:.2f}%"
        if coverage_pct < 99.0:
            logger.warning(msg + "  [WARN: below 99% threshold]")
        else:
            logger.info(msg)

    assert len(merged) == before, (
        f"Join produced {len(merged)} rows vs {before} input orders — "
        "check for duplicate order_numbers in the CSV"
    )
    logger.info("load_raw: final joined DataFrame has %d rows, %d columns",
                len(merged), merged.shape[1])
    return merged
