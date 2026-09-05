"""Physical container-level shipment journey (physical layer)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from nexafreight.enums import CargoClass, ShipmentStatus, TransportMode
from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from nexafreight.models.alert import Alert
    from nexafreight.models.decision import Decision
    from nexafreight.models.disruption import Disruption
    from nexafreight.models.leg import Leg
    from nexafreight.models.location import Location
    from nexafreight.models.order import Order
    from nexafreight.models.event import Event


class Shipment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Physical container journey from origin to destination.

    Physical/financial separation: Shipment owns route geometry, transport mode,
    and container count; Orders own SLA and revenue.
    Uses UUID PK (exposed in API URLs for tracking).
    route_version increments on reroute (zero-loss rerouting invariant).
    parent_shipment_id supports split-shipment scenarios.
    """

    __tablename__ = "shipments"

    # Origin and destination (references locations table)
    origin_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False
    )
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False
    )

    # Physical characteristics
    primary_transport_mode: Mapped[TransportMode] = mapped_column(String(20), nullable=False)
    primary_mode = synonym("primary_transport_mode")
    cargo_class: Mapped[CargoClass] = mapped_column(String(20), nullable=False)
    container_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Status and versioning
    status: Mapped[ShipmentStatus] = mapped_column(
        String(20), nullable=False, default=ShipmentStatus.PLANNED, index=True
    )
    route_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # Timing & SLA tracking (strictest deadline among child orders, updated by service logic)
    planned_departure: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    strictest_sla_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Split shipment support
    parent_shipment_id: Mapped[str | None] = mapped_column(
        ForeignKey("shipments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    origin: Mapped[Location] = relationship("Location", foreign_keys=[origin_id])
    destination: Mapped[Location] = relationship("Location", foreign_keys=[destination_id])
    parent_shipment: Mapped[Shipment | None] = relationship(
        "Shipment",
        remote_side="Shipment.id",
        back_populates="child_shipments",
    )
    child_shipments: Mapped[list[Shipment]] = relationship(
        "Shipment",
        back_populates="parent_shipment",
    )
    orders: Mapped[list[Order]] = relationship("Order", back_populates="shipment")
    legs: Mapped[list[Leg]] = relationship(
        "Leg",
        back_populates="shipment",
        # ORM-level cascade if shipment deleted (distinct from business rule
        # that legs are never individually deleted during rerouting)
        cascade="all, delete-orphan",
    )
    disruptions: Mapped[list[Disruption]] = relationship("Disruption", back_populates="shipment")
    alerts: Mapped[list[Alert]] = relationship("Alert", back_populates="shipment")
    decisions: Mapped[list[Decision]] = relationship("Decision", back_populates="shipment")
    events: Mapped[list[Event]] = relationship("Event", back_populates="shipment", cascade="all, delete-orphan")
