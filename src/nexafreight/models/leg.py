"""Individual route segment within a shipment's journey."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.enums import LegStatus, TransportMode
from nexafreight.models.base import Base
from nexafreight.models.mixins import ProvenanceMixin, TimestampMixin

if TYPE_CHECKING:
    from nexafreight.models.disruption import Disruption
    from nexafreight.models.location import Location
    from nexafreight.models.position import PositionReport
    from nexafreight.models.shipment import Shipment
    from nexafreight.models.vessel import Vessel


class Leg(Base, TimestampMixin, ProvenanceMixin):
    """One segment of a shipment's multi-modal route.

    Zero-loss rerouting invariant: Legs are NEVER deleted. When rerouted, old legs are marked
    REPLACED and new legs are created with incremented route_version.
    Uses integer autoincrement PK (high-volume internal reference).
    Composite index on (shipment_id, sequence_number) for efficient ordered retrieval.
    """

    __tablename__ = "legs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Parent shipment reference (mandatory, non-nullable)
    shipment_id: Mapped[str] = mapped_column(
        ForeignKey("shipments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Sequence and versioning
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    route_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Transport metadata
    transport_mode: Mapped[TransportMode] = mapped_column(String(20), nullable=False)
    status: Mapped[LegStatus] = mapped_column(String(20), nullable=False, default=LegStatus.PLANNED)

    # Origin and destination
    origin_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False
    )
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False
    )

    # Asset references (optional — not all legs have vessel/flight tracking)
    vessel_id: Mapped[int | None] = mapped_column(
        ForeignKey("vessels.id", ondelete="SET NULL"), nullable=True
    )
    flight_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Timing
    planned_departure: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_arrival: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_departure: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_arrival: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Geometry and metrics
    route_geometry_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="GeoJSON LineString geometry (serialized as JSON text)",
    )
    distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)
    co2_kg: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relationships
    shipment: Mapped[Shipment] = relationship("Shipment", back_populates="legs")
    origin: Mapped[Location] = relationship("Location", foreign_keys=[origin_id])
    destination: Mapped[Location] = relationship("Location", foreign_keys=[destination_id])
    vessel: Mapped[Vessel | None] = relationship("Vessel")
    position_reports: Mapped[list[PositionReport]] = relationship(
        "PositionReport",
        back_populates="leg",
        cascade="all, delete-orphan",
    )
    disruptions: Mapped[list[Disruption]] = relationship("Disruption", back_populates="leg")

    __table_args__ = (
        {"comment": "Route segments with zero-loss rerouting (REPLACED status, never deleted)"},
    )
