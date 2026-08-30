"""Reusable ORM mixins for common column patterns."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from nexafreight.enums import Provenance


class TimestampMixin:
    """Automatic created_at and updated_at timestamp tracking.

    created_at is set once on insert.
    updated_at is refreshed automatically on every update via onupdate.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class UUIDPrimaryKeyMixin:
    """UUID string primary key for externally-referenceable entities.

    Used for entities exposed via API URLs where non-guessable IDs are preferred
    (Shipment, Alert, Decision, Disruption).
    """

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )


class ProvenanceMixin:
    """Mandatory provenance tracking for data authenticity.

    No default value — every entity using this mixin must explicitly declare its data source
    at creation time.
    """

    provenance: Mapped[Provenance] = mapped_column(
        String(20),
        nullable=False,
    )
