"""Re-export enums from nexafreight.enums for backward compatibility."""

from __future__ import annotations

from enum import StrEnum

from nexafreight.enums import (
    AlertSeverity,
    AlertStatus,
    CargoClass,
    DecisionAction,
    DisruptionStatus,
    DisruptionType,
    LegStatus,
    LocationType,
    OrderSlaStatus,
    Provenance,
    ShipmentStatus,
    TransportMode,
    UserRole,
)


class AssetType(StrEnum):
    VESSEL = "VESSEL"
    AIRCRAFT = "AIRCRAFT"
    TRUCK = "TRUCK"


# Aliases for compatibility
ShippingMode = TransportMode
LegMode = TransportMode

__all__ = [
    "AlertSeverity",
    "AlertStatus",
    "AssetType",
    "CargoClass",
    "DecisionAction",
    "DisruptionStatus",
    "DisruptionType",
    "LegMode",
    "LegStatus",
    "LocationType",
    "OrderSlaStatus",
    "Provenance",
    "ShipmentStatus",
    "ShippingMode",
    "TransportMode",
    "UserRole",
]
