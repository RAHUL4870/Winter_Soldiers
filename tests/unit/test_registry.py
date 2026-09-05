"""
Unit tests for ModelRegistry (T-039).

Tests validate:
- All 3 models load successfully with schema_version pinned at 1.0.0
- Missing artifact → RuntimeError with clear message
- Missing metadata.json → RuntimeError
- Accessors return correct types
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from nexafreight.ml.constants import DELAY_MODEL_DIR, DEMAND_MODEL_DIR, ETA_MODEL_DIR


# ---------------------------------------------------------------------------
# Fixture: locate real model directories
# ---------------------------------------------------------------------------
@pytest.fixture()
def real_delay_dir() -> Path:
    return Path(DELAY_MODEL_DIR)


@pytest.fixture()
def real_eta_dir() -> Path:
    return Path(ETA_MODEL_DIR)


@pytest.fixture()
def real_demand_dir() -> Path:
    return Path(DEMAND_MODEL_DIR)


# ---------------------------------------------------------------------------
# Happy-path: registry loads successfully
# ---------------------------------------------------------------------------
class TestRegistryLoadsSuccessfully:
    """Full integration against real frozen artifacts on disk."""

    def test_all_models_load(
        self,
        real_delay_dir: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """Registry constructor should load all 3 models without errors."""
        from nexafreight.ml.registry import ModelRegistry

        registry = ModelRegistry(
            delay_dir=real_delay_dir,
            eta_dir=real_eta_dir,
            demand_dir=real_demand_dir,
        )
        assert registry._is_loaded is True

    def test_schema_version_pinned(
        self,
        real_delay_dir: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """All schema versions should be 1.0.0."""
        from nexafreight.ml.registry import ModelRegistry

        registry = ModelRegistry(
            delay_dir=real_delay_dir,
            eta_dir=real_eta_dir,
            demand_dir=real_demand_dir,
        )
        assert registry.delay_schema_version == "1.0.0"
        assert registry.eta_schema_version == "1.0.0"
        assert registry.demand_schema_version == "1.0.0"

    def test_model_version_pinned(
        self,
        real_delay_dir: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """All model versions should be 1.0.0."""
        from nexafreight.ml.registry import ModelRegistry

        registry = ModelRegistry(
            delay_dir=real_delay_dir,
            eta_dir=real_eta_dir,
            demand_dir=real_demand_dir,
        )
        assert registry.delay_model_version == "1.0.0"
        assert registry.eta_model_version == "1.0.0"
        assert registry.demand_model_version == "1.0.0"

    def test_delay_accessor_returns_callable(
        self,
        real_delay_dir: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """get_delay_model() should return an object with a .predict() method."""
        from nexafreight.ml.registry import ModelRegistry

        registry = ModelRegistry(
            delay_dir=real_delay_dir,
            eta_dir=real_eta_dir,
            demand_dir=real_demand_dir,
        )
        booster = registry.get_delay_model()
        # The delay model is a dict with a 'model' key, or a direct Booster
        model_obj = booster.get("model", booster) if isinstance(booster, dict) else booster
        assert hasattr(model_obj, "predict"), "Delay model must have .predict()"

    def test_eta_accessor_returns_eta_model(
        self,
        real_delay_dir: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """get_eta_model() should return an EtaQuantileModel instance."""
        from nexafreight.ml.eta_model import EtaQuantileModel
        from nexafreight.ml.registry import ModelRegistry

        registry = ModelRegistry(
            delay_dir=real_delay_dir,
            eta_dir=real_eta_dir,
            demand_dir=real_demand_dir,
        )
        assert isinstance(registry.get_eta_model(), EtaQuantileModel)

    def test_demand_accessor_returns_demand_model(
        self,
        real_delay_dir: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """get_demand_model() should return a DemandForecastModel."""
        from nexafreight.ml.demand_forecast import DemandForecastModel
        from nexafreight.ml.registry import ModelRegistry

        registry = ModelRegistry(
            delay_dir=real_delay_dir,
            eta_dir=real_eta_dir,
            demand_dir=real_demand_dir,
        )
        assert isinstance(registry.get_demand_model(), DemandForecastModel)

    def test_demand_model_has_lanes(
        self,
        real_delay_dir: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """Demand model should expose at least one lane."""
        from nexafreight.ml.registry import ModelRegistry

        registry = ModelRegistry(
            delay_dir=real_delay_dir,
            eta_dir=real_eta_dir,
            demand_dir=real_demand_dir,
        )
        dm = registry.get_demand_model()
        assert len(dm.available_lanes) > 0


# ---------------------------------------------------------------------------
# Failure cases: missing artifacts
# ---------------------------------------------------------------------------
class TestRegistryFailsOnMissingArtifacts:
    """Registry must fail fast with clear errors."""

    def test_missing_delay_model_file(
        self,
        tmp_path: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """Missing delay model.joblib → RuntimeError."""
        from nexafreight.ml.registry import ModelRegistry

        empty_delay = tmp_path / "delay_classifier"
        empty_delay.mkdir()
        # Create metadata but no model.joblib
        (empty_delay / "metadata.json").write_text('{"schema_version":"1.0.0"}')
        (empty_delay / "feature_schema.json").write_text('{}')

        with pytest.raises(RuntimeError, match="Delay classifier artifact missing"):
            ModelRegistry(
                delay_dir=empty_delay,
                eta_dir=real_eta_dir,
                demand_dir=real_demand_dir,
            )

    def test_missing_metadata_json(
        self,
        tmp_path: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """Missing metadata.json → RuntimeError."""
        from nexafreight.ml.registry import ModelRegistry

        empty_delay = tmp_path / "delay_classifier"
        empty_delay.mkdir()
        # No metadata.json at all

        with pytest.raises(RuntimeError, match="artifact missing"):
            ModelRegistry(
                delay_dir=empty_delay,
                eta_dir=real_eta_dir,
                demand_dir=real_demand_dir,
            )

    def test_wrong_schema_version(
        self,
        tmp_path: Path,
        real_delay_dir: Path,
        real_eta_dir: Path,
        real_demand_dir: Path,
    ) -> None:
        """Wrong schema_version → RuntimeError."""
        from nexafreight.ml.registry import ModelRegistry

        # Copy real delay dir and tamper with schema_version
        bad_delay = tmp_path / "delay_classifier"
        shutil.copytree(real_delay_dir, bad_delay)

        meta = json.loads((bad_delay / "metadata.json").read_text())
        meta["schema_version"] = "99.0.0"
        (bad_delay / "metadata.json").write_text(json.dumps(meta))

        with pytest.raises(RuntimeError, match="schema_version='99.0.0'"):
            ModelRegistry(
                delay_dir=bad_delay,
                eta_dir=real_eta_dir,
                demand_dir=real_demand_dir,
            )
