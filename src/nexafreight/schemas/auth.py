"""Pydantic schemas for authentication endpoints."""

from __future__ import annotations

import email_validator
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from nexafreight.enums import UserRole

email_validator.TEST_ENVIRONMENT = True
email_validator.SPECIAL_USE_DOMAIN_NAMES = []


class LoginRequest(BaseModel):
    """Login credentials submitted by client."""

    email: EmailStr = Field(..., description="User email address")
    password: str = Field(..., min_length=1, description="User password")


class UserProfile(BaseModel):
    """Safe user profile information returned after login.

    Never includes password hash or other sensitive internal fields.
    """

    model_config = ConfigDict(from_attributes=True)

    email: str
    full_name: str
    role: str
    is_active: bool


class LoginResponse(BaseModel):
    """Successful login response with access token."""

    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field(default="bearer", description="Token type (always 'bearer')")
    user: UserProfile = Field(..., description="Basic user profile")
    expires_in: int = Field(..., description="Token expiration time in seconds")


class TokenResponse(BaseModel):
    """Legacy token response for compatibility."""

    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"
    role: UserRole


class RefreshRequest(BaseModel):
    """Refresh token request."""

    refresh_token: str


class AccessTokenResponse(BaseModel):
    """Access token response."""

    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    """User profile output schema."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: UserRole
    full_name: str | None = None
