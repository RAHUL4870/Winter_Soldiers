"""Integration tests for authentication API endpoints."""

from __future__ import annotations
from pydantic import SecretStr

import os
import subprocess
import sys
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from jose import jwt

from nexafreight.auth import TokenPayload, hash_password
from nexafreight.config import Settings, get_settings
from nexafreight.database import get_engine, get_session_factory
from nexafreight.dependencies import get_current_user, require_role
from nexafreight.enums import UserRole
from nexafreight.main import create_app
from nexafreight.models.user import User


@pytest.fixture
def test_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Settings, None, None]:
    """Provide test settings with isolated database."""
    db_path = tmp_path / "test_auth.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-for-integration-tests")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_EXPIRY_MINUTES", "30")
    monkeypatch.setenv("BCRYPT_ROUNDS", "4")
    monkeypatch.setenv("ENVIRONMENT", "test")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    settings = get_settings()
    yield settings

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def migrated_db(test_settings: Settings) -> None:
    """Run migrations on test database."""
    env = {**os.environ, "DATABASE_PATH": str(test_settings.database_path)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, f"Migration failed: {result.stderr}"


@pytest.fixture
async def seeded_user(test_settings: Settings, migrated_db: None) -> dict[str, str]:
    """Create a test user directly in the database.

    Returns:
        Dict with email, password, and role
    """
    user_data = {
        "email": "test@example.com",
        "password": "test_password_123",
        "full_name": "Test User",
        "role": UserRole.OPERATOR.value,
    }

    session_factory = get_session_factory()

    async with session_factory() as session:
        hashed_pw = hash_password(user_data["password"], test_settings)

        user = User(
            email=user_data["email"],
            hashed_password=hashed_pw,
            full_name=user_data["full_name"],
            role=UserRole(user_data["role"]),
            is_active=True,
        )
        session.add(user)
        await session.commit()

    return user_data


@pytest.mark.asyncio
async def test_login_success_with_correct_credentials(
    test_settings: Settings,
    seeded_user: dict[str, str],
) -> None:
    """User can log in with correct credentials and receive valid token."""
    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={
                "email": seeded_user["email"],
                "password": seeded_user["password"],
            },
        )

        assert response.status_code == 200
        data = response.json()

        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == seeded_user["email"]
        assert data["user"]["role"] == seeded_user["role"]
        assert "hashed_password" not in data["user"]


@pytest.mark.asyncio
async def test_login_fails_with_incorrect_password(
    test_settings: Settings,
    seeded_user: dict[str, str],
) -> None:
    """Login with incorrect password fails with generic error."""
    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={
                "email": seeded_user["email"],
                "password": "wrong_password",
            },
        )

        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "Invalid credentials"


@pytest.mark.asyncio
async def test_login_fails_with_nonexistent_email(
    test_settings: Settings,
    migrated_db: None,
) -> None:
    """Login with non-existent email fails with same generic error."""
    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={
                "email": "nonexistent@example.com",
                "password": "any_password",
            },
        )

        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "Invalid credentials"


@pytest.mark.asyncio
async def test_login_fails_with_inactive_account(
    test_settings: Settings,
    migrated_db: None,
) -> None:
    """Login with inactive account fails appropriately."""
    session_factory = get_session_factory()

    async with session_factory() as session:
        hashed_pw = hash_password("password123", test_settings)
        user = User(
            email="inactive@example.com",
            hashed_password=hashed_pw,
            full_name="Inactive User",
            role=UserRole.VIEWER,
            is_active=False,
        )
        session.add(user)
        await session.commit()

    app = create_app(test_settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={
                "email": "inactive@example.com",
                "password": "password123",
            },
        )

        assert response.status_code == 401
        data = response.json()
        assert "inactive" in data["error"].lower()


