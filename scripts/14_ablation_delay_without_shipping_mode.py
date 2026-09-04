
"""
Script 14 — Ablation: Train delay classifier without shipping_mode (C1 / T-036).

Quantifies how much of the production delay classifier's test ROC-AUC depends
on the shipping_mode feature, which accounts for ~87.4% of model gain per E3
diagnostics.

Isolated experiment: writes ONLY to models/experiments/delay_without_shipping_mode/.
Never touches models/delay_classifier/model.joblib.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
import warnings
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

warnings.filterwarnings("ignore", category=UserWarning)
optuna.logging.set_verbosity(optuna.logging.WARNING)

log = logging.getLogger("nexafreight.ml.train_delay_ablation")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

RANDOM_SEED = 42
ABLATED_FEATURE = "shipping_mode"


# ---------------------------------------------------------------- paths
def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "pyproject.toml").exists():
            return p
    raise RuntimeError("repo root not found")


REPO_ROOT = find_repo_root(Path(__file__).resolve())
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
MODEL_DIR = REPO_ROOT / "models" / "delay_classifier"
EXPERIMENT_DIR = REPO_ROOT / "models" / "experiments" / "delay_without_shipping_mode"

sys.path.insert(0, str(REPO_ROOT / "src"))
from nexafreight.ml.constants import CATEGORICAL_COLUMNS  # noqa: E402


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------- data
def load_splits() -> tuple[dict[str, pd.DataFrame], dict]:
    schema_path = MODEL_DIR / "feature_schema.json"
    if not schema_path.exists():
        raise FileNotFoundError(
            f"{schema_path} missing — run scripts/10_train_delay_classifier.py first"
        )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    splits = {}
    for name in ("train", "val", "test"):
        path = PROCESSED_DIR / f"{name}.parquet"
        if not path.exists():
            raise FileNotFoundError(
                f"{path} missing — run scripts/09_build_training_data.py first"
            )
        splits[name] = pd.read_parquet(path)
    return splits, schema


def align_categoricals(
    dfs: dict[str, pd.DataFrame], cat_cols: list[str]
) -> tuple[dict[str, pd.DataFrame], dict[str, list[str]]]:
    """Learn category levels from train split only (matches production)."""
    levels: dict[str, list[str]] = {}
    out = {k: v.copy() for k, v in dfs.items()}
    for col in cat_cols:
        cats = sorted(out["train"][col].dropna().astype(str).unique().tolist())
        levels[col] = cats
        for name in out:
            out[name][col] = pd.Categorical(out[name][col].astype(str), categories=cats)
    return out, levels


# ---------------------------------------------------------------- metrics
def evaluate(y_true, y_score, tag: str) -> dict:
    m = {
        "roc_auc": float(roc_auc_score(y_true, y_score)),
        "pr_auc": float(average_precision_score(y_true, y_score)),
        "brier": float(brier_score_loss(y_true, np.clip(y_score, 0, 1))),
    }
    log.info(
        "%-32s ROC-AUC=%.4f  PR-AUC=%.4f  Brier=%.4f",
        tag, m["roc_auc"], m["pr_auc"], m["brier"],
    )
    return m


def _extract_prod_metrics(prod_meta: dict) -> tuple[float, float | None, str]:
    """Locate production test metrics; tolerate key-name variation."""
    metrics = prod_meta.get("metrics", {})
    for key in ("lgbm_tuned_test", "tuned_test", "test"):
        block = metrics.get(key)
        if isinstance(block, dict) and "roc_auc" in block:
            return float(block["roc_auc"]), block.get("pr_auc"), key
    raise KeyError(
        f"No production test metrics found. Available keys: {list(metrics.keys())}"
    )


# ---------------------------------------------------------------- main
def main(n_trials: int) -> None:
    t0 = time.time()
    EXPERIMENT_DIR.mkdir(parents=True, exist_ok=True)

    splits, schema = load_splits()
    label = schema["label_column"]

    # ---- feature selection: authoritative contract, minus the ablated feature
    all_features = [f for f in schema["feature_columns"] if f in splits["train"].columns]

    if ABLATED_FEATURE not in all_features:
        raise RuntimeError(
            f"'{ABLATED_FEATURE}' not present in production feature contract — "
            "nothing to ablate. Check feature_schema.json."
        )

    features = [f for f in all_features if f != ABLATED_FEATURE]
    assert ABLATED_FEATURE not in features

    # ---- categorical columns from the SCHEMA, not dtype inspection.
    # Parquet round-trips may yield dtype 'category', so `== object` under-detects.
    schema_cats = schema.get("categorical_columns") or list(CATEGORICAL_COLUMNS)
    cat_cols = [c for c in schema_cats if c in features]
    assert ABLATED_FEATURE not in cat_cols

    num_cols = [c for c in features if c not in cat_cols]

    log.info(
        "Features: %d total (%d categorical, %d numeric) — '%s' ABLATED (was %d features)",
        len(features), len(cat_cols), len(num_cols), ABLATED_FEATURE, len(all_features),
    )

    splits, cat_levels = align_categoricals(splits, cat_cols)

    X = {k: v[features] for k, v in splits.items()}
    y = {k: v[label].astype(int) for k, v in splits.items()}
    for k in ("train", "val", "test"):
        log.info("%-6s rows=%6d  positive_rate=%.4f", k, len(X[k]), y[k].mean())

    # ---------------- 1. datasets
    dtrain = lgb.Dataset(
        X["train"], y["train"], categorical_feature=cat_cols, free_raw_data=False,
        params={"feature_pre_filter": False},
    )
    dval = lgb.Dataset(
        X["val"], y["val"], categorical_feature=cat_cols, reference=dtrain,
        free_raw_data=False, params={"feature_pre_filter": False},
    )

    # ---------------- 2. Optuna
    print("\n" + "=" * 66)
    print(f"  OPTUNA TUNING ({n_trials} trials, val only)")
    print("=" * 66)

    def objective(trial: optuna.Trial) -> float:
        params = {
            "objective": "binary", "metric": "auc", "verbosity": -1,
            "seed": RANDOM_SEED, "num_threads": -1, "feature_pre_filter": False,
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "num_leaves": trial.suggest_int("num_leaves", 16, 256, log=True),
            "max_depth": trial.suggest_int("max_depth", 3, 12),
            "min_data_in_leaf": trial.suggest_int("min_data_in_leaf", 10, 200),
            "feature_fraction": trial.suggest_float("feature_fraction", 0.5, 1.0),
            "bagging_fraction": trial.suggest_float("bagging_fraction", 0.5, 1.0),
            "bagging_freq": trial.suggest_int("bagging_freq", 1, 7),
            "lambda_l1": trial.suggest_float("lambda_l1", 1e-8, 10.0, log=True),
            "lambda_l2": trial.suggest_float("lambda_l2", 1e-8, 10.0, log=True),
            "min_gain_to_split": trial.suggest_float("min_gain_to_split", 0.0, 1.0),
        }
        booster = lgb.train(
            params, dtrain, num_boost_round=2000, valid_sets=[dval],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        pred = booster.predict(X["val"], num_iteration=booster.best_iteration)
        return roc_auc_score(y["val"], pred)

    def progress(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if (trial.number + 1) % 10 == 0 or trial.number == 0:
            log.info(
                "  trial %3d/%d  best_val_auc=%.4f",
                trial.number + 1, n_trials, study.best_value,
            )

    study = optuna.create_study(
        direction="maximize", sampler=optuna.samplers.TPESampler(seed=RANDOM_SEED)
    )
    study.optimize(objective, n_trials=n_trials, callbacks=[progress], show_progress_bar=False)
    log.info("Best val ROC-AUC: %.4f", study.best_value)

    # ---------------- 3. final model
    best_params = {
        "objective": "binary", "metric": "auc", "verbosity": -1,
        "seed": RANDOM_SEED, "num_threads": -1, "feature_pre_filter": False,
        **study.best_params,
    }
    final = lgb.train(
        best_params, dtrain, num_boost_round=2000, valid_sets=[dval],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    best_iter = int(final.best_iteration or final.current_iteration())

    print("\n" + "=" * 66)
    print("  FINAL EVALUATION (ABLATION)")
    print("=" * 66)
    p_val = final.predict(X["val"], num_iteration=best_iter)
    p_test = final.predict(X["test"], num_iteration=best_iter)
    m_final_val = evaluate(y["val"], p_val, "ablation tuned lgbm (VAL)")
    m_final_test = evaluate(y["test"], p_test, "ablation tuned lgbm (TEST)")

    # ---------------- 4. compare with production
    prod_meta = json.loads((MODEL_DIR / "metadata.json").read_text(encoding="utf-8"))
    prod_roc, prod_pr, prod_key = _extract_prod_metrics(prod_meta)

    abl_roc = m_final_test["roc_auc"]
    abl_pr = m_final_test["pr_auc"]
    auc_drop = prod_roc - abl_roc

    # ---------------- 5. save experiment metadata
    metadata = {
        "model_name": "delay_classifier_ablation_no_shipping_mode",
        "experiment_id": "C1",
        "ablated_feature": ABLATED_FEATURE,
        "trained_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha(),
        "random_seed": RANDOM_SEED,
        "optuna_trials": n_trials,
        "training_runtime_sec": round(time.time() - t0, 1),
        "best_iteration": best_iter,
        "data": {
            "n_train": int(len(X["train"])),
            "n_val": int(len(X["val"])),
            "n_test": int(len(X["test"])),
            "positive_rate_test": round(float(y["test"].mean()), 4),
        },
        "features": {
            "n_features": len(features),
            "n_features_production": len(all_features),
            "feature_names": features,
            "categorical": cat_cols,
            "numeric": num_cols,
            "categorical_levels": cat_levels,
        },
        "best_params": study.best_params,
        "metrics": {
            "ablation_val": m_final_val,
            "ablation_test": m_final_test,
        },
        "comparison": {
            "production_metrics_key": prod_key,
            "production_test_roc_auc": round(prod_roc, 4),
            "ablation_test_roc_auc": round(abl_roc, 4),
            "roc_auc_drop": round(auc_drop, 4),
            "production_test_pr_auc": round(prod_pr, 4) if prod_pr is not None else None,
            "ablation_test_pr_auc": round(abl_pr, 4),
        },
        "note": (
            "Isolated ablation experiment (C1). Does not modify the production "
            "delay classifier. No model.joblib is written."
        ),
    }
    (EXPERIMENT_DIR / "metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    log.info("Experiment metadata → %s", EXPERIMENT_DIR / "metadata.json")

    # ---------------- 6. summary
    print("\n" + "=" * 66)
    print("  C1 ABLATION SUMMARY — shipping_mode")
    print("=" * 66)
    print(f"  Production features:            {len(all_features)}")
    print(f"  Ablation features:              {len(features)}")
    print("-" * 66)
    print(f"  Full-model test ROC-AUC:        {prod_roc:.4f}   (key: {prod_key})")
    print(f"  No-shipping_mode test ROC-AUC:  {abl_roc:.4f}")
    print(f"  ROC-AUC drop:                   {auc_drop:+.4f}")
    if prod_pr is not None:
        print("-" * 66)
        print(f"  Full-model test PR-AUC:         {prod_pr:.4f}")
        print(f"  No-shipping_mode test PR-AUC:   {abl_pr:.4f}")
        print(f"  PR-AUC drop:                    {prod_pr - abl_pr:+.4f}")
    print("-" * 66)

    if auc_drop > 0.05:
        verdict = (
            "STRONG DEPENDENCE — shipping_mode carries substantial, non-redundant "
            "signal. The classifier is legitimately mode-driven."
        )
    elif auc_drop > 0.02:
        verdict = (
            "MODERATE DEPENDENCE — shipping_mode matters, but remaining features "
            "recover most of the signal."
        )
    elif auc_drop > 0.0:
        verdict = (
            "WEAK DEPENDENCE — shipping_mode is largely redundant with other "
            "features despite high gain share."
        )
    else:
        verdict = (
            "NO DEPENDENCE — removing shipping_mode did not hurt (or helped). "
            "High gain share reflects split preference, not unique information."
        )
    print(f"  Interpretation: {verdict}")
    print("=" * 66 + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="C1 ablation: delay classifier without shipping_mode."
    )
    ap.add_argument("--trials", type=int, default=50, help="Optuna trials (default: 50)")
    args = ap.parse_args()
    main(args.trials)
