"""
tests/unit/test_ml_features.py
===============================
Unit tests for the NexaFreight ML feature pipeline.

All tests use a small synthetic fixture — no real DB or CSV is touched.

Tests
-----
    TestFeatureOrder
        X columns must be exactly FEATURE_COLUMNS in the same order.

    TestNoBannedColumnLeaks
        build_features() must raise RuntimeError if a banned column sneaks
        into FEATURE_COLUMNS (defence-in-depth leakage guard).

    TestChronologicalSplits
        train / val / test date ranges must not overlap and must be ordered
        (train max < val min, val max < test min).

    TestLabelIntegrity
        y must contain only {0, 1} and have zero nulls in every split.

    TestCategoricalColumns
        Categorical columns must be object/string dtype.
        Numeric columns must be numeric dtype.

    TestSplitSafety
        Calendar features (sla_month, sla_weekday, sla_quarter) must be
        present, numeric, and within valid ranges.
"""

import numpy as np
import pandas as pd
import pytest

from nexafreight.ml.constants import (
    BANNED_COLUMNS,
    CATEGORICAL_COLUMNS,
    FEATURE_COLUMNS,
    LABEL_COLUMN,
    NUMERIC_COLUMNS,
    SPLIT_DATES,
    TIME_AXIS_COLUMN,
)
from nexafreight.ml.features import build_features


# ---------------------------------------------------------------------------
# Synthetic fixture factory
# ---------------------------------------------------------------------------
def _make_raw_df(n: int = 120, seed: int = 42) -> pd.DataFrame:
    """
    Create a minimal synthetic DataFrame that looks like load_raw() output.

    - sla_deadline spans 2015-01-01 to 2018-06-01 (covers all three splits)
    - historical_late_delivery is random binary
    - All FEATURE_COLUMNS are present with realistic types
    - Excluded-v1 columns are present to verify build_features ignores them
    - No banned columns (those are tested separately via injection)
    """
    rng = np.random.default_rng(seed)

    dates = pd.date_range("2015-01-01", "2018-06-01", periods=n)

    data: dict = {
        TIME_AXIS_COLUMN:  dates,
        LABEL_COLUMN:      rng.integers(0, 2, size=n),
        # DB: orders
        "shipping_mode":   rng.choice(["Standard Class", "First Class", "SEA", "AIR"], size=n),
        "cargo_class":     rng.choice(["STANDARD", "REFRIGERATED", "HAZMAT"], size=n),
        "revenue":         rng.uniform(50, 2000, size=n),
        "shipping_cost":   rng.uniform(5, 200, size=n),
        # DB: shipments — excluded from v1 but present in raw fixture
        "container_count":        rng.integers(1, 10, size=n).astype(float),
        "primary_transport_mode": rng.choice(["SEA", "AIR", "RAIL", "ROAD"], size=n),
        # CSV
        "scheduled_shipping_days": rng.integers(1, 14, size=n).astype(float),
        "order_country":   rng.choice(["USA", "China", "Germany", "Brazil"], size=n),
        "customer_country": rng.choice(["USA", "UK", "India", "France"], size=n),
        "product_price":   rng.uniform(10, 500, size=n),
        "order_profit":    rng.uniform(-200, 500, size=n),
        # Leg aggregates
        "total_distance_km": rng.uniform(0, 25000, size=n),
        "leg_count":          rng.integers(1, 6, size=n).astype(float),
        # Excluded v1 features — present to verify they are silently ignored
        "has_air_leg":        rng.integers(0, 2, size=n).astype(float),
        "has_sea_leg":        rng.integers(0, 2, size=n).astype(float),
        "has_rail_leg":       rng.integers(0, 2, size=n).astype(float),
        "origin_congestion": np.ones(n, dtype=float),
        "dest_congestion":   np.ones(n, dtype=float),
    }

    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def raw_df() -> pd.DataFrame:
    return _make_raw_df()


@pytest.fixture()
def built(raw_df) -> tuple[pd.DataFrame, pd.Series]:
    """Call build_features — no DB calls or monkeypatching required in v1."""
    return build_features(raw_df)


