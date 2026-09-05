#!/usr/bin/env python
"""
09_build_training_data.py
=========================
Orchestration script for the NexaFreight ML training data pipeline.

Steps
-----
1. Load raw data       (data_source.load_raw)
2. Build features      (features.build_features)
3. Time-based split    (on sla_deadline; no future leakage)
4. Validate splits     (chronological disjointness, label integrity)
5. Save artefacts:
     data/processed/train.parquet
     data/processed/val.parquet
     data/processed/test.parquet
     models/delay_classifier/feature_schema.json

Prints a summary table at the end.

Usage
-----
    .venv/Scripts/python scripts/09_build_training_data.py
"""

import json
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Bootstrap: ensure the project src/ is on the path when run as a script
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from nexafreight.ml.constants import (  # noqa: E402
    CATEGORICAL_COLUMNS,
    FEATURE_COLUMNS,
    LABEL_COLUMN,
    NUMERIC_COLUMNS,
    SPLIT_DATES,
    TIME_AXIS_COLUMN,
)
from nexafreight.ml.data_source import load_raw  # noqa: E402
from nexafreight.ml.features import build_features  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("09_build_training_data")

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------
PROCESSED_DIR = _PROJECT_ROOT / "data" / "processed"
MODEL_DIR = _PROJECT_ROOT / "models" / "delay_classifier"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_git_sha() -> str:
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return sha
    except Exception:
        return "unknown"


def _time_split(
    df: pd.DataFrame,
    X: pd.DataFrame,
    y: pd.Series,
) -> dict[str, dict]:
    """
    Split (X, y) chronologically using sla_deadline from df.

    Returns a dict:
        {
            "train": {"X": ..., "y": ..., "dates": pd.Series},
            "val":   {...},
            "test":  {...},
        }
    """
    train_end = pd.Timestamp(SPLIT_DATES["train_end"])
    val_end = pd.Timestamp(SPLIT_DATES["val_end"])

    sla = pd.to_datetime(df[TIME_AXIS_COLUMN])

    masks = {
        "train": sla < train_end,
        "val": (sla >= train_end) & (sla < val_end),
        "test": sla >= val_end,
    }

    splits = {}
    for name, mask in masks.items():
        splits[name] = {
            "X": X[mask].reset_index(drop=True),
            "y": y[mask].reset_index(drop=True),
            "dates": sla[mask].reset_index(drop=True),
        }
    return splits


def _validate_splits(splits: dict) -> None:
    """Validate chronological disjointness and label integrity."""
    train_max = splits["train"]["dates"].max()
    val_min = splits["val"]["dates"].min()
    val_max = splits["val"]["dates"].max()
    test_min = splits["test"]["dates"].min()

    assert train_max < val_min, f"OVERLAP: train max ({train_max}) >= val min ({val_min})"
    assert val_max < test_min, f"OVERLAP: val max ({val_max}) >= test min ({test_min})"
    logger.info("Split chronological disjointness: OK")

    for name, split in splits.items():
        y = split["y"]
        assert y.isna().sum() == 0, f"[{name}] label has nulls"
        assert set(y.unique()).issubset({0, 1}), f"[{name}] label not binary"
        logger.info(
            "[%s] rows=%d, date_range=[%s, %s], positive_rate=%.4f",
            name,
            len(y),
            split["dates"].min().date(),
            split["dates"].max().date(),
            y.mean(),
        )


def _save_parquets(splits: dict) -> None:
    """Save train/val/test as parquet files (X + y combined)."""
    for name, split in splits.items():
        combined = split["X"].copy()
        combined[LABEL_COLUMN] = split["y"].values
        path = PROCESSED_DIR / f"{name}.parquet"
        combined.to_parquet(path, index=False)
        logger.info("Saved %s -> %s  (%d rows)", name, path.name, len(combined))


def _build_feature_schema(splits: dict) -> dict:
    """Build the feature_schema.json artefact."""
    schema: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "git_sha": _get_git_sha(),
        "label_column": LABEL_COLUMN,
        "time_axis_column": TIME_AXIS_COLUMN,
        "split_dates": SPLIT_DATES,
        "feature_columns": FEATURE_COLUMNS,
        "categorical_columns": CATEGORICAL_COLUMNS,
        "numeric_columns": NUMERIC_COLUMNS,
        "dtypes": {},
        "categorical_levels": {},
        "null_policy": "downstream model must handle NaN (e.g. imputation or native NaN support)",
        "splits": {},
    }

    # dtypes from train X
    train_X = splits["train"]["X"]
    schema["dtypes"] = {col: str(train_X[col].dtype) for col in FEATURE_COLUMNS}

    # categorical levels from train set (contract for inference)
    for col in CATEGORICAL_COLUMNS:
        if col in train_X.columns:
            levels = sorted(train_X[col].dropna().unique().tolist())
            schema["categorical_levels"][col] = levels

    # per-split stats
    for name, split in splits.items():
        y = split["y"]
        dates = split["dates"]
        schema["splits"][name] = {
            "rows": int(len(y)),
            "date_range": [
                str(dates.min().date()),
                str(dates.max().date()),
            ],
            "positive_rate": round(float(y.mean()), 6),
            "positive_count": int(y.sum()),
            "negative_count": int((y == 0).sum()),
        }

    return schema


def _print_summary_table(splits: dict) -> None:
    """Print the summary table to stdout."""
    header = f"{'Split':<8} {'Rows':>8} {'Date start':<12} {'Date end':<12} {'Positive rate':>14}"
    sep = "-" * len(header)
    print()
    print("=" * len(header))
    print("  NexaFreight — Training Data Build Summary")
    print("=" * len(header))
    print(header)
    print(sep)
    for name, split in splits.items():
        y = split["y"]
        dates = split["dates"]
        print(
            f"{name:<8} {len(y):>8,d} "
            f"{str(dates.min().date()):<12} "
            f"{str(dates.max().date()):<12} "
            f"{y.mean():>14.4f}"
        )
    total_rows = sum(len(s["y"]) for s in splits.values())
    total_pos = sum(s["y"].sum() for s in splits.values())
    print(sep)
    print(f"{'TOTAL':<8} {total_rows:>8,d} {'':12} {'':12} {total_pos/total_rows:>14.4f}")
    print("=" * len(header))
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    logger.info("=== NexaFreight: 09_build_training_data ===")

    # 1. Load raw
    logger.info("Step 1/5: Loading raw data …")
    raw_df = load_raw()

    # 2. Build features
    logger.info("Step 2/5: Building features …")
    X, y = build_features(raw_df)

    # 3. Time-based split
    logger.info("Step 3/5: Splitting on %s …", TIME_AXIS_COLUMN)
    splits = _time_split(raw_df, X, y)

    # 4. Validate
    logger.info("Step 4/5: Validating splits …")
    _validate_splits(splits)

    # 5. Save artefacts
    logger.info("Step 5/5: Saving artefacts …")
    _save_parquets(splits)

    schema = _build_feature_schema(splits)
    schema_path = MODEL_DIR / "feature_schema.json"
    with open(schema_path, "w", encoding="utf-8") as fh:
        json.dump(schema, fh, indent=2, default=str)
    logger.info("Saved feature schema -> %s", schema_path)

    # Print summary table
    _print_summary_table(splits)

    # Print schema JSON to stdout  (ensure_ascii=True for Windows cp1252 consoles)
    sep = "-" * 60
    print(sep)
    print("  models/delay_classifier/feature_schema.json")
    print(sep)
    print(json.dumps(schema, indent=2, default=str, ensure_ascii=True))
    print()
    logger.info("Done.")


if __name__ == "__main__":
    main()
