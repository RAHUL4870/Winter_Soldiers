"""Real-world vessel identity and metadata."""

from __future__ import annotations

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin


class Vessel(Base, TimestampMixin):
    """Real-world maritime vessel identified by MMSI.

    Uses integer autoincrement PK (internal reference).
    call_sign and typical_lanes_json were flagged as schema gaps in an earlier audit and
    are included from the start.
    """

    __tablename__ = "vessels"

    id: Mapped[int] = mapped_column(primary_key=True)
    mmsi: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    call_sign: Mapped[str | None] = mapped_column(String(20), nullable=True)
    typical_lanes_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON-encoded list of typical trade lanes for this vessel",
    )
