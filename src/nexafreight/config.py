"""Application configuration module using pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        frozen=True,  # immutable after creation
        env_file=".env",  # load from repo root .env
        env_file_encoding="utf-8",
        case_sensitive=False,  # ENV vars are case-insensitive
        extra="ignore",  # ignore unknown env vars (don't fail)
    )

    # --- Core App Settings ---
    environment: Environment = Field(default="development")
    log_level: LogLevel = Field(default="INFO")
    debug: bool = Field(default=False)

    # --- Database ---
    database_path: Path = Field(default=Path("./data/nexafreight.db"))
    test_database_path: Path = Field(default=Path(":memory:"))  # or ./data/test.db

    # --- JWT Authentication & Security ---
    jwt_secret: SecretStr = Field(...)  # required, no default
    jwt_algorithm: str = Field(default="HS256")
    jwt_expiry_minutes: int = Field(default=60)
    bcrypt_rounds: int = Field(default=12, ge=4, le=31)

    # --- CORS ---
    allowed_origins: list[str] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"],
        description="CORS allowed origins (frontend dev server)",
    )


    # --- External APIs (all optional/free-tier) ---
    # AIS Stream WebSocket & Replay
    enable_ais_listener: bool = Field(
        default=True,
        description="Enable/disable the background AIS listener worker",
    )
    enable_position_interpolator: bool = Field(
        default=True,
        description="Enable/disable the background position interpolator worker",
    )
    use_live_ais: bool = Field(
        default=True,
        description="Use live AIS (AISStreamAdapter) vs replay (ReplayFeedAdapter)",
    )

    ais_replay_data_path: str | None = Field(
        default=None,
        description="Path to Parquet files for AIS replay (used when use_live_ais=False)",
    )
    aisstream_api_key: SecretStr | None = Field(default=None)

    # Google Gemini (free tier)

    gemini_api_key: SecretStr | None = Field(default=None)
    gemini_model: str = Field(default="gemini-pro")

    # Ollama (local, no key)
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="llama2")

    # --- Computed Properties ---
    @property
    def database_url(self) -> str:
        """Async SQLite URL for main database."""
        if self.database_path == Path(":memory:"):
            return "sqlite+aiosqlite:///:memory:"
        posix = self.database_path.as_posix()
        if not posix.startswith("./") and not posix.startswith("/"):
            posix = f"./{posix}"
        return f"sqlite+aiosqlite:///{posix}"

    @property
    def test_database_url(self) -> str:
        """Async SQLite URL for test database."""
        if self.test_database_path == Path(":memory:"):
            return "sqlite+aiosqlite:///:memory:"
        posix = self.test_database_path.as_posix()
        if not posix.startswith("./") and not posix.startswith("/"):
            posix = f"./{posix}"
        return f"sqlite+aiosqlite:///{posix}"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    # --- Validators ---
    @field_validator("aisstream_api_key", "gemini_api_key", mode="before")
    @classmethod
    def empty_str_to_none(cls, v: Any) -> Any:
        if v == "" or (isinstance(v, SecretStr) and not v.get_secret_value()):
            return None
        return v

    @field_validator("database_path", "test_database_path", mode="before")
    @classmethod
    def ensure_path(cls, v: str | Path) -> Path:
        """Convert string to Path, create parent dirs if needed."""
        p = Path(v) if isinstance(v, str) else v
        if p != Path(":memory:") and not p.parent.exists():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        return p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached singleton settings instance.

    Use as FastAPI dependency:
        def my_route(settings: Settings = Depends(get_settings)): ...

    Or directly in scripts/workers:
        settings = get_settings()
    """
    return Settings()  # type: ignore[call-arg]


def ensure_directories() -> None:
    """Ensure required application directories exist.

    Creates data/ directory if it doesn't exist, so database file
    and other persistent state can be stored there.
    """
    settings = get_settings()
    if settings.database_path != Path(":memory:"):
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
