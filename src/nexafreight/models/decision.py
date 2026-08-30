"""Immutable record of approved rerouting or accept-delay decision."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.enums import DecisionAction
from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from nexafreight.models.alert import Alert
    from nexafreight.models.shipment import Shipment
    from nexafreight.models.user import User


class Decision(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Immutable record of an operator's decision in response to an alert.

    Uses UUID PK (exposed via API).
    Uniqueness constraint: only one decision per alert (cannot approve twice).
    No updated_at in practice — decisions are never modified after creation.
    """

    __tablename__ = "decisions"
    __table_args__ = (UniqueConstraint("alert_id", name="uq_decisions_alert_id"),)

    # Parent references
    alert_id: Mapped[str] = mapped_column(
        ForeignKey("alerts.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    shipment_id: Mapped[str] = mapped_column(
        ForeignKey("shipments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Decision metadata
    action: Mapped[DecisionAction] = mapped_column(String(20), nullable=False)
    chosen_option_key: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="CorridorAlternative.option_key if action=REROUTE",
    )

    # Full snapshot of presented options at decision time (for audit/ML training)
    options_snapshot_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="JSON-encoded snapshot of all options presented",
    )

    # Financial impact
    financial_impact: Mapped[float] = mapped_column(
        Float, nullable=False, comment="Approved cost change (USD)"
    )

    # Versioning
    route_version_before: Mapped[int] = mapped_column(Integer, nullable=False)
    route_version_after: Mapped[int] = mapped_column(Integer, nullable=False)

    # Approval tracking
    approved_by: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    # Relationships
    alert: Mapped[Alert] = relationship("Alert", back_populates="decision")
    shipment: Mapped[Shipment] = relationship("Shipment", back_populates="decisions")
    approved_by_user: Mapped[User] = relationship("User")
