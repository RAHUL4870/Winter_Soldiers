"""Reusable FastAPI dependencies for authentication and authorization."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, Query
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.auth import decode_access_token
from nexafreight.config import Settings, get_settings
from nexafreight.database import get_db_session
from nexafreight.enums import UserRole
from nexafreight.exceptions import (
    AuthenticationError,
    ForbiddenError,
    InvalidTokenError,
    TokenExpiredError,
)
from nexafreight.models.user import User

logger = logging.getLogger(__name__)


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> User:
    """Extract and validate JWT from Authorization header or token query parameter, return authenticated user.

    This dependency:
    1. Extracts Bearer token from Authorization header or ?token= query parameter (for SSE)
    2. Decodes and verifies JWT signature and expiration
    3. Loads user from database
    4. Confirms user is active
    """
    raw_token: str | None = None
    if authorization:
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise InvalidTokenError()
        raw_token = parts[1]
    elif token:
        raw_token = token
    else:
        raise AuthenticationError("Missing authentication token")


    # Decode and verify token
    try:
        payload = decode_access_token(raw_token, settings)
    except JWTError:
        raise InvalidTokenError() from None
    except ValueError as e:
        logger.warning(f"Token validation failed: {e}")
        raise InvalidTokenError() from e

    # Check expiration explicitly (jose should handle this, but be defensive)
    if payload.exp < int(datetime.now(UTC).timestamp()):
        raise TokenExpiredError()

    # Load user from database
    result = await db.execute(select(User).where(User.email == payload.sub))
    user = result.scalar_one_or_none()

    if not user:
        logger.warning(f"Token references non-existent user: {payload.sub}")
        raise AuthenticationError("User no longer exists")

    # Verify user is active
    if not user.is_active:
        raise AuthenticationError("User account is inactive")

    return user


def require_role(*allowed_roles: UserRole | str):
    """Dependency factory for role-based access control.

    Creates a dependency that enforces the authenticated user has
    one of the specified roles, building on get_current_user.

    Args:
        *allowed_roles: One or more UserRole or string values that are authorized

    Returns:
        FastAPI dependency function
    """
    allowed_values = [role.value if hasattr(role, "value") else str(role) for role in allowed_roles]

    async def role_checker(user: User = Depends(get_current_user)) -> User:
        user_role_val = user.role.value if hasattr(user.role, "value") else str(user.role)
        if user_role_val not in allowed_values:
            raise ForbiddenError(f"Requires one of: {', '.join(allowed_values)}")
        return user

    return role_checker
