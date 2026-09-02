"""
Script 10 — Train delay classifier (Phase 3, T-036).

Pipeline:
  1. Load train/val/test parquet + feature_schema.json
  2. Grouped historical baseline (the bar to beat)
  3. LightGBM default model
  4. Optuna tuning (val only — test never touched during tuning)
  5. Single final test evaluation
  6. SHAP top-5 global factors
  7. Save model.joblib + metadata.json

Usage:
    python scripts/10_train_delay_classifier.py --trials 50
    python scripts/10_train_delay_classifier.py --trials 5    # smoke test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import joblib
import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score

warnings.filterwarnings("ignore", category=UserWarning)
optuna.logging.set_verbosity(optuna.logging.WARNING)

log = logging.getLogger("nexafreight.ml.train_delay")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

RANDOM_SEED = 42


# ---------------------------------------------------------------- paths
def find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "pyproject.toml").exists():
            return p
    raise RuntimeError("repo root not found")


REPO_ROOT = find_repo_root(Path(__file__).resolve())
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
MODEL_DIR = REPO_ROOT / "models" / "delay_classifier"


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return "unknown"


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# ---------------------------------------------------------------- data
def load_splits() -> tuple[dict[str, pd.DataFrame], dict]:
    schema = json.loads((MODEL_DIR / "feature_schema.json").read_text(encoding="utf-8"))
    splits = {}
    for name in ("train", "val", "test"):
        path = PROCESSED_DIR / f"{name}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing — run scripts/09_build_training_data.py first")
        splits[name] = pd.read_parquet(path)
    return splits, schema


def align_categoricals(
    dfs: dict[str, pd.DataFrame], cat_cols: list[str]
) -> tuple[dict[str, pd.DataFrame], dict[str, list[str]]]:
    """Fix category levels from TRAIN only. Unseen levels in val/test -> NaN."""
    levels: dict[str, list[str]] = {}
    out = {k: v.copy() for k, v in dfs.items()}
    for col in cat_cols:
        cats = sorted(out["train"][col].dropna().astype(str).unique().tolist())
        levels[col] = cats
        for name in out:
            out[name][col] = pd.Categorical(out[name][col].astype(str), categories=cats)
    return out, levels


# ---------------------------------------------------------------- baseline
def grouped_baseline(
    train: pd.DataFrame,
    target: pd.DataFrame,
    label: str,
    group_cols: list[str],
) -> np.ndarray:
    """
    Hierarchical historical late-rate baseline.

    Predicts the historical late-delivery rate for matching groups, with
    progressively broader fallback groups and finally the global train rate.

    Example fallback order:
      order_country + shipping_mode + customer_country
      order_country + shipping_mode
      order_country
      global training late rate
    """
    global_rate = float(train[label].mean())
    predictions = np.full(len(target), global_rate, dtype=float)
    assigned = np.zeros(len(target), dtype=bool)

    def make_group_key(frame: pd.DataFrame, columns: list[str]) -> pd.Series:
        """Create a stable string key for one or more grouping columns."""
        return (
            frame[columns]
            .astype("string")
            .fillna("__MISSING__")
            .astype(str)
            .agg("||".join, axis=1)
        )

    # Start with the most specific group, then fall back to broader groups.
    for depth in range(len(group_cols), 0, -1):
        keys = group_cols[:depth]

        if not all(column in train.columns for column in keys):
            continue

        train_keys = make_group_key(train, keys)
        target_keys = make_group_key(target, keys)

        rates = (
            pd.DataFrame(
                {
                    "_group_key": train_keys,
                    "_label": train[label].astype(float),
                }
            )
            .groupby("_group_key", dropna=False)["_label"]
            .mean()
        )

        mapped = target_keys.map(rates).to_numpy(dtype=float)

        fill_mask = (~assigned) & (~np.isnan(mapped))
        predictions[fill_mask] = mapped[fill_mask]
        assigned |= fill_mask

    return predictions



# ---------------------------------------------------------------- metrics
def evaluate(y_true, y_score, tag: str) -> dict:
    m = {
        "roc_auc": float(roc_auc_score(y_true, y_score)),
        "pr_auc": float(average_precision_score(y_true, y_score)),
        "brier": float(brier_score_loss(y_true, np.clip(y_score, 0, 1))),
    }
    log.info("%-28s ROC-AUC=%.4f  PR-AUC=%.4f  Brier=%.4f", tag, m["roc_auc"], m["pr_auc"], m["brier"])
    return m


# ---------------------------------------------------------------- main
def main(n_trials: int) -> None:
    t0 = time.time()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    splits, schema = load_splits()
    label = schema["label_column"]
    features = [f for f in schema["feature_columns"] if f in splits["train"].columns]
    missing = set(schema["feature_columns"]) - set(features)
    if missing:
        log.warning("Schema features absent from parquet (skipped): %s", missing)

    cat_cols = [c for c in features if splits["train"][c].dtype == object]
    num_cols = [c for c in features if c not in cat_cols]
    log.info("Features: %d total (%d categorical, %d numeric)", len(features), len(cat_cols), len(num_cols))

    splits, cat_levels = align_categoricals(splits, cat_cols)

    X = {k: v[features] for k, v in splits.items()}
    y = {k: v[label].astype(int) for k, v in splits.items()}
    for k in X:
        log.info("%-6s rows=%6d  positive_rate=%.4f", k, len(X[k]), y[k].mean())

    # ---------------- 1. baselines
    print("\n" + "=" * 66)
    print("  BASELINES")
    print("=" * 66)

    const_val = np.full(len(y["val"]), y["train"].mean())
    m_const = evaluate(y["val"], const_val, "constant rate (val)")

    group_cols = [c for c in ["order_country", "shipping_mode", "customer_country"] if c in splits["train"].columns]
    log.info("Grouped baseline keys: %s", group_cols)
    base_val = grouped_baseline(splits["train"], splits["val"], label, group_cols)
    base_test = grouped_baseline(splits["train"], splits["test"], label, group_cols)
    m_base_val = evaluate(y["val"], base_val, "grouped baseline (val)")
    m_base_test = evaluate(y["test"], base_test, "grouped baseline (test)")

    # ---------------- 2. default LightGBM
    print("\n" + "=" * 66)
    print("  DEFAULT LIGHTGBM")
    print("=" * 66)

    dtrain = lgb.Dataset(
    X["train"], y["train"], categorical_feature=cat_cols, free_raw_data=False,
    params={"feature_pre_filter": False},
    )
    dval = lgb.Dataset(
        X["val"], y["val"], categorical_feature=cat_cols, reference=dtrain, free_raw_data=False,
        params={"feature_pre_filter": False},
    )


    default_params = {
        "objective": "binary", "metric": "auc", "verbosity": -1,
        "seed": RANDOM_SEED, "num_threads": -1,
    }
    m_default = lgb.train(
        default_params, dtrain, num_boost_round=1000, valid_sets=[dval],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    m_lgb_val = evaluate(y["val"], m_default.predict(X["val"]), "lgbm default (val)")

    # ---------------- 3. Optuna
    print("\n" + "=" * 66)
    print(f"  OPTUNA TUNING ({n_trials} trials, val only)")
    print("=" * 66)

    def objective(trial: optuna.Trial) -> float:
        params = {
            "objective": "binary", "metric": "auc", "verbosity": -1,
            "seed": RANDOM_SEED, "num_threads": -1,
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
        return roc_auc_score(y["val"], booster.predict(X["val"], num_iteration=booster.best_iteration))

    def progress(study: optuna.Study, trial: optuna.trial.FrozenTrial) -> None:
        if (trial.number + 1) % 10 == 0 or trial.number == 0:
            log.info("  trial %3d/%d  best_val_auc=%.4f", trial.number + 1, n_trials, study.best_value)

    study = optuna.create_study(
        direction="maximize", sampler=optuna.samplers.TPESampler(seed=RANDOM_SEED)
    )
    study.optimize(objective, n_trials=n_trials, callbacks=[progress], show_progress_bar=False)
    log.info("Best val ROC-AUC: %.4f", study.best_value)

    # ---------------- 4. final model
    best_params = {
        "objective": "binary", "metric": "auc", "verbosity": -1,
        "seed": RANDOM_SEED, "num_threads": -1,
        **study.best_params,
    }
    final = lgb.train(
        best_params, dtrain, num_boost_round=2000, valid_sets=[dval],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    best_iter = final.best_iteration

    print("\n" + "=" * 66)
    print("  FINAL EVALUATION")
    print("=" * 66)
    p_val = final.predict(X["val"], num_iteration=best_iter)
    p_test = final.predict(X["test"], num_iteration=best_iter)
    m_final_val = evaluate(y["val"], p_val, "tuned lgbm (val)")
    m_final_test = evaluate(y["test"], p_test, "tuned lgbm (TEST - once)")

    # ---------------- 5. SHAP
    print("\n" + "=" * 66)
    print("  SHAP — TOP GLOBAL FACTORS")
    print("=" * 66)
    top_features: list[dict] = []
    try:
        import shap

        sample = X["test"].sample(min(2000, len(X["test"])), random_state=RANDOM_SEED)
        explainer = shap.TreeExplainer(final)
        sv = explainer.shap_values(sample)
        if isinstance(sv, list):
            sv = sv[1]
        mean_abs = np.abs(sv).mean(axis=0)
        order = np.argsort(mean_abs)[::-1]
        for rank, i in enumerate(order[:10], 1):
            entry = {"rank": rank, "feature": features[i], "mean_abs_shap": float(mean_abs[i])}
            top_features.append(entry)
            print(f"  {rank:>2}. {features[i]:<32} {mean_abs[i]:.5f}")
    except Exception as exc:
        log.warning("SHAP failed (%s) — falling back to gain importance", exc)
        gains = final.feature_importance("gain")
        order = np.argsort(gains)[::-1]
        for rank, i in enumerate(order[:10], 1):
            entry = {"rank": rank, "feature": features[i], "gain": float(gains[i])}
            top_features.append(entry)
            print(f"  {rank:>2}. {features[i]:<32} {gains[i]:.1f}")

    # ---------------- 6. save
    artifact = {
        "model": final,
        "features": features,
        "categorical_features": cat_cols,
        "category_levels": cat_levels,
        "best_iteration": best_iter,
        "label_column": label,
    }
    joblib.dump(artifact, MODEL_DIR / "model.joblib")

    metadata = {
        "model_name": "delay_classifier",
        "model_version": "1.0.0",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "random_seed": RANDOM_SEED,
        "training_runtime_sec": round(time.time() - t0, 1),
        "data": {
            "train_hash": file_hash(PROCESSED_DIR / "train.parquet"),
            "n_train": int(len(X["train"])),
            "n_val": int(len(X["val"])),
            "n_test": int(len(X["test"])),
            "positive_rate_train": float(y["train"].mean()),
            "positive_rate_val": float(y["val"].mean()),
            "positive_rate_test": float(y["test"].mean()),
        },
        "features": {
            "n_features": len(features),
            "feature_names": features,
            "categorical": cat_cols,
            "numeric": num_cols,
        },
        "metrics": {
            "constant_baseline_val": m_const,
            "grouped_baseline_val": m_base_val,
            "grouped_baseline_test": m_base_test,
            "lgbm_default_val": m_lgb_val,
            "lgbm_tuned_val": m_final_val,
            "lgbm_tuned_test": m_final_test,
        },
        "optuna": {
            "n_trials": n_trials,
            "best_val_auc": float(study.best_value),
            "best_params": study.best_params,
            "best_iteration": int(best_iter),
        },
        "top_features": top_features,
        "notes": [
            "Tuning used validation set only; test evaluated exactly once.",
            "Port congestion features excluded (constant 1.0 — no 2015-18/2019-24 overlap).",
            "Model trained on train split only; refit on train+val is a future option.",
        ],
    }
    (MODEL_DIR / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    # ---------------- summary
    lift = m_final_test["roc_auc"] - m_base_test["roc_auc"]
    print("\n" + "=" * 66)
    print("  SUMMARY")
    print("=" * 66)
    print(f"  {'model':<28}{'val AUC':>12}{'test AUC':>12}")
    print(f"  {'-'*52}")
    print(f"  {'constant rate':<28}{m_const['roc_auc']:>12.4f}{'-':>12}")
    print(f"  {'grouped baseline':<28}{m_base_val['roc_auc']:>12.4f}{m_base_test['roc_auc']:>12.4f}")
    print(f"  {'lgbm default':<28}{m_lgb_val['roc_auc']:>12.4f}{'-':>12}")
    print(f"  {'lgbm tuned':<28}{m_final_val['roc_auc']:>12.4f}{m_final_test['roc_auc']:>12.4f}")
    print(f"  {'-'*52}")
    print(f"  lift over baseline (test): {lift:+.4f}")
    print(f"  runtime: {metadata['training_runtime_sec']}s")
    print("=" * 66)

    if m_final_test["roc_auc"] < m_base_test["roc_auc"]:
        print("\n  ⚠ FAIL: model does not beat the grouped baseline.")
    elif m_final_test["roc_auc"] > 0.90:
        print("\n  ⚠ WARNING: AUC > 0.90 on DataCo is suspicious — check for leakage.")
    else:
        print("\n  ✓ PASS: model beats baseline within a plausible range.")

    print(f"\n  saved: {MODEL_DIR / 'model.joblib'}")
    print(f"  saved: {MODEL_DIR / 'metadata.json'}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=50)
    args = ap.parse_args()
    main(args.trials)
