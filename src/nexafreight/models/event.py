"""Event tracking for shipments."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from nexafreight.models.shipment import Shipment


class Event(Base, TimestampMixin):
    """Event occurring during a shipment's lifecycle."""

    __tablename__ = "events"
    __table_args__ = ()

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shipment_id: Mapped[str] = mapped_column(
        ForeignKey("shipments.id", ondelete="CASCADE"),
        nullable=False,
    )
    leg_id: Mapped[int | None] = mapped_column(
        ForeignKey("legs.id", ondelete="SET NULL"),
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    location_locode: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    shipment: Mapped[Shipment] = relationship("Shipment", back_populates="events")
