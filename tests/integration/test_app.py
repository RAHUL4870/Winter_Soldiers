"""Integration tests for FastAPI application factory and lifespan."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from nexafreight.config import Settings
from nexafreight.exceptions import NexaFreightException
from nexafreight.main import create_app


def run_alembic_upgrade(db_path: Path) -> subprocess.CompletedProcess[str]:
    """Run alembic upgrade head against a specific database file."""
    env = {**os.environ, "DATABASE_PATH": str(db_path)}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Provide test settings with isolated database."""
    return Settings(
        jwt_secret="test-secret-key-for-integration-tests",
        environment="test",
        database_path=tmp_path / "test_app.db",
        allowed_origins=["http://localhost:3000", "http://localhost:5173"],
    )


def test_app_factory_creates_app_successfully(test_settings: Settings) -> None:
    """Application factory constructs FastAPI app without errors."""
    app = create_app(test_settings)

    assert app is not None
    assert app.title == "NexaFreight Control Tower API"
    assert app.version == "0.1.0"


def test_app_startup_succeeds_with_valid_database(test_settings: Settings) -> None:
    """Application starts cleanly with reachable database."""
    result = run_alembic_upgrade(test_settings.database_path)
    assert result.returncode == 0, f"Migration failed: {result.stderr}"

    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.get("/api/health/")
        assert response.status_code == 200


def test_health_check_returns_success(test_settings: Settings) -> None:
    """Health check endpoint reports healthy status."""
    run_alembic_upgrade(test_settings.database_path)

    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.get("/api/health/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "connected"
        assert data["version"] == "0.1.0"


def test_health_check_fails_with_unreachable_database() -> None:
    """Health check reports failure when database is unreachable."""
    bad_settings = Settings(
        jwt_secret="test-secret",
        environment="test",
        database_path=Path("./data/unreachable_test.db"),
    )

    app = create_app(bad_settings)

    with patch("nexafreight.main.create_engine") as mock_engine:
        mock_engine.side_effect = RuntimeError("Cannot connect to database: unreachable")

        with pytest.raises(RuntimeError, match="Cannot connect to database"):
            with TestClient(app):
                pass


def test_cors_headers_present_for_allowed_origin(test_settings: Settings) -> None:
    """CORS headers reflect configured allowed origins."""
    run_alembic_upgrade(test_settings.database_path)

    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.get(
            "/api/health/",
            headers={"Origin": "http://localhost:5173"},
        )
        assert response.status_code == 200
        assert "access-control-allow-origin" in response.headers
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_cors_headers_absent_for_disallowed_origin(test_settings: Settings) -> None:
    """CORS headers are restrictive for non-allowed origins."""
    run_alembic_upgrade(test_settings.database_path)

    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.get(
            "/api/health/",
            headers={"Origin": "http://evil.com"},
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") != "http://evil.com"


def test_custom_exception_handler_returns_clean_json(test_settings: Settings) -> None:
    """Custom application exceptions produce clean JSON responses."""
    run_alembic_upgrade(test_settings.database_path)

    app = create_app(test_settings)

    test_router = APIRouter()

    @test_router.get("/test-exception")
    async def trigger_custom_exception() -> None:
        raise NexaFreightException(
            message="Test error",
            status_code=400,
            details={"field": "test_field"},
        )

    app.include_router(test_router, prefix="/test")

    with TestClient(app) as client:
        response = client.get("/test/test-exception")
        assert response.status_code == 400
        data = response.json()
        assert data["error"] == "Test error"
        assert data["details"]["field"] == "test_field"


def test_generic_exception_handler_returns_safe_error(test_settings: Settings) -> None:
    """Unhandled exceptions produce safe, generic error responses."""
    run_alembic_upgrade(test_settings.database_path)

    app = create_app(test_settings)

    test_router = APIRouter()

    @test_router.get("/test-unhandled")
    async def trigger_unhandled_exception() -> None:
        raise ValueError("Internal implementation detail that should not leak")

    app.include_router(test_router, prefix="/test")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/test/test-unhandled")
        assert response.status_code == 500
        data = response.json()
        assert data["error"] == "Internal server error"
        assert "Internal implementation detail" not in str(data)


def test_lifespan_disposes_engine_on_shutdown(test_settings: Settings) -> None:
    """Lifespan correctly disposes engine during shutdown."""
    run_alembic_upgrade(test_settings.database_path)

    app = create_app(test_settings)

    with patch("nexafreight.main.dispose_engine", new_callable=AsyncMock) as mock_dispose:
        with TestClient(app):
            pass

        mock_dispose.assert_called_once()
