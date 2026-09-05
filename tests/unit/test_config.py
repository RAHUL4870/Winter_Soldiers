"""Tests for typed configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from nexafreight.config import Settings, get_settings


def test_settings_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings can be instantiated with minimal required fields."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    settings = Settings(jwt_secret=SecretStr("test-secret-key"))

    assert settings.environment == "development"
    assert settings.log_level == "INFO"
    assert settings.debug is False
    assert settings.jwt_algorithm == "HS256"
    assert settings.jwt_expiry_minutes == 60
    assert settings.gemini_model == "gemini-pro"
    assert settings.ollama_base_url == "http://localhost:11434"


def test_jwt_secret_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """JWT secret must be provided (no default)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError, match="jwt_secret"):
        Settings()  # type: ignore[call-arg]


def test_jwt_secret_is_secret_str() -> None:
    """JWT secret is stored as SecretStr and not logged in repr."""
    settings = Settings(jwt_secret=SecretStr("super-secret-value"))

    assert isinstance(settings.jwt_secret, SecretStr)
    assert settings.jwt_secret.get_secret_value() == "super-secret-value"
    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings)


def test_environment_validation() -> None:
    """Environment must be one of the allowed literal values."""
    # Valid
    Settings(jwt_secret=SecretStr("x"), environment="development")
    Settings(jwt_secret=SecretStr("x"), environment="test")
    Settings(jwt_secret=SecretStr("x"), environment="production")

    # Invalid
    with pytest.raises(ValidationError):
        Settings(jwt_secret=SecretStr("x"), environment="invalid")  # type: ignore[arg-type]


def test_database_url_computed() -> None:
    """database_url is computed from database_path."""
    settings = Settings(
        jwt_secret=SecretStr("x"),
        database_path=Path("./data/nexafreight.db"),
    )
    assert settings.database_url == "sqlite+aiosqlite:///./data/nexafreight.db"


def test_test_database_url_in_memory() -> None:
    """test_database_url defaults to in-memory."""
    settings = Settings(jwt_secret=SecretStr("x"))
    assert settings.test_database_url == "sqlite+aiosqlite:///:memory:"


def test_is_production_flag() -> None:
    """is_production property works correctly."""
    dev = Settings(jwt_secret=SecretStr("x"), environment="development")
    prod = Settings(jwt_secret=SecretStr("x"), environment="production")

    assert not dev.is_production
    assert prod.is_production


def test_settings_immutable() -> None:
    """Settings object is frozen (immutable)."""
    settings = Settings(jwt_secret=SecretStr("x"))

    with pytest.raises(ValidationError):  # pydantic 2.x raises ValidationError for frozen
        settings.environment = "production"  # type: ignore[misc]


def test_get_settings_singleton() -> None:
    """get_settings returns the same cached instance."""
    s1 = get_settings()
    s2 = get_settings()

    assert s1 is s2  # same object identity


def test_optional_api_keys_none_by_default() -> None:
    """Optional API keys (Gemini, AIS Stream) default to None."""
    settings = Settings(jwt_secret=SecretStr("x"))

    assert settings.gemini_api_key is None
    assert settings.aisstream_api_key is None


def test_optional_api_keys_can_be_set() -> None:
    """Optional API keys can be provided as SecretStr."""
    settings = Settings(
        jwt_secret=SecretStr("x"),
        gemini_api_key="fake-gemini-key",  # type: ignore[arg-type]
        aisstream_api_key="fake-ais-key",  # type: ignore[arg-type]
    )

    assert isinstance(settings.gemini_api_key, SecretStr)
    assert settings.gemini_api_key.get_secret_value() == "fake-gemini-key"
    assert isinstance(settings.aisstream_api_key, SecretStr)
    assert settings.aisstream_api_key.get_secret_value() == "fake-ais-key"


def test_settings_load_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings can load from .env file."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JWT_SECRET=from-env-file\nENVIRONMENT=production\nLOG_LEVEL=WARNING\n",
        encoding="utf-8",
    )

    # Point pydantic-settings to our test .env
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    settings = Settings()  # type: ignore[call-arg]

    assert settings.jwt_secret.get_secret_value() == "from-env-file"
    assert settings.environment == "production"
    assert settings.log_level == "WARNING"


def test_real_env_vars_override_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real environment variables take precedence over .env file."""
    env_file = tmp_path / ".env"
    env_file.write_text("JWT_SECRET=from-file\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JWT_SECRET", "from-real-env")

    settings = Settings()  # type: ignore[call-arg]

    assert settings.jwt_secret.get_secret_value() == "from-real-env"
