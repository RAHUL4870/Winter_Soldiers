"""Complete ORM model registry for NexaFreight Control Tower.

All models must be imported here so Base.metadata includes every table for Alembic autogeneration.
"""

from __future__ import annotations

from nexafreight.models.alert import Alert
from nexafreight.models.audit import AuditLog
from nexafreight.models.base import Base
from nexafreight.models.corridor import CorridorAlternative
from nexafreight.models.decision import Decision
from nexafreight.models.disruption import Disruption
from nexafreight.models.leg import Leg
from nexafreight.models.location import Location
from nexafreight.models.order import Order, OrderItem
from nexafreight.models.port import Port, PortDailyStat
from nexafreight.models.position import PositionReport
from nexafreight.models.shipment import Shipment
from nexafreight.models.user import User
from nexafreight.models.vessel import Vessel

__all__ = [
    "Base",
    "User",
    "Location",
    "Port",
    "PortDailyStat",
    "Vessel",
    "Order",
    "OrderItem",
    "Shipment",
    "Leg",
    "PositionReport",
    "Disruption",
    "Alert",
    "CorridorAlternative",
    "Decision",
    "AuditLog",
]
