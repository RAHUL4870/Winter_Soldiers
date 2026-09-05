#!/usr/bin/env python
"""
11_train_eta_model.py — Train LightGBM Quantile ETA Regressor (Phase 3, T-037).

Pipeline
--------
  1. Load raw joined data  (orders + shipments + legs + DataCo CSV)
  2. Derive target residual:
       residual = days_for_shipping_real − scheduled_shipping_days
       (positive = late,  negative = early)
  3. Feature engineering via the authoritative 14-feature contract
  4. Chronological time-based split on sla_deadline  (train / val / test)
  5. Fit naive grouped-quantile baseline  (empirical quantiles per shipping_mode)
  6. Train three LightGBM quantile regressors:
       alpha = 0.10  (optimistic / P10)
       alpha = 0.50  (median / P50)
       alpha = 0.85  (conservative / P85 — used for SLA risk checks)
  7. Evaluate:
       – per-quantile pinball loss  (train / val / test)
       – empirical [P10, P85] interval coverage
       – raw and post-correction monotonicity rate
       – lift over grouped baseline
       – P50 mean absolute error
  8. Save artifacts:
       models/eta_quantile/model.joblib
       models/eta_quantile/feature_schema.json
       models/eta_quantile/metadata.json

Usage
-----
  python scripts/11_train_eta_model.py
  python scripts/11_train_eta_model.py --max-rounds 500 --early-stopping 30
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
import lightgbm as lgb
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=UserWarning)

# ---------------------------------------------------------------------------
# Path setup  — make ``src/`` importable when running from project root
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from nexafreight.ml.constants import (  # noqa: E402
    BANNED_COLUMNS,
    CATEGORICAL_COLUMNS,
    ETA_ACTUAL_DAYS_COLUMN,
    ETA_MODEL_DIR,
    ETA_QUANTILES,
    ETA_SCHEDULED_DAYS_COLUMN,
    ETA_TARGET_COLUMN,
    FEATURE_COLUMNS,
    MISSING_SENTINEL,
    NUMERIC_COLUMNS,
    SPLIT_DATES,
    TIME_AXIS_COLUMN,
)
from nexafreight.ml.data_source import load_raw  # noqa: E402
from nexafreight.ml.eta_model import interval_coverage, pinball_loss  # noqa: E402
from nexafreight.ml.features import build_features  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("train_eta")

# ---------------------------------------------------------------------------
# Script-level constants
# ---------------------------------------------------------------------------
RANDOM_SEED = 42
MODEL_VERSION = "1.0.0"


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


def _qtag(alpha: float) -> str:
    """0.10 → 'p10',  0.50 → 'p50',  0.85 → 'p85'."""
    return f"p{int(round(alpha * 100))}"


def _date_range(s: pd.Series) -> str:
    if s.empty:
        return "[empty]"
    lo, hi = s.min(), s.max()
    if pd.isna(lo) or pd.isna(hi):
        return "[invalid]"
    return f"[{lo.date()} .. {hi.date()}]"


def _lift_pct(model_loss: float, baseline_loss: float) -> float | None:
    if not np.isfinite(baseline_loss) or np.isclose(baseline_loss, 0.0):
        return None
    return float((baseline_loss - model_loss) / baseline_loss * 100.0)


def _jround(v: Any, digits: int = 4) -> Any:
    """Round finite floats for JSON;  pass None / non-finite through as None."""
    if v is None:
        return None
    fv = float(v)
    return round(fv, digits) if np.isfinite(fv) else None


def _atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2, allow_nan=False, default=str))
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


# ============================================================================
# Validation
# ============================================================================
def _validate_config(max_rounds: int, early_stop: int) -> list[float]:
    """Validate constants and CLI args;  return sorted quantile list."""
    if max_rounds <= 0:
        raise ValueError(f"max_rounds must be > 0, got {max_rounds}")
    if early_stop <= 0:
        raise ValueError(f"early_stopping must be > 0, got {early_stop}")

    feats = list(FEATURE_COLUMNS)
    if not feats:
        raise ValueError("FEATURE_COLUMNS is empty")

    dupes = sorted(f for f in set(feats) if feats.count(f) > 1)
    if dupes:
        raise ValueError(f"FEATURE_COLUMNS has duplicates: {dupes}")

    banned = sorted(set(feats) & set(BANNED_COLUMNS))
    if banned:
        raise RuntimeError(f"BANNED columns in FEATURE_COLUMNS: {banned}")

    if TIME_AXIS_COLUMN in feats:
        raise RuntimeError(f"TIME_AXIS_COLUMN '{TIME_AXIS_COLUMN}' must not be a feature")
    if ETA_TARGET_COLUMN in feats:
        raise RuntimeError(f"ETA_TARGET_COLUMN '{ETA_TARGET_COLUMN}' must not be a feature")
    if ETA_ACTUAL_DAYS_COLUMN in feats:
        raise RuntimeError(
            f"ETA_ACTUAL_DAYS_COLUMN '{ETA_ACTUAL_DAYS_COLUMN}' must not be a feature"
        )

    quantiles = sorted(float(q) for q in ETA_QUANTILES)
    expected = [0.10, 0.50, 0.85]
    if not np.allclose(quantiles, expected, atol=1e-12):
        raise ValueError(f"ETA_QUANTILES must be {expected}, got {quantiles}")

    return quantiles


# ============================================================================
# Data preparation
# ============================================================================
def _prepare_features(raw: pd.DataFrame) -> pd.DataFrame:
    """Run build_features() and enforce the 14-column contract."""
    result = build_features(raw)
    X = result[0] if isinstance(result, tuple) else result

    if not isinstance(X, pd.DataFrame):
        raise TypeError(f"build_features() returned {type(X).__name__}, expected DataFrame")

    missing = [c for c in FEATURE_COLUMNS if c not in X.columns]
    if missing:
        raise KeyError(f"Missing feature column(s): {missing}")

    X = X[list(FEATURE_COLUMNS)].copy()

    # Coerce numerics
    for col in NUMERIC_COLUMNS:
        X[col] = pd.to_numeric(X[col], errors="coerce")

    return X


def _align_categoricals(
    splits: dict[str, pd.DataFrame],
    cat_cols: list[str],
) -> tuple[dict[str, pd.DataFrame], dict[str, list[str]]]:
    """
    Learn categorical levels from training split ONLY.
    Unseen val/test levels become NaN (matching inference-time behaviour).
    """
    out = {k: df.copy() for k, df in splits.items()}
    levels: dict[str, list[str]] = {}

    for col in cat_cols:
        train_vals = out["train"][col].dropna().astype(str).unique()
        cats = sorted(str(v) for v in train_vals)
        levels[col] = cats

        for name in out:
            raw = out[name][col].fillna(MISSING_SENTINEL).astype(str)
            out[name][col] = pd.Categorical(raw, categories=cats + [MISSING_SENTINEL])

    return out, levels


# ============================================================================
# Grouped quantile baseline
# ============================================================================
class GroupedQuantileBaseline:
    """Empirical quantile baseline grouped by shipping_mode."""

    def __init__(self, quantiles: list[float]) -> None:
        self.quantiles = quantiles
        self.global_q: dict[float, float] = {}
        self.group_q: dict[str, dict[float, float]] = {}
        self._fitted = False

    def fit(self, df: pd.DataFrame, target: str, group: str) -> None:
        y = pd.to_numeric(df[target], errors="coerce")
        mask = np.isfinite(y)
        y_clean = y[mask]
        groups = df.loc[mask, group].astype(str)

        self.global_q = {q: float(np.quantile(y_clean, q)) for q in self.quantiles}
        self.group_q = {}
        for name, gy in y_clean.groupby(groups):
            if len(gy) >= 10:
                self.group_q[str(name)] = {q: float(np.quantile(gy, q)) for q in self.quantiles}
        self._fitted = True

    def predict(self, X: pd.DataFrame) -> dict[float, np.ndarray]:
        if not self._fitted:
            raise RuntimeError("Baseline not fitted")
        n = len(X)
        preds = {q: np.full(n, self.global_q[q]) for q in self.quantiles}
        if "shipping_mode" in X.columns:
            modes = X["shipping_mode"].astype(str).values
            for i, m in enumerate(modes):
                if m in self.group_q:
                    for q in self.quantiles:
                        preds[q][i] = self.group_q[m][q]
        return preds


# ============================================================================
# Main training routine
# ============================================================================
def train(max_rounds: int = 1000, early_stop: int = 50) -> None:
    quantiles = _validate_config(max_rounds, early_stop)
    started = time.time()
    trained_at = datetime.now(UTC).isoformat()
    model_dir = Path(ETA_MODEL_DIR)
    model_dir.mkdir(parents=True, exist_ok=True)

    log.info("=" * 64)
    log.info("  NexaFreight Phase 3 — Train Quantile ETA Regressor (T-037)")
    log.info("=" * 64)

    # ------------------------------------------------------------------
    # Step 1: Load raw data
    # ------------------------------------------------------------------
    log.info("Step 1/8 — Loading raw data via data_source.load_raw() ...")
    raw = load_raw()
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise ValueError("load_raw() returned empty or non-DataFrame")
    log.info("  %d rows loaded", len(raw))

    for col in (ETA_ACTUAL_DAYS_COLUMN, ETA_SCHEDULED_DAYS_COLUMN, TIME_AXIS_COLUMN):
        if col not in raw.columns:
            raise KeyError(f"Raw data missing required column: '{col}'")

    # ------------------------------------------------------------------
    # Step 2: Derive target residual
    # ------------------------------------------------------------------
    log.info(
        "Step 2/8 — Deriving target: %s − %s ...", ETA_ACTUAL_DAYS_COLUMN, ETA_SCHEDULED_DAYS_COLUMN
    )

    actual = pd.to_numeric(raw[ETA_ACTUAL_DAYS_COLUMN], errors="coerce")
    sched = pd.to_numeric(raw[ETA_SCHEDULED_DAYS_COLUMN], errors="coerce")

    valid = actual.notna() & sched.notna() & (actual >= 0) & (sched > 0)
    dropped = int((~valid).sum())
    if dropped:
        log.warning("  Dropped %d rows with invalid durations", dropped)

    raw = raw[valid].reset_index(drop=True)
    actual = actual[valid].reset_index(drop=True)
    sched = sched[valid].reset_index(drop=True)
    y_all = (actual - sched).rename(ETA_TARGET_COLUMN).astype(float)

    if raw.empty:
        raise ValueError("No rows remain after duration validation")
    log.info(
        "  %d valid rows,  target mean=%+.3f,  std=%.3f", len(y_all), y_all.mean(), y_all.std()
    )

    # ------------------------------------------------------------------
    # Step 3: Feature engineering
    # ------------------------------------------------------------------
    log.info("Step 3/8 — Building %d-feature matrix ...", len(FEATURE_COLUMNS))
    X_all = _prepare_features(raw)

    if len(X_all) != len(y_all):
        raise ValueError(f"Feature/target length mismatch: {len(X_all)} vs {len(y_all)}")

    # ------------------------------------------------------------------
    # Step 4: Chronological split
    # ------------------------------------------------------------------
    log.info("Step 4/8 — Chronological split on '%s' ...", TIME_AXIS_COLUMN)

    time_s = pd.to_datetime(raw[TIME_AXIS_COLUMN], errors="coerce", utc=True)
    time_valid = time_s.notna()
    if not time_valid.all():
        n_bad = int((~time_valid).sum())
        log.warning("  Dropping %d rows with invalid %s", n_bad, TIME_AXIS_COLUMN)
        X_all = X_all[time_valid].reset_index(drop=True)
        y_all = y_all[time_valid].reset_index(drop=True)
        time_s = time_s[time_valid].reset_index(drop=True)

    train_end = pd.Timestamp(SPLIT_DATES["train_end"], tz="UTC")
    val_end = pd.Timestamp(SPLIT_DATES["val_end"], tz="UTC")

    masks = {
        "train": time_s < train_end,
        "val": (time_s >= train_end) & (time_s < val_end),
        "test": time_s >= val_end,
    }

    X_splits: dict[str, pd.DataFrame] = {}
    y_splits: dict[str, pd.Series] = {}
    for name, m in masks.items():
        X_splits[name] = X_all[m].reset_index(drop=True)
        y_splits[name] = y_all[m].reset_index(drop=True)
        ts = time_s[m]
        log.info(
            "  %-6s %6d rows   range=%s   mean_res=%+.3f",
            name,
            len(y_splits[name]),
            _date_range(ts),
            y_splits[name].mean(),
        )

    for name in ("train", "val", "test"):
        if len(X_splits[name]) == 0:
            raise ValueError(f"Split '{name}' is empty — check SPLIT_DATES")

    # Align categoricals from training data only
    cat_cols = [c for c in FEATURE_COLUMNS if c in CATEGORICAL_COLUMNS]
    num_cols = [c for c in FEATURE_COLUMNS if c not in cat_cols]
    X_splits, cat_levels = _align_categoricals(X_splits, cat_cols)

    # ------------------------------------------------------------------
    # Step 5: Baseline
    # ------------------------------------------------------------------
    log.info("Step 5/8 — Fitting grouped-quantile baseline ...")
    bl_df = X_splits["train"].copy()
    bl_df[ETA_TARGET_COLUMN] = y_splits["train"].values
    baseline = GroupedQuantileBaseline(quantiles)
    baseline.fit(bl_df, ETA_TARGET_COLUMN, "shipping_mode")

    # ------------------------------------------------------------------
    # Step 6: Train quantile models
    # ------------------------------------------------------------------
    log.info("Step 6/8 — Training LightGBM quantile regressors (P10 / P50 / P85) ...")
    boosters: dict[str, lgb.Booster] = {}
    best_iters: dict[str, int] = {}

    for alpha in quantiles:
        tag = _qtag(alpha)
        log.info("  --- %s  (alpha=%.2f) ---", tag.upper(), alpha)

        params = {
            "objective": "quantile",
            "alpha": alpha,
            "metric": "quantile",
            "verbosity": -1,
            "seed": RANDOM_SEED,
            "data_random_seed": RANDOM_SEED,
            "feature_fraction_seed": RANDOM_SEED,
            "bagging_seed": RANDOM_SEED,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": -1,
            "feature_pre_filter": False,
            "learning_rate": 0.03,
            "num_leaves": 15,
            "max_depth": 4,
            "min_data_in_leaf": 200,
            "feature_fraction": 0.8,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
        }

        dtrain = lgb.Dataset(
            X_splits["train"],
            label=y_splits["train"],
            categorical_feature=cat_cols,
            free_raw_data=False,
            params={"feature_pre_filter": False},
        )
        dval = lgb.Dataset(
            X_splits["val"],
            label=y_splits["val"],
            categorical_feature=cat_cols,
            reference=dtrain,
            free_raw_data=False,
            params={"feature_pre_filter": False},
        )

        booster = lgb.train(
            params,
            dtrain,
            num_boost_round=max_rounds,
            valid_sets=[dval],
            valid_names=["val"],
            callbacks=[lgb.early_stopping(early_stop, verbose=False)],
        )

        bi = int(booster.best_iteration or booster.current_iteration())
        boosters[tag] = booster
        best_iters[tag] = bi
        log.info("    converged at iteration %d", bi)

    # ------------------------------------------------------------------
    # Step 7: Evaluate
    # ------------------------------------------------------------------
    log.info("Step 7/8 — Evaluating pinball loss, coverage, monotonicity ...")

    eval_results: dict[str, dict[str, Any]] = {}
    coverage: dict[str, float] = {}
    bl_coverage: dict[str, float] = {}
    p50_mae: dict[str, float] = {}
    raw_mono: dict[str, float] = {}
    cor_mono: dict[str, float] = {}

    for split in ("train", "val", "test"):
        Xs = X_splits[split]
        yt = y_splits[split].values

        # Raw predictions
        raw_preds = {}
        for alpha in quantiles:
            tag = _qtag(alpha)
            raw_preds[tag] = boosters[tag].predict(Xs, num_iteration=best_iters[tag])

        # Monotonicity before correction
        raw_mat = np.column_stack([raw_preds[_qtag(a)] for a in quantiles])
        raw_mono[split] = float(np.mean(np.all(np.diff(raw_mat, axis=1) >= 0, axis=1)))

        # Rearrange
        sorted_mat = np.sort(raw_mat, axis=1)
        cor_mono[split] = float(np.mean(np.all(np.diff(sorted_mat, axis=1) >= 0, axis=1)))
        corrected = {_qtag(a): sorted_mat[:, i] for i, a in enumerate(quantiles)}

        # Baseline predictions
        bl_preds = baseline.predict(Xs)

        # Per-quantile metrics
        split_metrics: dict[str, Any] = {}
        for alpha in quantiles:
            tag = _qtag(alpha)
            ml = pinball_loss(yt, corrected[tag], alpha)
            bl = pinball_loss(yt, bl_preds[alpha], alpha)
            lift = _lift_pct(ml, bl)
            split_metrics[tag] = {
                "pinball_loss": round(ml, 4),
                "baseline_loss": round(bl, 4),
                "lift_pct": _jround(lift, 2),
            }

        # Coverage + MAE
        coverage[split] = interval_coverage(yt, corrected["p10"], corrected["p85"])
        bl_coverage[split] = interval_coverage(yt, bl_preds[0.10], bl_preds[0.85])
        p50_mae[split] = float(np.mean(np.abs(yt - corrected["p50"])))

        eval_results[split] = split_metrics

    # ------------------------------------------------------------------
    # Step 8: Save artifacts
    # ------------------------------------------------------------------
    log.info("Step 8/8 — Saving artifacts ...")

    # --- model.joblib ---
    artifact = {
        "models": boosters,
        "best_iterations": best_iters,
        "feature_columns": list(FEATURE_COLUMNS),
        "categorical_columns": cat_cols,
        "numeric_columns": num_cols,
        "category_levels": cat_levels,
        "model_version": MODEL_VERSION,
        "trained_at": trained_at,
    }
    model_path = model_dir / "model.joblib"
    _atomic_joblib(artifact, model_path)
    mhash = _file_hash(model_path)
    log.info("  model.joblib  → %s  (sha256=%s)", model_path, mhash)

    # --- feature_schema.json ---
    schema = {
        "model_version": MODEL_VERSION,
        "schema_version": "1.0.0",
        "extensibility": {
            "policy": (
                "v1 inference ignores features marked required=false "
                "with min_version greater than schema_version"
            ),
            "reserved_v2_features": [
                {
                    "name": "active_disruption_near_dest",
                    "dtype": "float",
                    "required": False,
                    "min_version": "2.0.0",
                },
                {
                    "name": "news_risk_score",
                    "dtype": "float",
                    "required": False,
                    "min_version": "2.0.0",
                },
            ],
        },
        "trained_at": trained_at,
        "git_sha": _git_sha(),
        "target_column": ETA_TARGET_COLUMN,
        "actual_days_column": ETA_ACTUAL_DAYS_COLUMN,
        "scheduled_days_column": ETA_SCHEDULED_DAYS_COLUMN,
        "time_axis_column": TIME_AXIS_COLUMN,
        "split_dates": dict(SPLIT_DATES),
        "quantiles": {_qtag(a): a for a in quantiles},
        "feature_columns": list(FEATURE_COLUMNS),
        "categorical_columns": cat_cols,
        "numeric_columns": num_cols,
        "categorical_levels": cat_levels,
        "postprocessing": {
            "quantile_rearrangement": "sort_per_row",
            "minimum_transit_days": 0.5,
        },
        "notes": [
            "Target = days_for_shipping_real − scheduled_shipping_days (positive = late).",
            "Categorical levels learned from training split only; unseen → __MISSING__.",
            "Raw quantile outputs sorted per row to enforce P10 ≤ P50 ≤ P85.",
            "Port congestion features excluded per T-035/T-036 audit.",
        ],
    }
    schema_path = model_dir / "feature_schema.json"
    _atomic_json(schema_path, schema)
    log.info("  feature_schema.json  → %s", schema_path)

    # --- metadata.json ---
    runtime = round(time.time() - started, 1)
    metadata = {
        "model_name": "eta_quantile_regressor",
        "model_version": MODEL_VERSION,
        "trained_at": trained_at,
        "git_sha": _git_sha(),
        "random_seed": RANDOM_SEED,
        "training_runtime_sec": runtime,
        "model_artifact_sha256": mhash,
        "data": {
            name: {
                "n_rows": int(len(y_splits[name])),
                "mean_residual": round(float(y_splits[name].mean()), 3),
                "std_residual": round(float(y_splits[name].std()), 3),
            }
            for name in ("train", "val", "test")
        },
        "features": {
            "n_features": len(FEATURE_COLUMNS),
            "feature_names": list(FEATURE_COLUMNS),
            "categorical": cat_cols,
            "numeric": num_cols,
        },
        "metrics": {name: eval_results[name] for name in ("train", "val", "test")},
        "interval_coverage_p10_p85": {
            name: round(coverage[name], 4) for name in ("train", "val", "test")
        },
        "baseline_coverage_p10_p85": {
            name: round(bl_coverage[name], 4) for name in ("train", "val", "test")
        },
        "raw_monotonicity_rate": {
            name: round(raw_mono[name], 4) for name in ("train", "val", "test")
        },
        "corrected_monotonicity_rate": {
            name: round(cor_mono[name], 4) for name in ("train", "val", "test")
        },
        "p50_mae_days": {name: round(p50_mae[name], 3) for name in ("train", "val", "test")},
        "best_iterations": best_iters,
        "training_parameters": {
            "max_rounds": max_rounds,
            "early_stopping_rounds": early_stop,
        },
    }
    meta_path = model_dir / "metadata.json"
    _atomic_json(meta_path, metadata)
    log.info("  metadata.json  → %s", meta_path)

    # ------------------------------------------------------------------
    # Summary table
    # ------------------------------------------------------------------
    hdr = f"{'Quantile':<10} {'Val Loss':>10} {'Test Loss':>10} {'Baseline':>10} {'Lift':>10}"
    sep = "=" * len(hdr)

    print(f"\n{sep}")
    print("  NexaFreight — ETA Quantile Model (T-037) Training Summary")
    print(sep)
    print(hdr)
    print("-" * len(hdr))

    for alpha in quantiles:
        tag = _qtag(alpha)
        vl = eval_results["val"][tag]["pinball_loss"]
        tl = eval_results["test"][tag]["pinball_loss"]
        bl = eval_results["test"][tag]["baseline_loss"]
        lift = eval_results["test"][tag]["lift_pct"]
        lift_s = "N/A" if lift is None else f"{lift:.1f}%"
        print(f"{tag.upper():<10} {vl:>10.4f} {tl:>10.4f} {bl:>10.4f} {lift_s:>10}")

    print("-" * len(hdr))
    print(f"  Test Coverage [P10..P85]:    {coverage['test'] * 100:.1f}%  (nominal 75%)")
    print(f"  Baseline Coverage:           {bl_coverage['test'] * 100:.1f}%")
    print(f"  Raw Monotonicity:            {raw_mono['test'] * 100:.1f}%")
    print(f"  Corrected Monotonicity:      {cor_mono['test'] * 100:.1f}%")
    print(f"  P50 Test MAE:                {p50_mae['test']:.2f} days")
    print(f"  Runtime:                     {runtime}s")
    print(f"  Model SHA-256:               {mhash}")
    print(f"{sep}\n")


# ============================================================================
# CLI entry point
# ============================================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train LightGBM Quantile ETA Regressor (T-037).")
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=1000,
        help="Maximum boosting iterations (default: 1000).",
    )
    parser.add_argument(
        "--early-stopping",
        type=int,
        default=50,
        help="Early-stopping patience rounds (default: 50).",
    )
    args = parser.parse_args()

    try:
        train(max_rounds=args.max_rounds, early_stop=args.early_stopping)
    except Exception:
        log.exception("ETA model training failed")
        sys.exit(1)
