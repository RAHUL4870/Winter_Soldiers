"""Domain enums for NexaFreight Control Tower.

All application enums are centralized here to ensure consistency
across models, schemas, and service logic.
"""

from __future__ import annotations

from enum import StrEnum


class Provenance(StrEnum):
    """Source of truth for position/tracking data.

    - REAL: Live data from actual external sources (AIS, GPS, flight trackers)
    - REPLAYED: Historical data replayed for demonstration/testing
    - DERIVED: Calculated/interpolated from real or replayed data
    - CALIBRATED: Real data adjusted for known systematic errors
    - SIMULATED: Synthetic data from simulation engines
    - MOCK: Fake data for development/testing purposes
    """

    REAL = "REAL"
    REPLAYED = "REPLAYED"
    DERIVED = "DERIVED"
    CALIBRATED = "CALIBRATED"
    SIMULATED = "SIMULATED"
    MOCK = "MOCK"


class UserRole(StrEnum):
    """User access level and permissions.

    - ADMIN: Full system access, user management
    - OPERATOR: Can approve rerouting decisions, acknowledge alerts
    - VIEWER: Read-only access to dashboards and reports
    """

    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"


class TransportMode(StrEnum):
    """Mode of physical transport for a leg or shipment."""

    SEA = "SEA"
    AIR = "AIR"
    ROAD = "ROAD"
    RAIL = "RAIL"


class ShipmentStatus(StrEnum):
    """High-level status of a shipment's physical journey.

    PLANNED: Not yet departed from origin
    IN_TRANSIT: Currently moving through at least one active leg
    DELIVERED: Reached final destination
    DELAYED: Behind schedule but still moving
    CANCELLED: Shipment aborted
    """

    PLANNED = "PLANNED"
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    DELAYED = "DELAYED"
    CANCELLED = "CANCELLED"


class LegStatus(StrEnum):
    """Status of an individual route segment.

    PLANNED: Not yet started
    IN_PROGRESS: Currently active/moving
    COMPLETED: Finished successfully
    REPLACED: Superseded by a reroute (never deleted for zero-loss rerouting)
    CANCELLED: Leg aborted
    """

    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    REPLACED = "REPLACED"
    CANCELLED = "CANCELLED"


class CargoClass(StrEnum):
    """Cargo handling and risk classification."""

    STANDARD = "STANDARD"
    REFRIGERATED = "REFRIGERATED"
    HAZMAT = "HAZMAT"
    HIGH_VALUE = "HIGH_VALUE"


class LocationType(StrEnum):
    """Physical location category."""

    PORT = "PORT"
    AIRPORT = "AIRPORT"
    INLAND_DEPOT = "INLAND_DEPOT"
    WAREHOUSE = "WAREHOUSE"


class DisruptionType(StrEnum):
    """Category of disruption event."""

    VESSEL_DELAY = "VESSEL_DELAY"
    PORT_CONGESTION = "PORT_CONGESTION"
    WEATHER = "WEATHER"
    MANUAL = "MANUAL"


class DisruptionStatus(StrEnum):
    """Lifecycle state of a disruption."""

    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class AlertSeverity(StrEnum):
    """Priority level for operator attention."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AlertStatus(StrEnum):
    """Lifecycle state of an alert."""

    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class DecisionAction(StrEnum):
    """Type of decision made in response to an alert."""

    ACCEPT_DELAY = "ACCEPT_DELAY"
    REROUTE = "REROUTE"
    SPLIT_SHIPMENT = "SPLIT_SHIPMENT"


class OrderSlaStatus(StrEnum):
    """SLA compliance tracking for an order."""

    ON_TIME = "ON_TIME"
    AT_RISK = "AT_RISK"
    LATE = "LATE"
