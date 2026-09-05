"""Options for rerouting shipments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from nexafreight.models.alert import Alert


class RerouteOption(Base, TimestampMixin):
    """A reroute option generated in response to an alert."""

    __tablename__ = "reroute_options"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[str] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    
    cost: Mapped[float] = mapped_column(Float, nullable=False)
    eta: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    alert: Mapped[Alert] = relationship("Alert", back_populates="reroute_options")
