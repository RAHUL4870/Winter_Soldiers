"""Immutable append-only audit log for system and user actions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.models.base import Base

if TYPE_CHECKING:
    from nexafreight.models.user import User


class AuditLog(Base):
    """Append-only audit trail for actions, decisions, and LLM interactions.

    Deliberately does NOT use TimestampMixin — audit entries are immutable and never updated,
    so no updated_at column is needed.
    Uses integer autoincrement PK (high-volume, internal reference only).
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Timestamp (set once, never updated)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
    )

    # Actor identification
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="user, system, llm")
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="User ID if actor_type=user",
    )
    actor_name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Human-readable actor name"
    )

    # Action metadata
    action: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="login, approve_reroute, llm_query, etc."
    )
    entity_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="shipment, alert, decision, etc."
    )
    entity_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="ID of affected entity"
    )

    # LLM interaction tracking (optional)
    input_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="SHA256 of LLM input"
    )
    output_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="SHA256 of LLM output"
    )

    # Structured details (JSON-encoded)
    details_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON-encoded action-specific details",
    )

    # Relationships
    actor_user: Mapped[User | None] = relationship("User")
