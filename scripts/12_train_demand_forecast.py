#!/usr/bin/env python
"""
12_train_demand_forecast.py — Train StatsForecast AutoETS demand model (Phase 3, T-038).

Pipeline
--------
  1. Load raw DataCo CSV directly (no DB join needed — purely order-level history).
  2. Aggregate weekly order counts per (category_name × order_region) lane.
  3. Discard lanes with fewer than DEMAND_MIN_SERIES_LEN weeks of history.
  4. Hold out the last DEMAND_HOLDOUT_WEEKS for MAPE evaluation.
  5. Fit one StatsForecast AutoETS model per lane on the training portion.
  6. Evaluate: per-lane MAPE on the holdout; report weighted mean MAPE.
  7. Generate 30/60/90-day point forecasts + 80% prediction intervals
     by extending each fitted model DEMAND_FORECAST_HORIZON_WEEKS further.
  8. Save artifacts:
       models/demand_forecast/model.joblib        ← {models, lane_index, history}
       models/demand_forecast/forecasts.json      ← chart-ready JSON (all lanes)
       models/demand_forecast/metadata.json       ← provenance + metrics

Usage
-----
  python scripts/12_train_demand_forecast.py
  python scripts/12_train_demand_forecast.py --min-series 26 --holdout-weeks 13
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# ---------------------------------------------------------------------------
# Path setup — make ``src/`` importable when running from project root
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from nexafreight.ml.constants import (  # noqa: E402
    DATACO_CSV_PATH,
    DEMAND_FORECAST_HORIZON_WEEKS,
    DEMAND_FORECAST_HORIZONS,
    DEMAND_HOLDOUT_WEEKS,
    DEMAND_MIN_SERIES_LEN,
    DEMAND_MODEL_DIR,
    DEMAND_PREDICTION_LEVEL,
    DEMAND_TARGET_COLUMN,
    DEMAND_UNIQUE_ID_COL,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("train_demand")

# ---------------------------------------------------------------------------
# Script-level constants
# ---------------------------------------------------------------------------
RANDOM_SEED = 42
MODEL_VERSION = "1.0.0"

# CSV column names (raw DataCo)
_CSV_ORDER_ID = "Order Id"
_CSV_ORDER_DATE = "order date (DateOrders)"
_CSV_CATEGORY = "Category Name"
_CSV_REGION = "Order Region"


# ============================================================================
# Helpers
# ============================================================================
def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


def _file_hash(path: Path) -> str:
    if not path.exists():
        return "none"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_joblib(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        joblib.dump(obj, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _mape(actual: np.ndarray, predicted: np.ndarray) -> float | None:
    """Mean Absolute Percentage Error, ignoring zero-actual rows."""
    mask = actual != 0
    if mask.sum() == 0:
        return None
    return float(np.mean(np.abs((actual[mask] - predicted[mask]) / actual[mask])) * 100)


def _make_lane_id(category: str, region: str) -> str:
    """Stable URL-safe lane identifier, e.g. 'Cleats__Western Europe'."""
    return f"{category}__{region}"


# ============================================================================
# Step 1 — Load & aggregate
# ============================================================================
def load_and_aggregate(csv_path: Path) -> tuple[pd.DataFrame, str]:
    """
    Load the DataCo CSV, deduplicate to order-level, aggregate weekly order
    counts per (category_name × order_region) lane.

    Returns
    -------
    panel : pd.DataFrame
        Columns: unique_id, ds (period W), order_count
    csv_hash : str
        SHA-256 prefix of the source CSV (16 chars).
    """
    log.info("Loading DataCo CSV from %s ...", csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"DataCo CSV not found: {csv_path}")

    raw = pd.read_csv(csv_path, encoding="latin-1", low_memory=False)
    csv_hash = _file_hash(csv_path)

    # Deduplicate to one row per order (CSV has one row per order-item)
    orders = raw.drop_duplicates(subset=[_CSV_ORDER_ID], keep="first").copy()
    log.info("  %d item rows → %d unique orders after dedup", len(raw), len(orders))

    # Parse order date
    orders["_order_dt"] = pd.to_datetime(
        orders[_CSV_ORDER_DATE], format="mixed", errors="coerce"
    )
    bad_dates = orders["_order_dt"].isna().sum()
    if bad_dates:
        log.warning("  Dropping %d orders with unparseable dates", bad_dates)
    orders = orders[orders["_order_dt"].notna()].copy()

    # Derive week (Monday-anchored period)
    orders["_week"] = orders["_order_dt"].dt.to_period("W")

    # Rename grouping columns to internal names
    orders["category_name"] = orders[_CSV_CATEGORY].astype(str).str.strip()
    orders["order_region"] = orders[_CSV_REGION].astype(str).str.strip()

    # Aggregate: weekly order count per lane
    agg = (
        orders
        .groupby(["category_name", "order_region", "_week"])
        .size()
        .reset_index(name=DEMAND_TARGET_COLUMN)
    )

    # Build unique_id
    agg[DEMAND_UNIQUE_ID_COL] = agg.apply(
        lambda r: _make_lane_id(r["category_name"], r["order_region"]), axis=1
    )

    # Convert period to timestamp (start of week) for StatsForecast compatibility
    agg["ds"] = agg["_week"].dt.to_timestamp(how="start")

    panel = agg[
        [DEMAND_UNIQUE_ID_COL, "ds", DEMAND_TARGET_COLUMN, "category_name", "order_region"]
    ].copy()
    panel = panel.sort_values([DEMAND_UNIQUE_ID_COL, "ds"]).reset_index(drop=True)

    log.info(
        "  Aggregated to %d lane-week rows across %d unique lanes",
        len(panel),
        panel[DEMAND_UNIQUE_ID_COL].nunique(),
    )
    return panel, csv_hash


# ============================================================================
# Step 2 — Filter & split
# ============================================================================
def filter_and_split(
    panel: pd.DataFrame,
    min_series_len: int,
    holdout_weeks: int,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """
    Discard sparse lanes, then time-split into train and holdout.

    Returns
    -------
    train : pd.DataFrame
    holdout : pd.DataFrame
    qualified_ids : list of unique_ids that passed the minimum-length filter
    """
    # Count observations per lane
    series_lens = panel.groupby(DEMAND_UNIQUE_ID_COL)["ds"].count()
    qualified = series_lens[series_lens >= min_series_len].index.tolist()
    dropped = int(series_lens.shape[0]) - len(qualified)
    log.info(
        "  Qualified lanes (>= %d weeks): %d  (dropped %d sparse lanes)",
        min_series_len, len(qualified), dropped,
    )

    panel_q = panel[panel[DEMAND_UNIQUE_ID_COL].isin(qualified)].copy()

    # Time-based split: holdout = last <holdout_weeks> per lane
    def _split_lane(group: pd.DataFrame) -> pd.DataFrame:
        group = group.sort_values("ds")
        group["_split"] = "train"
        group.iloc[-holdout_weeks:, group.columns.get_loc("_split")] = "holdout"
        return group

    panel_q = panel_q.groupby(DEMAND_UNIQUE_ID_COL, group_keys=False).apply(_split_lane)

    train = panel_q[panel_q["_split"] == "train"].drop(columns="_split").reset_index(drop=True)
    holdout = panel_q[panel_q["_split"] == "holdout"].drop(columns="_split").reset_index(drop=True)

    log.info(
        "  Train: %d lane-week rows | Holdout: %d lane-week rows",
        len(train), len(holdout),
    )
    return train, holdout, qualified


# ============================================================================
# Step 3 — Fit AutoETS
# ============================================================================
def fit_autoets(
    train: pd.DataFrame,
    horizon_weeks: int,
    prediction_level: int,
) -> Any:
    """
    Fit one AutoETS model per lane using StatsForecast.

    StatsForecast expects a DataFrame with columns:
        unique_id, ds (datetime64), y (numeric target)
    """
    from statsforecast import StatsForecast
    from statsforecast.models import AutoETS

    sf_train = train.rename(columns={DEMAND_TARGET_COLUMN: "y"})[
        [DEMAND_UNIQUE_ID_COL, "ds", "y"]
    ].copy()

    # Ensure ds is datetime64
    sf_train["ds"] = pd.to_datetime(sf_train["ds"])
    sf_train["y"] = sf_train["y"].astype(float)

    log.info(
        "  Fitting AutoETS on %d lanes × up to %d weeks each ...",
        sf_train[DEMAND_UNIQUE_ID_COL].nunique(),
        sf_train.groupby(DEMAND_UNIQUE_ID_COL)["ds"].count().max(),
    )

    sf = StatsForecast(
        models=[AutoETS(season_length=4)],  # ~monthly seasonality on weekly data (4 weeks)
        freq="W",
        n_jobs=1,  # single-threaded: multiprocessing hides per-lane errors
    )
    sf.fit(sf_train)

    log.info("  AutoETS fitting complete.")
    return sf


# ============================================================================
# Step 4 — Evaluate on holdout
# ============================================================================
def _seasonal_naive_forecast(
    train: pd.DataFrame,
    holdout: pd.DataFrame,
    season_length: int = 4,
) -> dict[str, np.ndarray]:
    """
    Seasonal naive baseline: repeat the last `season_length` weeks of training
    data cyclically into the holdout horizon.  Returns {unique_id: predictions}.
    """
    baseline_preds: dict[str, np.ndarray] = {}
    for uid, h_group in holdout.groupby(DEMAND_UNIQUE_ID_COL):
        t_group = train[train[DEMAND_UNIQUE_ID_COL] == uid].sort_values("ds")
        history = t_group[DEMAND_TARGET_COLUMN].to_numpy(dtype=float)
        n_holdout = len(h_group)
        if len(history) < season_length:
            # Fall back to global mean if too little history
            baseline_preds[str(uid)] = np.full(n_holdout, history.mean() if len(history) > 0 else 0.0)
        else:
            cycle = history[-season_length:]
            # Tile cyclically to fill holdout horizon
            reps = (n_holdout // season_length) + 1
            baseline_preds[str(uid)] = np.tile(cycle, reps)[:n_holdout]
    return baseline_preds


def evaluate_holdout(
    sf: Any,
    train: pd.DataFrame,
    holdout: pd.DataFrame,
    horizon_weeks: int,
    prediction_level: int,
) -> tuple[dict[str, float], float, float, float]:
    """
    Generate point forecasts over the holdout horizon and compute MAPE per lane.
    Also computes a seasonal naive baseline WAPE for lift comparison.

    Returns
    -------
    per_lane_mape : {unique_id: mape_pct}
    wape          : overall WAPE (sum|a-p| / sum(a) × 100)
    baseline_wape : seasonal naive baseline WAPE
    lift_pct      : (1 - wape/baseline_wape) × 100
    """
    # Point-only predict — no level= to avoid statsforecast class-3 ETS bug.
    preds = sf.predict(h=horizon_weeks).reset_index()
    point_col = "AutoETS"

    # Seasonal naive baseline
    sn_preds = _seasonal_naive_forecast(train, holdout)

    per_lane_mape: dict[str, float] = {}
    weighted_actuals = 0.0
    weighted_errors = 0.0
    bl_weighted_errors = 0.0

    for uid, group in holdout.groupby(DEMAND_UNIQUE_ID_COL):
        pred_group = preds[preds[DEMAND_UNIQUE_ID_COL] == uid].copy()
        actual = group.sort_values("ds")[DEMAND_TARGET_COLUMN].to_numpy(dtype=float)
        n = min(len(actual), len(pred_group))
        if n == 0:
            continue
        predicted = pred_group.sort_values("ds")[point_col].to_numpy(dtype=float)[:n]
        actual = actual[:n]

        mape = _mape(actual, predicted)
        if mape is not None:
            per_lane_mape[str(uid)] = round(mape, 2)
            weighted_actuals += actual.sum()
            weighted_errors += float(np.sum(np.abs(actual - predicted)))

            bl = sn_preds.get(str(uid))
            if bl is not None:
                bl_weighted_errors += float(np.sum(np.abs(actual - bl[:n])))

    wape = (
        (weighted_errors / weighted_actuals * 100) if weighted_actuals > 0 else float("nan")
    )
    baseline_wape = (
        (bl_weighted_errors / weighted_actuals * 100) if weighted_actuals > 0 else float("nan")
    )
    lift_pct = (
        (1.0 - wape / baseline_wape) * 100 if baseline_wape > 0 else float("nan")
    )

    log.info(
        "  Holdout — WAPE: %.1f%%  |  Baseline WAPE: %.1f%%  |  Lift: %+.1f%%  |  Lanes: %d",
        wape, baseline_wape, lift_pct, len(per_lane_mape),
    )
    return per_lane_mape, wape, baseline_wape, lift_pct


# ============================================================================
# Step 5 — Generate full forecasts (train + horizon)
# ============================================================================
def generate_forecasts(
    sf: Any,
    panel: pd.DataFrame,
    qualified_ids: list[str],
    horizon_weeks: int,
    prediction_level: int,
) -> tuple[dict[str, Any], Any]:
    """
    Re-fit on the full series (train + holdout) and produce chart-ready
    30/60/90-day forecasts with 80% prediction intervals.

    Falls back to point-only (lower == upper == yhat) for individual lanes
    whose ETS model type triggers the statsforecast class-3 PI bug.

    Returns
    -------
    forecasts : {unique_id: {"category", "region", "series": [...]}}
    sf_final  : fitted StatsForecast object (persisted in model.joblib)
    """
    from statsforecast import StatsForecast
    from statsforecast.models import AutoETS

    log.info("  Re-fitting on full series for final forecast generation ...")
    panel_q = panel[panel[DEMAND_UNIQUE_ID_COL].isin(qualified_ids)].copy()

    sf_full_train = panel_q.rename(columns={DEMAND_TARGET_COLUMN: "y"})[
        [DEMAND_UNIQUE_ID_COL, "ds", "y"]
    ].copy()
    sf_full_train["ds"] = pd.to_datetime(sf_full_train["ds"])
    sf_full_train["y"] = sf_full_train["y"].astype(float)

    sf_final = StatsForecast(
        models=[AutoETS(season_length=4)],  # ~monthly seasonality (4 weeks)
        freq="W",
        n_jobs=1,  # single-threaded: multiprocessing hides per-lane errors
    )
    sf_final.fit(sf_full_train)

    point_col = "AutoETS"
    lo_col = f"AutoETS-lo-{prediction_level}"
    hi_col = f"AutoETS-hi-{prediction_level}"

    # Try batch predict with PIs; fall back to point-only for the full set
    # if the class-3 ETS bug is triggered (statsforecast 1.7.x known issue).
    # In both cases, reset_index() normalises unique_id from index → column.
    try:
        preds = sf_final.predict(h=horizon_weeks, level=[prediction_level]).reset_index()
        has_pi = lo_col in preds.columns and hi_col in preds.columns
        log.info("  Prediction intervals available: %s", has_pi)
    except (ValueError, Exception) as exc:
        log.warning(
            "  PI batch predict failed (%s) — falling back to point-only. "
            "Confidence bands will be yhat ±15%% heuristic.", exc
        )
        preds = sf_final.predict(h=horizon_weeks).reset_index()
        has_pi = False

    # Build lane metadata lookup
    meta = (
        panel_q[[DEMAND_UNIQUE_ID_COL, "category_name", "order_region"]]
        .drop_duplicates(DEMAND_UNIQUE_ID_COL)
        .set_index(DEMAND_UNIQUE_ID_COL)
    )

    forecasts: dict[str, Any] = {}
    pi_fallback_count = 0
    pi_fallback_lanes: list[str] = []

    for uid in qualified_ids:
        history = panel_q[panel_q[DEMAND_UNIQUE_ID_COL] == uid].sort_values("ds")
        fut = preds[preds[DEMAND_UNIQUE_ID_COL] == uid].sort_values("ds")

        lane_data: list[dict[str, Any]] = []

        # Historical actuals (lower == upper == actual for non-forecast rows)
        for _, row in history.iterrows():
            y = float(row[DEMAND_TARGET_COLUMN])   # panel_q uses original col name
            lane_data.append({
                "ds": pd.Timestamp(row["ds"]).strftime("%Y-%m-%d"),
                "yhat": round(y, 2),
                "yhat_lower": round(y, 2),
                "yhat_upper": round(y, 2),
                "is_forecast": False,
            })

        # Forecast rows — per-lane PI fallback heuristic if batch PIs failed
        lane_used_heuristic = False
        for _, row in fut.iterrows():
            yhat = round(float(row[point_col]), 2)
            if has_pi and lo_col in row.index and hi_col in row.index:
                ylo = round(float(row[lo_col]), 2)
                yhi = round(float(row[hi_col]), 2)
                pi_method = "ets"
            else:
                # Heuristic: ±15% bands when ETS model type doesn't support PIs
                ylo = round(max(0.0, yhat * 0.85), 2)
                yhi = round(yhat * 1.15, 2)
                pi_method = "heuristic_15pct"
                pi_fallback_count += 1
                lane_used_heuristic = True
            lane_data.append({
                "ds": pd.Timestamp(row["ds"]).strftime("%Y-%m-%d"),
                "yhat": yhat,
                "yhat_lower": ylo,
                "yhat_upper": yhi,
                "is_forecast": True,
                "pi_method": pi_method,
            })
        if lane_used_heuristic:
            pi_fallback_lanes.append(str(uid))

        cat = meta.loc[uid, "category_name"] if uid in meta.index else ""
        reg = meta.loc[uid, "order_region"] if uid in meta.index else ""
        forecasts[str(uid)] = {
            "category": cat,
            "region": reg,
            "series": lane_data,
            "pi_method": "heuristic_15pct" if str(uid) in pi_fallback_lanes else "ets",
        }

    if pi_fallback_count:
        log.info(
            "  PI heuristic fallback applied to %d forecast rows across %d lanes "
            "(class-3 ETS model type; ±15%% bands used).",
            pi_fallback_count, len(pi_fallback_lanes),
        )
    log.info("  Generated forecasts for %d lanes.", len(forecasts))
    return forecasts, sf_final, pi_fallback_lanes


# ============================================================================
# Main training routine
# ============================================================================
def train(
    min_series_len: int = DEMAND_MIN_SERIES_LEN,
    holdout_weeks: int = DEMAND_HOLDOUT_WEEKS,
) -> None:
    started = time.time()
    trained_at = datetime.now(UTC).isoformat()
    model_dir = Path(DEMAND_MODEL_DIR)
    model_dir.mkdir(parents=True, exist_ok=True)

    log.info("=" * 64)
    log.info("  NexaFreight Phase 3 — Train AutoETS Demand Model (T-038)")
    log.info("=" * 64)

    # ------------------------------------------------------------------
    # Step 1: Load & aggregate
    # ------------------------------------------------------------------
    log.info("Step 1/5 — Loading & aggregating weekly demand ...")
    panel, csv_hash = load_and_aggregate(Path(DATACO_CSV_PATH))

    total_lanes = panel[DEMAND_UNIQUE_ID_COL].nunique()
    date_min = panel["ds"].min().strftime("%Y-%m-%d")
    date_max = panel["ds"].max().strftime("%Y-%m-%d")
    log.info("  %d total lanes  |  %d weeks of history  (%s .. %s)",
             total_lanes, panel["ds"].nunique(), date_min, date_max)

    # ------------------------------------------------------------------
    # Step 2: Filter & split
    # ------------------------------------------------------------------
    log.info(
        "Step 2/5 — Filtering lanes (min=%d weeks) and splitting holdout (%d weeks) ...",
        min_series_len, holdout_weeks,
    )
    train_df, holdout_df, qualified_ids = filter_and_split(panel, min_series_len, holdout_weeks)

    if not qualified_ids:
        raise ValueError(
            f"No lanes survive the minimum series length filter ({min_series_len} weeks). "
            "Lower --min-series or use more data."
        )

    # ------------------------------------------------------------------
    # Step 3: Fit AutoETS on train split
    # ------------------------------------------------------------------
    log.info(
        "Step 3/5 — Fitting AutoETS on %d qualified lanes (train split) ...",
        len(qualified_ids),
    )
    sf_eval = fit_autoets(train_df, DEMAND_FORECAST_HORIZON_WEEKS, DEMAND_PREDICTION_LEVEL)

    # ------------------------------------------------------------------
    # Step 4: Evaluate holdout MAPE
    # ------------------------------------------------------------------
    log.info("Step 4/5 — Evaluating holdout MAPE (%d-week holdout) ...", holdout_weeks)
    per_lane_mape, wape, baseline_wape, lift_pct = evaluate_holdout(
        sf_eval, train_df, holdout_df, holdout_weeks, DEMAND_PREDICTION_LEVEL
    )

    # ------------------------------------------------------------------
    # Step 5: Generate final forecasts on full data + save artifacts
    # ------------------------------------------------------------------
    log.info("Step 5/5 — Generating final forecasts and saving artifacts ...")
    forecasts, sf_final, pi_fallback_lanes = generate_forecasts(
        sf_eval, panel, qualified_ids,
        DEMAND_FORECAST_HORIZON_WEEKS, DEMAND_PREDICTION_LEVEL,
    )

    # --- model.joblib ---
    lane_index = {
        uid: {
            "category": forecasts[uid]["category"],
            "region": forecasts[uid]["region"],
        }
        for uid in qualified_ids
    }
    artifact = {
        "sf": sf_final,
        "lane_index": lane_index,
        "qualified_ids": qualified_ids,
        "model_version": MODEL_VERSION,
        "trained_at": trained_at,
        "forecast_horizon_weeks": DEMAND_FORECAST_HORIZON_WEEKS,
        "prediction_level": DEMAND_PREDICTION_LEVEL,
    }
    model_path = model_dir / "model.joblib"
    _atomic_joblib(artifact, model_path)
    mhash = _file_hash(model_path)
    log.info("  model.joblib → %s  (sha256=%s)", model_path, mhash)

    # --- forecasts.json ---
    forecasts_path = model_dir / "forecasts.json"
    _atomic_json(forecasts_path, forecasts)
    log.info("  forecasts.json → %s", forecasts_path)

    # --- metadata.json ---
    runtime = round(time.time() - started, 1)
    # Top/bottom 5 lanes by MAPE for summary
    sorted_mapes = sorted(per_lane_mape.items(), key=lambda x: x[1])
    best5 = {k: v for k, v in sorted_mapes[:5]}
    worst5 = {k: v for k, v in sorted_mapes[-5:]}

    metadata = {
        "model_name": "demand_forecast_autoets",
        "model_version": MODEL_VERSION,
        "trained_at": trained_at,
        "git_sha": _git_sha(),
        "source_csv_sha256": csv_hash,
        "model_artifact_sha256": mhash,
        "training_runtime_sec": runtime,
        "data": {
            "total_lanes_in_dataset": total_lanes,
            "qualified_lanes": len(qualified_ids),
            "date_range": {"start": date_min, "end": date_max},
            "train_weeks": int(train_df.groupby(DEMAND_UNIQUE_ID_COL)["ds"].count().median()),
            "holdout_weeks": holdout_weeks,
        },
        "training_parameters": {
            "min_series_len": min_series_len,
            "forecast_horizon_weeks": DEMAND_FORECAST_HORIZON_WEEKS,
            "prediction_level": DEMAND_PREDICTION_LEVEL,
            "autoets_season_length": 4,
            "freq": "W",
        },
        "metrics": {
            "wape_pct": round(wape, 2),
            "baseline_wape_pct": round(baseline_wape, 2),
            "lift_over_baseline_pct": round(lift_pct, 2),
            "baseline_method": "seasonal_naive_4week",
            "median_lane_mape_pct": round(
                float(np.median(list(per_lane_mape.values()))), 2
            ) if per_lane_mape else None,
            "n_lanes_evaluated": len(per_lane_mape),
            "per_lane_mape_zero_actual_policy": "dropped",
            "best_5_lanes_mape": best5,
            "worst_5_lanes_mape": worst5,
        },
        "prediction_intervals": {
            "method": "ets_native",
            "fallback_method": "heuristic_15pct",
            "fallback_lanes_count": len(pi_fallback_lanes),
            "fallback_lanes": pi_fallback_lanes,
        },
        "forecast_horizons_days": list(DEMAND_FORECAST_HORIZONS),
        "notes": {
            "metrics_source": "holdout_model",
            "shipped_model": "refitted_on_full_history",
            "explanation": (
                "Metrics above were computed from a model fit on the training"
                " split only.  The shipped model.joblib was then re-fit on the"
                " full history (train + holdout) for maximum forecast quality."
            ),
        },
    }
    meta_path = model_dir / "metadata.json"
    _atomic_json(meta_path, metadata)
    log.info("  metadata.json → %s", meta_path)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    hdr = f"{'Lane':<50} {'MAPE %':>8}"
    sep = "=" * (len(hdr))
    print(f"\n{sep}")
    print("  NexaFreight — Demand Forecast (T-038) Training Summary")
    print(sep)
    print(f"  Qualified lanes: {len(qualified_ids)} / {total_lanes}")
    print(f"  WAPE:            {wape:.1f}%")
    print(f"  Median MAPE:     {float(np.median(list(per_lane_mape.values()))):.1f}%"
          if per_lane_mape else "  Median MAPE:     N/A")
    print(f"  Runtime:         {runtime}s")
    print(f"  Model SHA-256:   {mhash}")
    print()
    print("  BEST 5 lanes:")
    for lane, mape in list(sorted_mapes)[:5]:
        print(f"    {lane:<48}  {mape:>6.1f}%")
    print()
    print("  WORST 5 lanes:")
    for lane, mape in list(sorted_mapes)[-5:]:
        print(f"    {lane:<48}  {mape:>6.1f}%")
    print(f"{sep}\n")


# ============================================================================
# CLI entry point
# ============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Train StatsForecast AutoETS Demand Model (T-038)."
    )
    parser.add_argument(
        "--min-series", type=int, default=DEMAND_MIN_SERIES_LEN,
        help=f"Minimum weeks of history required per lane (default: {DEMAND_MIN_SERIES_LEN}).",
    )
    parser.add_argument(
        "--holdout-weeks", type=int, default=DEMAND_HOLDOUT_WEEKS,
        help=f"Weeks to hold out for MAPE evaluation (default: {DEMAND_HOLDOUT_WEEKS}).",
    )
    args = parser.parse_args()

    try:
        train(min_series_len=args.min_series, holdout_weeks=args.holdout_weeks)
    except Exception:
        log.exception("Demand model training failed")
        sys.exit(1)