# ---------------------------------------------------------------------------
# TestFeatureOrder
# ---------------------------------------------------------------------------
class TestFeatureOrder:
    def test_feature_order_matches_contract(self, built):
        """X columns must be exactly FEATURE_COLUMNS in the same order."""
        X, _ = built
        assert list(X.columns) == FEATURE_COLUMNS, (
            f"Column order mismatch.\n"
            f"Expected: {FEATURE_COLUMNS}\n"
            f"Got:      {list(X.columns)}"
        )

    def test_all_feature_columns_present(self, built):
        """Every column in FEATURE_COLUMNS must be present in X."""
        X, _ = built
        missing = [c for c in FEATURE_COLUMNS if c not in X.columns]
        assert not missing, f"Missing feature columns: {missing}"

    def test_no_extra_columns(self, built):
        """X must contain ONLY the columns in FEATURE_COLUMNS (no extras)."""
        X, _ = built
        extra = [c for c in X.columns if c not in FEATURE_COLUMNS]
        assert not extra, f"Extra (unexpected) columns in X: {extra}"

    def test_feature_count_is_14(self, built):
        """V1 contract specifies exactly 14 active features."""
        X, _ = built
        assert len(X.columns) == 14, (
            f"Expected 14 features (v1 contract), got {len(X.columns)}"
        )

    def test_row_count_preserved(self, raw_df, built):
        X, y = built
        assert len(X) == len(raw_df)
        assert len(y) == len(raw_df)


# ---------------------------------------------------------------------------
# TestNoBannedColumnLeaks
# ---------------------------------------------------------------------------
class TestNoBannedColumnLeaks:
    def test_banned_label_column_not_in_X(self, built):
        """The label column must not appear in X."""
        X, _ = built
        assert LABEL_COLUMN not in X.columns

    def test_banned_time_axis_not_in_X(self, built):
        """The time-axis column must not appear in X."""
        X, _ = built
        assert TIME_AXIS_COLUMN not in X.columns

    def test_banned_columns_not_in_X(self, built):
        """No banned column must appear in X."""
        X, _ = built
        leaked = [c for c in X.columns if c in BANNED_COLUMNS]
        assert not leaked, f"Banned columns found in X: {leaked}"

    def test_operational_timestamps_not_in_X(self, built):
        X, _ = built
        operational = ["created_at", "updated_at", "planned_departure", "planned_arrival"]
        found = [c for c in operational if c in X.columns]
        assert found == [], f"Operational timestamp(s) in X: {found}"

    def test_excluded_v1_features_not_in_X(self, built):
        """Redundant features confirmed excluded from v1 contract."""
        X, _ = built
        excluded = [
            "container_count",
            "primary_transport_mode",
            "has_air_leg",
            "has_sea_leg",
            "has_rail_leg",
            "origin_congestion",
            "dest_congestion",
        ]
        found = [c for c in excluded if c in X.columns]
        assert found == [], f"Excluded v1 feature(s) found in X: {found}"

    def test_build_features_raises_on_banned_column_injection(
        self, raw_df, monkeypatch
    ):
        """
        If a banned column somehow ends up in FEATURE_COLUMNS, the internal
        leakage guard must raise RuntimeError containing 'BANNED'.

        We patch FEATURE_COLUMNS and NUMERIC_COLUMNS on the
        nexafreight.ml.features module (which reads them via the _constants
        module reference), so the guard sees the injected banned column.

        monkeypatch restores the originals automatically after the test.
        """
        import nexafreight.ml.features as feat_module

        bad_features = FEATURE_COLUMNS + [LABEL_COLUMN]
        bad_numeric  = NUMERIC_COLUMNS + [LABEL_COLUMN]

        monkeypatch.setattr(feat_module._constants, "FEATURE_COLUMNS", bad_features)
        monkeypatch.setattr(feat_module._constants, "NUMERIC_COLUMNS",  bad_numeric)

        with pytest.raises(RuntimeError, match="BANNED"):
            build_features(raw_df)


# ---------------------------------------------------------------------------
# TestChronologicalSplits
# ---------------------------------------------------------------------------
class TestChronologicalSplits:
    """
    Validate that applying SPLIT_DATES to the synthetic fixture yields
    disjoint, correctly ordered splits.
    """

    @staticmethod
    def _do_split(raw_df):
        train_end = pd.Timestamp(SPLIT_DATES["train_end"])
        val_end   = pd.Timestamp(SPLIT_DATES["val_end"])
        dates = pd.to_datetime(raw_df[TIME_AXIS_COLUMN])
        return {
            "train": dates[dates < train_end],
            "val":   dates[(dates >= train_end) & (dates < val_end)],
            "test":  dates[dates >= val_end],
        }

    def test_splits_are_non_empty(self, raw_df):
        splits = self._do_split(raw_df)
        for name, dates in splits.items():
            assert len(dates) > 0, f"Split '{name}' is empty"

    def test_train_ends_before_val_starts(self, raw_df):
        splits = self._do_split(raw_df)
        assert splits["train"].max() < splits["val"].min(), (
            "train max date must be strictly before val min date"
        )

    def test_val_ends_before_test_starts(self, raw_df):
        splits = self._do_split(raw_df)
        assert splits["val"].max() < splits["test"].min(), (
            "val max date must be strictly before test min date"
        )

    def test_no_date_overlap(self, raw_df):
        splits = self._do_split(raw_df)
        train_set = set(splits["train"].dt.date)
        val_set   = set(splits["val"].dt.date)
        test_set  = set(splits["test"].dt.date)
        assert not train_set.intersection(val_set), "Overlap between train and val"
        assert not val_set.intersection(test_set),  "Overlap between val and test"
        assert not train_set.intersection(test_set), "Overlap between train and test"

    def test_no_future_leakage_in_train(self, raw_df):
        splits = self._do_split(raw_df)
        train_end = pd.Timestamp(SPLIT_DATES["train_end"])
        assert (splits["train"] < train_end).all(), (
            "Train split contains dates on or after train_end"
        )


