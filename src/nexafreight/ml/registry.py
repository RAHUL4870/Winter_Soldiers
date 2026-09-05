"""
registry.py — ML Model Registry for NexaFreight (T-039).

Loads all three frozen model artifacts at application startup, validates
schema versions, and exposes typed accessors.  Singleton per process;
thread-safe for read-only inference — no hot-reload.

Public API
----------
ModelRegistry          Initialise with models root, validates & loads all 3.
  .get_delay_model()   → (booster, metadata_dict)
  .get_eta_model()     → EtaQuantileModel (ready to predict)
  .get_demand_model()  → (DemandForecastModel, forecasts_cache dict)
  .delay_metadata      → dict
  .eta_metadata        → dict
  .demand_metadata     → dict
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import joblib

from nexafreight.ml.constants import (
    DELAY_MODEL_DIR,
    DEMAND_MODEL_DIR,
    ETA_MODEL_DIR,
)
from nexafreight.ml.demand_forecast import DemandForecastModel
from nexafreight.ml.eta_model import EtaQuantileModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Expected schema version for v1 artifact compatibility
# ---------------------------------------------------------------------------
EXPECTED_SCHEMA_VERSION = "1.0.0"


class ModelRegistry:
    """Loads and validates all three frozen ML artifacts at startup.

    Parameters
    ----------
    delay_dir : str | Path
        Path to ``models/delay_classifier/``.
    eta_dir : str | Path
        Path to ``models/eta_quantile/``.
    demand_dir : str | Path
        Path to ``models/demand_forecast/``.

    Raises
    ------
    RuntimeError
        If any artifact is missing, or schema_version != EXPECTED_SCHEMA_VERSION.
    """

    def __init__(
        self,
        delay_dir: str | Path = DELAY_MODEL_DIR,
        eta_dir: str | Path = ETA_MODEL_DIR,
        demand_dir: str | Path = DEMAND_MODEL_DIR,
    ) -> None:
        self._delay_dir = Path(delay_dir)
        self._eta_dir = Path(eta_dir)
        self._demand_dir = Path(demand_dir)

        # Loaded artefacts (populated by _load_all)
        self._delay_booster: Any = None
        self._delay_metadata: dict[str, Any] = {}
        self._delay_feature_schema: dict[str, Any] = {}

        self._eta_model: EtaQuantileModel | None = None
        self._eta_metadata: dict[str, Any] = {}
        self._eta_feature_schema: dict[str, Any] = {}

        self._demand_model: DemandForecastModel | None = None
        self._demand_metadata: dict[str, Any] = {}

        self._is_loaded: bool = False

        # Eagerly load everything on construction
        self._load_all()

    # ------------------------------------------------------------------
    # Internal loaders
    # ------------------------------------------------------------------
    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        """Read and parse a JSON file, raising RuntimeError on failure."""
        if not path.exists():
            raise RuntimeError(f"Required artifact missing: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _validate_schema_version(
        source: dict[str, Any],
        label: str,
    ) -> None:
        """Ensure schema_version matches EXPECTED_SCHEMA_VERSION."""
        version = source.get("schema_version")
        if version is None:
            raise RuntimeError(
                f"{label}: missing 'schema_version' in artifact"
            )
        if version != EXPECTED_SCHEMA_VERSION:
            raise RuntimeError(
                f"{label}: schema_version={version!r}, "
                f"expected {EXPECTED_SCHEMA_VERSION!r}"
            )

    def _load_delay(self) -> None:
        """Load delay classifier booster dict from joblib + metadata."""
        model_path = self._delay_dir / "model.joblib"
        if not model_path.exists():
            raise RuntimeError(
                f"Delay classifier artifact missing: {model_path}"
            )

        self._delay_metadata = self._read_json(
            self._delay_dir / "metadata.json"
        )
        self._delay_feature_schema = self._read_json(
            self._delay_dir / "feature_schema.json"
        )

        # schema_version lives in metadata.json for the delay model
        self._validate_schema_version(
            self._delay_metadata, "delay_classifier"
        )

        self._delay_booster = joblib.load(model_path)
        logger.info(
            "Delay classifier loaded (version=%s)",
            self._delay_metadata.get("model_version"),
        )

    def _load_eta(self) -> None:
        """Load ETA quantile model via EtaQuantileModel.load()."""
        self._eta_metadata = self._read_json(
            self._eta_dir / "metadata.json"
        )
        self._eta_feature_schema = self._read_json(
            self._eta_dir / "feature_schema.json"
        )

        # schema_version lives in feature_schema.json for ETA
        self._validate_schema_version(
            self._eta_feature_schema, "eta_quantile"
        )

        self._eta_model = EtaQuantileModel()
        self._eta_model.load(self._eta_dir)
        logger.info(
            "ETA quantile model loaded (version=%s)",
            self._eta_metadata.get("model_version"),
        )

    def _load_demand(self) -> None:
        """Load demand forecast model via DemandForecastModel.load()."""
        self._demand_metadata = self._read_json(
            self._demand_dir / "metadata.json"
        )

        # schema_version lives in metadata.json for demand
        self._validate_schema_version(
            self._demand_metadata, "demand_forecast"
        )

        self._demand_model = DemandForecastModel()
        self._demand_model.load(self._demand_dir)
        logger.info(
            "Demand forecast model loaded (version=%s, lanes=%d)",
            self._demand_metadata.get("model_version"),
            len(self._demand_model.available_lanes),
        )

    def _load_all(self) -> None:
        """Load all three models. Fail fast on first error."""
        self._load_delay()
        self._load_eta()
        self._load_demand()
        self._is_loaded = True
        logger.info("ModelRegistry: all 3 models loaded and validated.")

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------
    def get_delay_model(self) -> Any:
        """Return the delay classifier booster (LightGBM Booster).

        The caller should invoke ``booster.predict(X)`` where X is a
        pandas DataFrame with the 14 feature columns.
        """
        if not self._is_loaded:
            raise RuntimeError("ModelRegistry not loaded")
        return self._delay_booster

    def get_eta_model(self) -> EtaQuantileModel:
        """Return the loaded EtaQuantileModel instance."""
        if not self._is_loaded or self._eta_model is None:
            raise RuntimeError("ModelRegistry not loaded")
        return self._eta_model

    def get_demand_model(self) -> DemandForecastModel:
        """Return the loaded DemandForecastModel instance."""
        if not self._is_loaded or self._demand_model is None:
            raise RuntimeError("ModelRegistry not loaded")
        return self._demand_model

    # ------------------------------------------------------------------
    # Metadata properties
    # ------------------------------------------------------------------
    @property
    def delay_metadata(self) -> dict[str, Any]:
        """Delay classifier metadata.json contents."""
        return self._delay_metadata

    @property
    def eta_metadata(self) -> dict[str, Any]:
        """ETA quantile metadata.json contents."""
        return self._eta_metadata

    @property
    def demand_metadata(self) -> dict[str, Any]:
        """Demand forecast metadata.json contents."""
        return self._demand_metadata

    @property
    def delay_schema_version(self) -> str:
        return self._delay_metadata.get("schema_version", "unknown")

    @property
    def eta_schema_version(self) -> str:
        return self._eta_feature_schema.get("schema_version", "unknown")

    @property
    def demand_schema_version(self) -> str:
        return self._demand_metadata.get("schema_version", "unknown")

    @property
    def delay_model_version(self) -> str:
        return self._delay_metadata.get("model_version", "unknown")

    @property
    def eta_model_version(self) -> str:
        return self._eta_metadata.get("model_version", "unknown")

    @property
    def demand_model_version(self) -> str:
        return self._demand_metadata.get("model_version", "unknown")
