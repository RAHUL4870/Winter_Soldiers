"""Detected or manually reported disruption events."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.enums import DisruptionStatus, DisruptionType
from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from nexafreight.models.alert import Alert
    from nexafreight.models.leg import Leg
    from nexafreight.models.shipment import Shipment


class Disruption(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Detected or manually reported disruption event.

    Uses UUID PK (exposed via API).
    Uniqueness constraint enforces idempotency: only one ACTIVE disruption per (shipment, leg, type)
    combination at a time.
    """

    __tablename__ = "disruptions"
    __table_args__ = (
        UniqueConstraint(
            "shipment_id",
            "leg_id",
            "disruption_type",
            "status",
            name="uq_disruptions_active_per_shipment_leg_type",
            # Note: This constraint applies when status=ACTIVE; resolved disruptions
            # do not conflict. SQLite does not support partial indexes natively,
            # so application logic must ensure RESOLVED disruptions don't violate this.
        ),
    )

    # Parent references
    shipment_id: Mapped[str] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    leg_id: Mapped[int | None] = mapped_column(
        ForeignKey("legs.id", ondelete="SET NULL"),
        nullable=True,
        comment="Optional — some disruptions affect entire shipment, not a specific leg",
    )

    # Disruption metadata
    disruption_type: Mapped[DisruptionType] = mapped_column(String(50), nullable=False)
    status: Mapped[DisruptionStatus] = mapped_column(
        String(20), nullable=False, default=DisruptionStatus.ACTIVE
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Timing
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    shipment: Mapped[Shipment] = relationship("Shipment", back_populates="disruptions")
    leg: Mapped[Leg | None] = relationship("Leg", back_populates="disruptions")
    alert: Mapped[Alert | None] = relationship("Alert", back_populates="disruption", uselist=False)
