"""
features.py — Feature-engineering pipeline for the NexaFreight delay classifier.

Public API
----------
    build_features(raw_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]

        Transform a raw DataFrame (as returned by data_source.load_raw) into
        (X, y) ready for chronological splitting and model training.

        X — exactly FEATURE_COLUMNS in the authoritative order from constants.py
        y — integer binary label (0/1), no nulls, name == LABEL_COLUMN

Design decisions
----------------
- No database calls.  All data must already be present in raw_df.
- No congestion features.  origin_congestion / dest_congestion are constant
  1.0 (IMF 2019-2024 data has no valid join to DataCo 2015-2018 history).
- Banned-column leakage guard: raises RuntimeError if any banned column
  somehow ends up in FEATURE_COLUMNS (defence-in-depth check).
- Extra columns in raw_df that are not in FEATURE_COLUMNS are silently ignored.
- Missing categorical values are replaced with the safe sentinel "__MISSING__".
- Numeric columns are coerced with pd.to_numeric(errors="coerce").
"""

from __future__ import annotations

import pandas as pd

import nexafreight.ml.constants as _constants
from nexafreight.ml.constants import (
    BANNED_COLUMNS,
    CATEGORICAL_COLUMNS,
    FEATURE_COLUMNS,
    LABEL_COLUMN,
    NUMERIC_COLUMNS,
    TIME_AXIS_COLUMN,
)


def build_features(
    raw_df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Build (X, y) from a raw joined DataFrame.

    Parameters
    ----------
    raw_df : pd.DataFrame
        Output of data_source.load_raw() or a synthetic equivalent that
        contains at least TIME_AXIS_COLUMN, LABEL_COLUMN, and all columns
        named in FEATURE_COLUMNS (except the three derived calendar fields
        sla_month, sla_weekday, sla_quarter, which are derived here from
        TIME_AXIS_COLUMN).

    Returns
    -------
    X : pd.DataFrame
        Columns == FEATURE_COLUMNS, in that exact order.
        Categorical columns: object dtype, missing filled with "__MISSING__".
        Numeric columns: float64 (coerced), NaN kept for model-native handling.

    y : pd.Series
        Integer (0/1), no nulls, name == LABEL_COLUMN.

    Raises
    ------
    KeyError
        If LABEL_COLUMN or TIME_AXIS_COLUMN is absent from raw_df.
    ValueError
        If LABEL_COLUMN contains null values (must be clean before calling).
    RuntimeError
        If a banned column has been erroneously added to FEATURE_COLUMNS
        (leakage guard — defence-in-depth).
    """
    df = raw_df.copy()

    # ------------------------------------------------------------------
    # 1. Label: validate nulls BEFORE converting to int
    # ------------------------------------------------------------------
    if LABEL_COLUMN not in df.columns:
        raise KeyError(f"Label column '{LABEL_COLUMN}' not found in raw_df.")

    null_count = df[LABEL_COLUMN].isna().sum()
    if null_count > 0:
        raise ValueError(
            f"LABEL_COLUMN '{LABEL_COLUMN}' has {null_count} null value(s). "
            "Drop or impute before calling build_features."
        )

    y: pd.Series = df[LABEL_COLUMN].astype(int)
    y.name = LABEL_COLUMN

    # ------------------------------------------------------------------
    # 2. Derive calendar features from TIME_AXIS_COLUMN
    #    (sla_month, sla_weekday, sla_quarter are in FEATURE_COLUMNS)
    # ------------------------------------------------------------------
    if TIME_AXIS_COLUMN not in df.columns:
        raise KeyError(f"Time-axis column '{TIME_AXIS_COLUMN}' not found in raw_df.")

    sla = pd.to_datetime(df[TIME_AXIS_COLUMN])
    df["sla_month"]   = sla.dt.month.astype(int)
    df["sla_weekday"] = sla.dt.dayofweek.astype(int)   # 0=Monday … 6=Sunday
    df["sla_quarter"] = sla.dt.quarter.astype(int)

    # ------------------------------------------------------------------
    # 3. Read the current module-level lists so that monkeypatching in tests
    #    is respected (tests patch _constants.FEATURE_COLUMNS, not the
    #    local name-binding, so always dereference via the module).
    # ------------------------------------------------------------------
    active_features  = _constants.FEATURE_COLUMNS
    active_numeric   = _constants.NUMERIC_COLUMNS
    active_cats      = _constants.CATEGORICAL_COLUMNS

    # ------------------------------------------------------------------
    # 4. Banned-column leakage guard
    # ------------------------------------------------------------------
    for col in active_features:
        if col in BANNED_COLUMNS:
            raise RuntimeError(
                f"BANNED column '{col}' found in FEATURE_COLUMNS. "
                "This is a leakage guard violation — remove it from FEATURE_COLUMNS."
            )

    # ------------------------------------------------------------------
    # 5. Cast categorical columns
    # ------------------------------------------------------------------
    for col in active_cats:
        if col in df.columns:
            df[col] = (
                df[col]
                .astype(object)
                .fillna("__MISSING__")
                .astype(object)
            )
        else:
            # Column absent from raw_df — create it with the sentinel value
            df[col] = "__MISSING__"

    # ------------------------------------------------------------------
    # 6. Cast numeric columns
    # ------------------------------------------------------------------
    for col in active_numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            # Column absent — create NaN column (model handles downstream)
            df[col] = float("nan")

    # ------------------------------------------------------------------
    # 7. Select exactly FEATURE_COLUMNS in the authoritative order
    # ------------------------------------------------------------------
    X = df[active_features].copy()

    return X, y
