"""
13_model_diagnostics.py — Feature importance diagnostics (E3).

Loads saved model artifacts (NO retraining) and prints per-model feature
importance by gain, top 8, as % of total. Purpose: determine whether
shipping_mode dominance explains ETA baseline parity.
"""

from __future__ import annotations

import sys
from pathlib import Path

import joblib

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from nexafreight.ml.constants import DELAY_MODEL_DIR, ETA_MODEL_DIR  # noqa: E402


def print_importance(name: str, booster) -> None:
    gains = booster.feature_importance(importance_type="gain")
    names = booster.feature_name()
    total = gains.sum() or 1.0
    pairs = sorted(zip(names, gains, strict=False), key=lambda x: -x[1])[:8]
    print(f"\n=== {name} — top 8 by gain ===")
    for feat, g in pairs:
        bar = "#" * int(g / total * 40)
        print(f"  {feat:28s} {g / total * 100:5.1f}%  {bar}")


def get_booster(artifact, keys=("booster", "model")):
    """Handle artifact dicts with different key conventions."""
    if not isinstance(artifact, dict):
        return artifact
    for k in keys:
        if k in artifact:
            return artifact[k]
    raise KeyError(f"No booster found; artifact keys = {list(artifact.keys())}")


def main() -> None:
    # --- Delay classifier (T-036) ---
    delay_path = Path(DELAY_MODEL_DIR) / "model.joblib"
    if delay_path.exists():
        art = joblib.load(delay_path)
        print_importance("Delay Classifier", get_booster(art))
    else:
        print(f"SKIP: {delay_path} not found")

    # --- ETA quantile models (T-037) ---
    eta_path = Path(ETA_MODEL_DIR) / "model.joblib"
    if eta_path.exists():
        art = joblib.load(eta_path)
        for tag, booster in art["models"].items():
            print_importance(f"ETA {tag.upper()}", booster)
    else:
        print(f"SKIP: {eta_path} not found")

    # Demand model is univariate AutoETS — no feature importance; skipped.
    print("\nDemand forecast (T-038): univariate ETS, no feature importance — N/A")


if __name__ == "__main__":
    main()
