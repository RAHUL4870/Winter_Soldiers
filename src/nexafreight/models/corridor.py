"""Reference data for rerouting corridor alternatives."""

from __future__ import annotations

from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexafreight.models.base import Base
from nexafreight.models.mixins import TimestampMixin


class CorridorAlternative(Base, TimestampMixin):
    """Pre-configured rerouting option for an origin-destination lane.

    Populated by data seeding tasks.
    Uses integer autoincrement PK (internal reference only, not exposed in API).
    """

    __tablename__ = "corridor_alternatives"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Identification
    option_key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Applicability
    applicable_disruption_types_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="JSON-encoded list of DisruptionType values this option applies to",
    )

    # Route template
    route_template_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="JSON-encoded template for constructing new legs if chosen",
    )

    # Delta factors (relative to baseline route)
    cost_delta_factor: Mapped[float] = mapped_column(
        Float, nullable=False, comment="Cost multiplier (e.g., 1.2 = +20%)"
    )
    time_delta_hours: Mapped[float] = mapped_column(
        Float, nullable=False, comment="Additional time (hours)"
    )
    co2_delta_factor: Mapped[float] = mapped_column(Float, nullable=False, comment="CO2 multiplier")
