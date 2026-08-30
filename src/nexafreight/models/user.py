"""User authentication and authorization model."""

from __future__ import annotations

from sqlalchemy import Boolean, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from nexafreight.enums import UserRole
from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin


class User(Base, TimestampMixin):
    """Authentication identity and access control.

    Uses integer autoincrement PK (internal reference only, not exposed in API URLs).
    Password hashing logic implemented in T-010 auth module.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False, length=20, validate_strings=True),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
