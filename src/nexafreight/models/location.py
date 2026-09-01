"""Geographic location master data (UN/LOCODE-based)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from nexafreight.enums import LocationType
from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from nexafreight.models.port import Port


class Location(Base, TimestampMixin):
    """Physical place (port, airport, warehouse, depot) identified by UN/LOCODE.

    Uses integer autoincrement PK (high-volume internal references from legs, shipments, orders).
    Populated by data ingestion tasks.
    """

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    locode: Mapped[str] = mapped_column(String(10), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    location_type: Mapped[LocationType] = mapped_column(String(20), nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    lat = synonym("latitude")
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    lon = synonym("longitude")

    # Relationships
    port: Mapped[Port | None] = relationship("Port", back_populates="location", uselist=False)
