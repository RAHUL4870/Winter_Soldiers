"""
eta_model.py — ETA Quantile Regressor inference module (Phase 3, T-037).

Public API
----------
EtaPrediction          Frozen dataclass returned by predict().
EtaQuantileModel       Load trained boosters and produce ETA predictions.
pinball_loss()         Quantile (tick) loss metric.
interval_coverage()    Empirical coverage of a prediction interval.
get_eta_model()        Convenience loader.

Design rules
------------
* Raw feature vectors NEVER appear in API responses.
* Every prediction carries provenance = "DERIVED".
* Monotonicity P10 <= P50 <= P85 is enforced even if raw boosters cross.
* Unseen categorical levels degrade to MISSING_SENTINEL, never crash.
* BANNED_COLUMNS are checked at both load() and predict() time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import joblib
import numpy as np
import pandas as pd

from nexafreight.ml.constants import (
    BANNED_COLUMNS,
    MISSING_SENTINEL,
    QUANTILE_KEYS,
)

# ---------------------------------------------------------------------------
# Constants local to inference
# ---------------------------------------------------------------------------
MIN_TRANSIT_DAYS: float = 0.5
DEFAULT_SCHEDULED_DAYS: float = 5.0


# ---------------------------------------------------------------------------
# Prediction dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EtaPrediction:
    """Immutable prediction envelope returned by EtaQuantileModel.predict()."""

    p10_eta_days: float
    p50_eta_days: float
    p85_eta_days: float

    p10_residual: float
    p50_residual: float
    p85_residual: float

    p10_remaining_days: float
    p50_remaining_days: float
    p85_remaining_days: float

    confidence_interval_width: float

    sla_risk: Optional[str]

    p10_eta_date: Optional[datetime]
    p50_eta_date: Optional[datetime]
    p85_eta_date: Optional[datetime]

    provenance: str = "DERIVED"


# ---------------------------------------------------------------------------
# Standalone metrics (used by both training script and tests)
# ---------------------------------------------------------------------------
def pinball_loss(
    y_true: Sequence[float],
    y_pred: Sequence[float],
    alpha: float,
) -> float:
    """
    Mean pinball (quantile / tick) loss.

    Parameters
    ----------
    y_true : array-like   Observed values.
    y_pred : array-like   Predicted quantile values.
    alpha  : float        Quantile level, must be in open interval (0, 1).

    Raises
    ------
    ValueError  If alpha is out of range or shapes mismatch.
    """
    if alpha <= 0.0 or alpha >= 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")

    yt = np.asarray(y_true, dtype=float)
    yp = np.asarray(y_pred, dtype=float)

    if yt.shape != yp.shape:
        raise ValueError(
            f"Shape mismatch: y_true {yt.shape} vs y_pred {yp.shape}"
        )

    diff = yt - yp
    loss = np.where(diff >= 0.0, alpha * diff, (1.0 - alpha) * (-diff))
    return float(np.mean(loss))


def interval_coverage(
    y_true: Sequence[float],
    lower: Sequence[float],
    upper: Sequence[float],
) -> float:
    """
    Fraction of observations inside [lower, upper] (inclusive both sides).

    Returns 0.0 for empty arrays.
    """
    yt = np.asarray(y_true, dtype=float)
    lo = np.asarray(lower, dtype=float)
    hi = np.asarray(upper, dtype=float)

    if len(yt) == 0:
        return 0.0

    return float(np.mean((yt >= lo) & (yt <= hi)))


# ---------------------------------------------------------------------------
# Model class
# ---------------------------------------------------------------------------
class EtaQuantileModel:
    """
    Inference wrapper around three trained LightGBM quantile boosters
    (P10 / P50 / P85).
    """

    def __init__(self) -> None:
        self.models: Dict[str, Any] = {}
        self.features: List[str] = []
        self.cat_cols: List[str] = []
        self.num_cols: List[str] = []
        self.cat_levels: Dict[str, List[str]] = {}
        self._is_loaded: bool = False
        self._model_version: Optional[str] = None

    # ------------------------------------------------------------------
    # Load from disk
    # ------------------------------------------------------------------
    def load(self, model_dir: Union[str, Path]) -> None:
        """Load a trained artifact bundle produced by 11_train_eta_model.py."""
        model_path = Path(model_dir) / "model.joblib"
        if not model_path.exists():
            raise FileNotFoundError(f"Model artifact not found: {model_path}")

        artifact = joblib.load(model_path)

        self.models = artifact["models"]
        self.features = list(artifact["feature_columns"])
        self.cat_cols = list(artifact["categorical_columns"])
        self.num_cols = list(artifact["numeric_columns"])
        self.cat_levels = artifact.get("category_levels", {})
        self._model_version = artifact.get("model_version", "unknown")

        # Leakage guard at load time
        banned = set(self.features).intersection(BANNED_COLUMNS)
        if banned:
            raise RuntimeError(f"BANNED column(s) in loaded model: {banned}")

        self._is_loaded = True

    # ------------------------------------------------------------------
    # SLA risk classification (static — usable without a model instance)
    # ------------------------------------------------------------------
    @staticmethod
    def assess_sla_risk(
        p10_date: datetime,
        p50_date: datetime,
        p85_date: datetime,
        sla_deadline: datetime,
    ) -> str:
        """
        Four-band SLA risk.

        ON_TIME : P85 <= SLA   (even conservative estimate is safe)
        MEDIUM  : P50 <= SLA < P85
        HIGH    : P10 <= SLA < P50
        BREACH  : SLA < P10   (even optimistic estimate misses SLA)
        """
        if p85_date <= sla_deadline:
            return "ON_TIME"
        if p50_date <= sla_deadline:
            return "MEDIUM"
        if p10_date <= sla_deadline:
            return "HIGH"
        return "BREACH"

    # ------------------------------------------------------------------
    # Internal encoding helpers
    # ------------------------------------------------------------------
    def _encode_single(self, row: Dict[str, Any]) -> pd.DataFrame:
        """Encode a single dict row into a 1-row DataFrame."""
        df = pd.DataFrame([row])

        for col in self.features:
            if col not in df.columns:
                df[col] = np.nan

        df = df[self.features].copy()

        for col in self.cat_cols:
            raw_val = df[col].iloc[0]
            str_val = str(raw_val) if pd.notna(raw_val) else MISSING_SENTINEL
            if col in self.cat_levels:
                known = self.cat_levels[col]
                if str_val not in known:
                    str_val = MISSING_SENTINEL
                df[col] = pd.Categorical(
                    [str_val],
                    categories=known + [MISSING_SENTINEL],
                )
            else:
                df[col] = pd.Categorical([str_val])

        for col in self.num_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

        return df

    def _encode_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Encode a multi-row DataFrame."""
        df = frame[self.features].copy()

        for col in self.cat_cols:
            vals = df[col].fillna(MISSING_SENTINEL).astype(str)
            if col in self.cat_levels:
                known = self.cat_levels[col]
                vals = vals.where(vals.isin(known), MISSING_SENTINEL)
                df[col] = pd.Categorical(
                    vals, categories=known + [MISSING_SENTINEL]
                )
            else:
                df[col] = pd.Categorical(vals)

        for col in self.num_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)

        return df

    # ------------------------------------------------------------------
    # Raw prediction + monotonic rearrangement
    # ------------------------------------------------------------------
    def _predict_raw(self, X: pd.DataFrame) -> Dict[str, np.ndarray]:
        """Run each booster and return raw residual arrays."""
        return {
            tag: np.asarray(self.models[tag].predict(X), dtype=float).ravel()
            for tag in QUANTILE_KEYS
        }

    @staticmethod
    def _rearrange(raw: Dict[str, np.ndarray]) -> Dict[str, np.ndarray]:
        """Per-row sort to enforce P10 <= P50 <= P85 on residuals."""
        matrix = np.column_stack([raw[tag] for tag in QUANTILE_KEYS])
        sorted_matrix = np.sort(matrix, axis=1)
        return {
            tag: sorted_matrix[:, i] for i, tag in enumerate(QUANTILE_KEYS)
        }

    # ------------------------------------------------------------------
    # Public predict
    # ------------------------------------------------------------------
    def predict(
        self,
        input_data: Union[Dict[str, Any], pd.DataFrame],
        departure_time: Optional[datetime] = None,
        sla_deadline: Optional[datetime] = None,
        scheduled_days: Optional[float] = None,
        days_elapsed: float = 0.0,
    ) -> Union[EtaPrediction, List[EtaPrediction]]:
        """
        Produce quantile ETA predictions.

        Parameters
        ----------
        input_data      dict (single row) or DataFrame (batch).
        departure_time  If given, calendar dates are computed.
        sla_deadline    If given with departure_time, SLA risk is assessed.
        scheduled_days  Override for planned transit; otherwise read from
                        the ``scheduled_shipping_days`` feature column.
        days_elapsed    Days already in transit (>= 0). Reduces remaining
                        but does NOT change total ETA.

        Returns
        -------
        EtaPrediction          for dict input
        List[EtaPrediction]    for DataFrame input
        """
        # --- Guards ---
        if not self._is_loaded:
            raise RuntimeError(
                "Model is not loaded — call load() or use get_eta_model()"
            )

        if days_elapsed < 0.0:
            raise ValueError(
                f"days_elapsed must be >= 0, got {days_elapsed}"
            )

        banned = set(self.features).intersection(BANNED_COLUMNS)
        if banned:
            raise RuntimeError(f"BANNED column(s) in features: {banned}")

        # --- Encode ---
        single = isinstance(input_data, dict)

        if single:
            X = self._encode_single(input_data)
        elif isinstance(input_data, pd.DataFrame):
            X = self._encode_frame(input_data)
        else:
            raise TypeError(
                f"Expected dict or DataFrame, got {type(input_data).__name__}"
            )

        # --- Scheduled days per row ---
        if scheduled_days is not None:
            sched_arr = np.full(len(X), scheduled_days, dtype=float)
        elif "scheduled_shipping_days" in X.columns:
            sched_arr = (
                pd.to_numeric(X["scheduled_shipping_days"], errors="coerce")
                .fillna(DEFAULT_SCHEDULED_DAYS)
                .to_numpy(dtype=float)
            )
        else:
            sched_arr = np.full(len(X), DEFAULT_SCHEDULED_DAYS, dtype=float)

        # --- Residual predictions ---
        raw_preds = self._predict_raw(X)
        corrected = self._rearrange(raw_preds)

        # --- Build EtaPrediction per row ---
        results: List[EtaPrediction] = []

        for i in range(len(X)):
            p10_res = float(corrected["p10"][i])
            p50_res = float(corrected["p50"][i])
            p85_res = float(corrected["p85"][i])

            sched = float(sched_arr[i])

            # ETA = scheduled + residual, floored at MIN_TRANSIT_DAYS
            p10_eta = max(MIN_TRANSIT_DAYS, sched + p10_res)
            p50_eta = max(MIN_TRANSIT_DAYS, sched + p50_res)
            p85_eta = max(MIN_TRANSIT_DAYS, sched + p85_res)

            # Re-enforce monotonicity after floor clipping
            p10_eta, p50_eta, p85_eta = sorted([p10_eta, p50_eta, p85_eta])

            # Remaining transit
            p10_rem = max(0.0, p10_eta - days_elapsed)
            p50_rem = max(0.0, p50_eta - days_elapsed)
            p85_rem = max(0.0, p85_eta - days_elapsed)

            ci_width = p85_rem - p10_rem

            # Calendar dates and SLA risk (only when departure is known)
            sla_risk: Optional[str] = None
            p10_date: Optional[datetime] = None
            p50_date: Optional[datetime] = None
            p85_date: Optional[datetime] = None

            if departure_time is not None:
                p10_date = departure_time + timedelta(days=p10_eta)
                p50_date = departure_time + timedelta(days=p50_eta)
                p85_date = departure_time + timedelta(days=p85_eta)

                if sla_deadline is not None:
                    sla_risk = self.assess_sla_risk(
                        p10_date, p50_date, p85_date, sla_deadline
                    )

            results.append(
                EtaPrediction(
                    p10_eta_days=round(p10_eta, 2),
                    p50_eta_days=round(p50_eta, 2),
                    p85_eta_days=round(p85_eta, 2),
                    p10_residual=round(p10_res, 2),
                    p50_residual=round(p50_res, 2),
                    p85_residual=round(p85_res, 2),
                    p10_remaining_days=round(p10_rem, 2),
                    p50_remaining_days=round(p50_rem, 2),
                    p85_remaining_days=round(p85_rem, 2),
                    confidence_interval_width=round(ci_width, 2),
                    sla_risk=sla_risk,
                    p10_eta_date=p10_date,
                    p50_eta_date=p50_date,
                    p85_eta_date=p85_date,
                )
            )

        return results[0] if single else results


# ---------------------------------------------------------------------------
# Convenience loader
# ---------------------------------------------------------------------------
def get_eta_model(
    model_dir: Union[str, Path] = "models/eta_quantile",
) -> EtaQuantileModel:
    """Load and return a ready-to-predict EtaQuantileModel."""
    model = EtaQuantileModel()
    model.load(model_dir)
    return model
