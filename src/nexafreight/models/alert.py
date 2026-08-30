"""Generated alert for operator attention."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.enums import AlertSeverity, AlertStatus
from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from nexafreight.models.decision import Decision
    from nexafreight.models.disruption import Disruption
    from nexafreight.models.shipment import Shipment
    from nexafreight.models.user import User


class Alert(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Operator-facing alert generated from a disruption.

    Uses UUID PK (exposed via API).
    Uniqueness constraint: only one alert per disruption (idempotency at schema level).
    """

    __tablename__ = "alerts"
    __table_args__ = (UniqueConstraint("disruption_id", name="uq_alerts_disruption_id"),)

    # Parent references
    disruption_id: Mapped[str] = mapped_column(
        ForeignKey("disruptions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    shipment_id: Mapped[str] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Alert metadata
    severity: Mapped[AlertSeverity] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[AlertStatus] = mapped_column(
        String(20), nullable=False, default=AlertStatus.OPEN
    )

    # Financial impact
    financial_exposure: Mapped[float] = mapped_column(
        Float,
        nullable=False,
        comment="Total potential financial loss (USD)",
    )
    sla_breach_details_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON-encoded per-order SLA breach details",
    )

    # Acknowledgment tracking
    acknowledged_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    disruption: Mapped[Disruption] = relationship("Disruption", back_populates="alert")
    shipment: Mapped[Shipment] = relationship("Shipment", back_populates="alerts")
    acknowledged_by_user: Mapped[User | None] = relationship("User")
    decision: Mapped[Decision | None] = relationship(
        "Decision", back_populates="alert", uselist=False
    )
