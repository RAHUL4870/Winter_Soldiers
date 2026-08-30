"""Authentication endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.auth import create_access_token, verify_password
from nexafreight.config import Settings, get_settings
from nexafreight.database import get_db_session
from nexafreight.exceptions import AuthenticationError
from nexafreight.models.user import User
from nexafreight.schemas.auth import LoginRequest, LoginResponse, UserProfile

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/login", response_model=LoginResponse, tags=["authentication"])
async def login(
    credentials: LoginRequest,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """Authenticate user and issue JWT access token.

    Validates credentials against stored user record, ensuring:
    - User exists
    - Password matches stored hash
    - Account is active

    On success, returns JWT access token and basic profile information.
    On failure, returns generic error that does not reveal whether email
    exists or password was incorrect (prevents user enumeration).

    Args:
        credentials: Email and password
        db: Database session
        settings: Application settings

    Returns:
        LoginResponse with access token and user profile

    Raises:
        AuthenticationError: If credentials invalid or account inactive
    """
    # Load user by email
    result = await db.execute(select(User).where(User.email == credentials.email))
    user = result.scalar_one_or_none()

    # Generic error for non-existent user (avoid email enumeration)
    if not user:
        logger.info(f"Login attempt for non-existent email: {credentials.email}")
        raise AuthenticationError()

    # Verify password
    if not verify_password(credentials.password, user.hashed_password):
        logger.info(f"Login attempt with incorrect password: {credentials.email}")
        raise AuthenticationError()

    # Check account is active
    if not user.is_active:
        logger.info(f"Login attempt for inactive account: {credentials.email}")
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
