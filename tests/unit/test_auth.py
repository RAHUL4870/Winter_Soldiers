"""Unit tests for authentication and password hashing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jose import JWTError, jwt

from nexafreight.auth import (
    TokenPayload,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from nexafreight.config import Settings
from nexafreight.enums import UserRole


@pytest.fixture
def test_settings() -> Settings:
    """Provide test settings with known values."""
    return Settings(
        jwt_secret="test-secret-key-that-is-sufficiently-long",
        jwt_algorithm="HS256",
        jwt_expiry_minutes=30,
        bcrypt_rounds=4,  # Low rounds for fast tests
    )


def test_hash_password_creates_valid_hash(test_settings: Settings) -> None:
    """Hashing produces a valid bcrypt hash."""
    password = "test_password_123"
    hashed = hash_password(password, test_settings)

    assert hashed is not None
    assert isinstance(hashed, str)
    assert hashed != password  # Not plaintext
    assert hashed.startswith("$2b$")  # Bcrypt format


def test_verify_password_succeeds_with_correct_password(test_settings: Settings) -> None:
    """Correct password verifies successfully against its hash."""
    password = "correct_password"
    hashed = hash_password(password, test_settings)

    assert verify_password(password, hashed) is True


def test_verify_password_fails_with_incorrect_password(test_settings: Settings) -> None:
    """Incorrect password fails verification."""
    correct_password = "correct_password"
    wrong_password = "wrong_password"
    hashed = hash_password(correct_password, test_settings)

    assert verify_password(wrong_password, hashed) is False


def test_create_access_token_contains_required_claims(test_settings: Settings) -> None:
    """Created JWT contains all required claims."""
    user_email = "test@example.com"
    user_role = UserRole.OPERATOR.value

    token = create_access_token(user_email, user_role, test_settings)

    # Decode without verification (just to inspect claims)
    payload = jwt.decode(
        token,
        test_settings.jwt_secret.get_secret_value(),
        algorithms=[test_settings.jwt_algorithm],
    )

    assert payload["sub"] == user_email
    assert payload["role"] == user_role
    assert "exp" in payload
    assert "iat" in payload


def test_decode_access_token_succeeds_with_valid_token(test_settings: Settings) -> None:
    """Valid token decodes successfully."""
    user_email = "test@example.com"
    user_role = UserRole.ADMIN.value

    token = create_access_token(user_email, user_role, test_settings)
    decoded = decode_access_token(token, test_settings)

    assert decoded.sub == user_email
    assert decoded.role == user_role
    assert decoded.exp > int(datetime.now(UTC).timestamp())


def test_decode_access_token_fails_with_tampered_signature(test_settings: Settings) -> None:
    """Token with tampered signature fails verification."""
    user_email = "test@example.com"
    user_role = UserRole.VIEWER.value

    token = create_access_token(user_email, user_role, test_settings)

    # Tamper with token signature
    parts = token.split(".")
    parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
    tampered_token = ".".join(parts)

    with pytest.raises(JWTError):
        decode_access_token(tampered_token, test_settings)


def test_decode_access_token_fails_with_expired_token(test_settings: Settings) -> None:
    """Expired token fails verification."""
    user_email = "test@example.com"
    user_role = UserRole.OPERATOR.value

    # Create token with past expiration
    now = datetime.now(UTC)
    past_exp = now - timedelta(hours=1)

    payload = TokenPayload(
        sub=user_email,
        role=user_role,
        exp=int(past_exp.timestamp()),
        iat=int((now - timedelta(hours=2)).timestamp()),
    )

    expired_token = jwt.encode(
        payload.model_dump(),
        test_settings.jwt_secret.get_secret_value(),
        algorithm=test_settings.jwt_algorithm,
    )

    with pytest.raises(JWTError):
        decode_access_token(expired_token, test_settings)


def test_decode_access_token_fails_with_missing_claim(test_settings: Settings) -> None:
    """Token missing required claim fails validation."""
    # Create token without 'role' claim
    now = datetime.now(UTC)
    payload_dict = {
        "sub": "test@example.com",
        # "role" intentionally missing
        "exp": int((now + timedelta(minutes=30)).timestamp()),
        "iat": int(now.timestamp()),
    }

    token = jwt.encode(
        payload_dict,
        test_settings.jwt_secret.get_secret_value(),
        algorithm=test_settings.jwt_algorithm,
    )

    with pytest.raises(ValueError, match="missing 'role' claim"):
        decode_access_token(token, test_settings)