# ---------------------------------------------------------------------------
# TestLabelIntegrity
# ---------------------------------------------------------------------------
class TestLabelIntegrity:
    def test_label_is_binary(self, built):
        _, y = built
        unique_vals = set(y.unique())
        assert unique_vals.issubset({0, 1}), (
            f"y contains non-binary values: {unique_vals}"
        )

    def test_label_has_no_nulls(self, built):
        _, y = built
        assert y.isna().sum() == 0, f"y has {y.isna().sum()} null values"

    def test_label_name(self, built):
        _, y = built
        assert y.name == LABEL_COLUMN, (
            f"y.name should be '{LABEL_COLUMN}', got '{y.name}'"
        )

    def test_label_dtype_is_integer(self, built):
        _, y = built
        assert pd.api.types.is_integer_dtype(y), (
            f"y dtype should be integer, got {y.dtype}"
        )

    def test_label_not_all_one_class(self, built):
        """With 120 synthetic rows and 50/50 split, both classes should appear."""
        _, y = built
        assert y.nunique() == 2, "y should have both classes (0 and 1)"


# ---------------------------------------------------------------------------
# TestCategoricalColumns
# ---------------------------------------------------------------------------
class TestCategoricalColumns:
    def test_categorical_columns_are_object_or_string(self, built):
        X, _ = built
        for col in CATEGORICAL_COLUMNS:
            assert col in X.columns
            assert X[col].dtype == object or pd.api.types.is_string_dtype(X[col]), (
                f"Expected object/string dtype for '{col}', got {X[col].dtype}"
            )

    def test_numeric_columns_are_numeric(self, built):
        X, _ = built
        for col in NUMERIC_COLUMNS:
            assert col in X.columns
            assert pd.api.types.is_numeric_dtype(X[col]), (
                f"Expected numeric dtype for '{col}', got {X[col].dtype}"
            )

    def test_categorical_and_numeric_partition_features(self):
        all_features = set(FEATURE_COLUMNS)
        cats = set(CATEGORICAL_COLUMNS)
        nums = set(NUMERIC_COLUMNS)
        assert cats | nums == all_features, (
            f"Features not fully partitioned.\n"
            f"  In FEATURE_COLUMNS but not in either list: "
            f"{all_features - cats - nums}"
        )
        assert cats & nums == set(), (
            f"Overlap between categorical and numeric: {cats & nums}"
        )

    def test_missing_categorical_filled_with_sentinel(self, raw_df):
        """Rows with NaN in a categorical column must become '__MISSING__'."""
        raw_df = raw_df.copy()
        raw_df.loc[0, "shipping_mode"] = None
        X, _ = build_features(raw_df)
        assert X.loc[0, "shipping_mode"] == "__MISSING__", (
            "NaN in categorical column was not replaced with '__MISSING__'"
        )


# ---------------------------------------------------------------------------
# TestSplitSafety  (calendar feature derivation)
# ---------------------------------------------------------------------------
class TestSplitSafety:
    def test_time_axis_derives_calendar_features(self, built):
        X, _ = built
        for col in ("sla_month", "sla_weekday", "sla_quarter"):
            assert col in X.columns, f"Calendar feature '{col}' missing"
            assert pd.api.types.is_numeric_dtype(X[col])

    def test_sla_month_range(self, built):
        X, _ = built
        assert X["sla_month"].between(1, 12).all(), (
            "sla_month values outside 1-12"
        )

    def test_sla_weekday_range(self, built):
        X, _ = built
        assert X["sla_weekday"].between(0, 6).all(), (
            "sla_weekday values outside 0-6"
        )

    def test_sla_quarter_range(self, built):
        X, _ = built
        assert X["sla_quarter"].between(1, 4).all(), (
            "sla_quarter values outside 1-4"
        )
