"""Live/replayed/simulated position tracking for legs."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from nexafreight.models.base import Base
from nexafreight.models.mixins import ProvenanceMixin

if TYPE_CHECKING:
    from nexafreight.models.leg import Leg


class PositionReport(Base, ProvenanceMixin):
    """High-volume position update for a leg.

    Provenance is MANDATORY (no default) — every position must declare its source.
    Composite index on (leg_id, timestamp DESC) for fast latest-position queries.
    Uses integer autoincrement PK (high-volume, internal reference only).
    No timestamp mixin (reported_at is the domain timestamp, not audit metadata).
    """

    __tablename__ = "position_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    leg_id: Mapped[int] = mapped_column(
        ForeignKey("legs.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Asset identification
    asset_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="vessel, truck, flight, etc."
    )
    mmsi: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="For vessel tracking")

    # Position data
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    heading: Mapped[float | None] = mapped_column(Float, nullable=True, comment="Degrees 0-360")
    heading_deg = synonym("heading")
    speed_knots: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Timestamp
    reported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    recorded_at = synonym("reported_at")

    # Relationships
    leg: Mapped[Leg] = relationship("Leg", back_populates="position_reports")

    __table_args__ = (
        UniqueConstraint("leg_id", "reported_at", name="uq_position_reports_leg_reported_at"),
        {"comment": "High-volume position tracking with mandatory provenance"},
    )
