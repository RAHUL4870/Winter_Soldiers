"""Authentication endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.auth import create_access_token, decode_access_token, verify_password
from nexafreight.config import Settings, get_settings
from nexafreight.database import get_db_session
from nexafreight.dependencies import get_current_user
from nexafreight.exceptions import AuthenticationError
from nexafreight.models.user import User
from nexafreight.schemas.auth import (
    AccessTokenResponse,
    LoginResponse,
    RefreshRequest,
    UserOut,
    UserProfile,
)

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/login", response_model=LoginResponse, tags=["authentication"])
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """Authenticate user and issue JWT access token.

    Accepts application/x-www-form-urlencoded form data (OAuth2 standard).
    Fields: username (email), password.

    Validates credentials against stored user record, ensuring:
    - User exists
    - Password matches stored hash
    - Account is active

    On success, returns JWT access token and basic profile information.
    On failure, returns generic error that does not reveal whether email
    exists or password was incorrect (prevents user enumeration).
    """
    # Load user by email (OAuth2PasswordRequestForm uses 'username' field for email)
    result = await db.execute(select(User).where(User.email == form_data.username))
    user = result.scalar_one_or_none()

    # Generic error for non-existent user (avoid email enumeration)
    if not user:
        logger.info(f"Login attempt for non-existent email: {form_data.username}")
        raise AuthenticationError()

    # Verify password
    if not verify_password(form_data.password, user.hashed_password):
        logger.info(f"Login attempt with incorrect password: {form_data.username}")
        raise AuthenticationError()

    # Check account is active
    if not user.is_active:
        logger.info(f"Login attempt for inactive account: {form_data.username}")
        raise AuthenticationError("Account is inactive")

    # Create access token
    role_str = user.role.value if hasattr(user.role, "value") else str(user.role)
    access_token = create_access_token(
        user_email=user.email,
        user_role=role_str,
        settings=settings,
    )

    logger.info(f"Successful login: {user.email} (role={user.role})")

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        user=UserProfile(
            email=user.email,
            full_name=user.full_name,
            role=role_str,
            is_active=user.is_active,
        ),
        expires_in=settings.jwt_expiry_minutes * 60,  # Convert to seconds
    )


@router.post("/refresh", response_model=AccessTokenResponse, tags=["authentication"])
async def refresh_token(
    body: RefreshRequest,
    settings: Settings = Depends(get_settings),
) -> AccessTokenResponse:
    """Issue a new access token from a valid refresh token.

    Stateless implementation — validates the existing JWT's signature and
    expiry, then issues a fresh access token. No server-side token storage.

    Args:
        body: Contains the refresh_token (a previously issued JWT)
        settings: Application settings

    Returns:
        AccessTokenResponse with a new access token

    Raises:
        AuthenticationError: If token is invalid or expired
    """
    try:
        payload = decode_access_token(body.refresh_token, settings)
    except JWTError:
        raise AuthenticationError("Invalid or expired refresh token")

    new_token = create_access_token(
        user_email=payload.sub,
        user_role=payload.role,
        settings=settings,
    )
    return AccessTokenResponse(access_token=new_token, token_type="bearer")


@router.get("/me", response_model=UserOut, tags=["authentication"])
async def me(
    current_user: User = Depends(get_current_user),
) -> UserOut:
    """Return the authenticated user's profile.

    Uses existing get_current_user dependency to validate the JWT
    and load the user from the database.

    Returns:
        UserOut with id, email, role (and full_name)
    """
    return UserOut(
        id=current_user.id,
        email=current_user.email,
        role=current_user.role,
        full_name=current_user.full_name,
    )
