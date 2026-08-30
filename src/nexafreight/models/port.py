"""Port-specific master data and congestion tracking."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Date, Float, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from nexafreight.models.location import Location


class Port(Base, TimestampMixin):
    """Port-specific metadata linked one-to-one with a PORT-type Location.

    Uses integer autoincrement PK (internal reference).
    """

    __tablename__ = "ports"

    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )

    # Relationships
    location: Mapped[Location] = relationship("Location", back_populates="port")
    daily_stats: Mapped[list[PortDailyStat]] = relationship(
        "PortDailyStat",
        back_populates="port",
        cascade="all, delete-orphan",
    )


class PortDailyStat(Base):
    """Daily congestion index snapshot for a port.

    No timestamp mixin — stats are immutable once recorded.
    Uniqueness constraint prevents duplicate stats for same port+date.
    """

    __tablename__ = "port_daily_stats"
    __table_args__ = (
        UniqueConstraint("port_id", "stat_date", name="uq_port_daily_stats_port_id_stat_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    port_id: Mapped[int] = mapped_column(ForeignKey("ports.id", ondelete="CASCADE"), nullable=False)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    congestion_index: Mapped[float] = mapped_column(Float, nullable=False)

    # Relationships
    port: Mapped[Port] = relationship("Port", back_populates="daily_stats")