@pytest.mark.asyncio
async def test_protected_endpoint_allows_valid_token(
    test_settings: Settings,
    seeded_user: dict[str, str],
) -> None:
    """Protected endpoint allows access with valid token."""
    app = create_app(test_settings)

    test_router = APIRouter()

    @test_router.get("/protected")
    async def protected_endpoint(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"message": "success", "user_email": user.email}

    app.include_router(test_router, prefix="/test")

    with TestClient(app) as client:
        login_response = client.post(
            "/api/auth/login",
            json={
                "email": seeded_user["email"],
                "password": seeded_user["password"],
            },
        )
        token = login_response.json()["access_token"]

        response = client.get(
            "/test/protected",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "success"
        assert data["user_email"] == seeded_user["email"]


@pytest.mark.asyncio
async def test_protected_endpoint_rejects_missing_token(
    test_settings: Settings,
    migrated_db: None,
) -> None:
    """Protected endpoint rejects request with no token."""
    app = create_app(test_settings)

    test_router = APIRouter()

    @test_router.get("/protected")
    async def protected_endpoint(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"message": "success"}

    app.include_router(test_router, prefix="/test")

    with TestClient(app) as client:
        response = client.get("/test/protected")

        assert response.status_code == 401
        data = response.json()
        assert "authentication" in data["error"].lower()


@pytest.mark.asyncio
async def test_protected_endpoint_rejects_malformed_token(
    test_settings: Settings,
    migrated_db: None,
) -> None:
    """Protected endpoint rejects malformed token."""
    app = create_app(test_settings)

    test_router = APIRouter()

    @test_router.get("/protected")
    async def protected_endpoint(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"message": "success"}

    app.include_router(test_router, prefix="/test")

    with TestClient(app) as client:
        response = client.get(
            "/test/protected",
            headers={"Authorization": "Bearer not-a-valid-jwt-token"},
        )

        assert response.status_code == 401
        data = response.json()
        assert "invalid" in data["error"].lower()


@pytest.mark.asyncio
async def test_protected_endpoint_rejects_expired_token(
    test_settings: Settings,
    migrated_db: None,
) -> None:
    """Protected endpoint rejects expired token."""
    now = datetime.now(UTC)
    past_exp = now - timedelta(hours=1)

    payload = TokenPayload(
        sub="test@example.com",
        role=UserRole.OPERATOR.value,
        exp=int(past_exp.timestamp()),
        iat=int((now - timedelta(hours=2)).timestamp()),
    )

    expired_token = jwt.encode(
        payload.model_dump(),
        test_settings.jwt_secret.get_secret_value(),
        algorithm=test_settings.jwt_algorithm,
    )

    app = create_app(test_settings)

    test_router = APIRouter()

    @test_router.get("/protected")
    async def protected_endpoint(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"message": "success"}

    app.include_router(test_router, prefix="/test")

    with TestClient(app) as client:
        response = client.get(
            "/test/protected",
            headers={"Authorization": f"Bearer {expired_token}"},
        )

        assert response.status_code == 401


@pytest.mark.asyncio
async def test_role_based_access_control_allows_authorized_role(
    test_settings: Settings,
    seeded_user: dict[str, str],
) -> None:
    """Role-based dependency allows user with authorized role."""
    app = create_app(test_settings)

    test_router = APIRouter()

    @test_router.get("/operator-only")
    async def operator_endpoint(
        user: User = Depends(require_role(UserRole.OPERATOR, UserRole.ADMIN)),
    ) -> dict[str, str]:
        role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
        return {"message": "success", "role": role_val}

    app.include_router(test_router, prefix="/test")

    with TestClient(app) as client:
        login_response = client.post(
            "/api/auth/login",
            json={
                "email": seeded_user["email"],
                "password": seeded_user["password"],
            },
        )
        token = login_response.json()["access_token"]

        response = client.get(
            "/test/operator-only",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        assert response.json()["role"] == UserRole.OPERATOR.value


@pytest.mark.asyncio
async def test_role_based_access_control_rejects_unauthorized_role(
    test_settings: Settings,
    migrated_db: None,
) -> None:
    """Role-based dependency rejects user without required role."""
    session_factory = get_session_factory()

    async with session_factory() as session:
        hashed_pw = hash_password("viewer_pass", test_settings)
        user = User(
            email="viewer@example.com",
            hashed_password=hashed_pw,
            full_name="Viewer User",
            role=UserRole.VIEWER,
            is_active=True,
        )
        session.add(user)
        await session.commit()

    app = create_app(test_settings)

    test_router = APIRouter()

    @test_router.get("/admin-only")
    async def admin_endpoint(
        user: User = Depends(require_role(UserRole.ADMIN)),
    ) -> dict[str, str]:
        return {"message": "success"}

    app.include_router(test_router, prefix="/test")

    with TestClient(app) as client:
        login_response = client.post(
            "/api/auth/login",
            json={
                "email": "viewer@example.com",
                "password": "viewer_pass",
            },
        )
        token = login_response.json()["access_token"]

        response = client.get(
            "/test/admin-only",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 403
        data = response.json()
        assert "permission" in data["error"].lower() or "requires" in data["error"].lower()
