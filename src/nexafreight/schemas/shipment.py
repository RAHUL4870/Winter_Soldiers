"""Pydantic schemas for shipment endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from nexafreight.enums import LegStatus, OrderSlaStatus, Provenance, ShipmentStatus, TransportMode


class ShipmentListItem(BaseModel):
    """One shipment in a paginated list response.

    Represents minimal shipment data needed for list views (T-024 frontend).
    Full detail with route/events comes from separate detail endpoint (T-022).
    """

    id: str = Field(..., description="Shipment UUID")
    origin: str = Field(..., description="Origin location UN/LOCODE")
    destination: str = Field(..., description="Destination location UN/LOCODE")
    mode: TransportMode = Field(..., description="Primary transport mode")
    status: ShipmentStatus = Field(..., description="Current shipment status")
    strictest_sla_deadline: datetime | None = Field(
        None, description="Tightest SLA deadline among all orders on this shipment"
    )

    revised_eta: datetime | None = Field(
        None,
        description=(
            "Current best estimate of arrival. PLACEHOLDER IMPLEMENTATION: "
            "Uses latest leg's planned_arrival as a naive estimate. "
            "ML-based ETA prediction (T-040/T-043) will replace this logic later."
        ),
    )


class ShipmentListFilters(BaseModel):
    """Query parameters for filtering shipment list."""

    status: ShipmentStatus | None = Field(None, description="Filter by exact status")
    mode: TransportMode | None = Field(None, description="Filter by transport mode")
    alert: bool | None = Field(None, description="If true, only shipments with active alerts")
    page: int = Field(1, ge=1, description="Page number (1-indexed)")
    size: int = Field(20, ge=1, le=100, description="Items per page (max 100)")


# ============================================================================
# T-022: Detail View Schemas
# ============================================================================


class VesselInfo(BaseModel):
    """Vessel information for a leg."""

    name: str = Field(..., description="Vessel name")
    mmsi: int = Field(..., description="Maritime Mobile Service Identity number")


class LegDetail(BaseModel):
    """Detailed information about one leg in a shipment's route.

    Includes all timing, routing, and provenance data for one segment.
    """

    id: int = Field(..., description="Leg database ID")
    sequence_number: int = Field(..., description="Order within shipment route (1-indexed)")
    mode: TransportMode = Field(..., description="Transport mode for this segment")
    status: LegStatus = Field(..., description="Current leg status")
    route_version: int = Field(..., description="Route version (increments on reroute)")

    origin: str = Field(..., description="Origin location UN/LOCODE")
    destination: str = Field(..., description="Destination location UN/LOCODE")

    vessel: VesselInfo | None = Field(None, description="Vessel info if applicable")
    flight_number: str | None = Field(None, description="Flight number if applicable")

    planned_departure: datetime = Field(..., description="Planned departure time")
    planned_arrival: datetime = Field(..., description="Planned arrival time")
    actual_departure: datetime | None = Field(
        None, description="Actual departure time (null if not departed)"
    )
    actual_arrival: datetime | None = Field(
        None, description="Actual arrival time (null if not arrived)"
    )

    distance_km: float | None = Field(None, description="Segment distance in kilometers")
    co2_kg: float | None = Field(None, description="Estimated CO2 emissions for this leg")

    provenance: Provenance = Field(
        ...,
        description=(
            "Data source provenance (REAL/REPLAYED/DERIVED/CALIBRATED/SIMULATED/MOCK). "
            "Mandatory on every leg per architecture invariant."
        ),
    )


class OrderSummary(BaseModel):
    """Summary of one order within a shipment (not full detail)."""

    id: int = Field(..., description="Order database ID")
    order_number: str = Field(..., description="Human-readable order reference")
    sla_deadline: datetime = Field(..., description="SLA delivery deadline")
    revenue: float = Field(..., description="Order revenue (USD)")
    sla_status: OrderSlaStatus = Field(..., description="Current SLA compliance status")


class ShipmentDetail(BaseModel):
    """Full detail view of a shipment with legs and orders.

    Returned by GET /api/shipments/{id}.
    """

    id: str = Field(..., description="Shipment UUID")
    origin: str = Field(..., description="Origin location UN/LOCODE")
    destination: str = Field(..., description="Destination location UN/LOCODE")
    mode: TransportMode = Field(..., description="Primary transport mode")
    cargo_class: str = Field(..., description="Cargo classification")
    status: ShipmentStatus = Field(..., description="Current shipment status")
    route_version: int = Field(..., description="Current route version")
    strictest_sla_deadline: datetime | None = Field(None, description="Tightest SLA among orders")
    container_count: int = Field(..., description="Number of containers")

    legs: list[LegDetail] = Field(..., description="Route segments (ordered by sequence)")
    orders: list[OrderSummary] = Field(..., description="Associated orders")

    # DESIGN DECISION: provenance omitted at shipment level, same rationale as T-021.
    # Shipment has no single provenance field. Would need to be derived from legs,
    # which is ambiguous when legs have mixed provenance. Each leg carries its own
    # provenance in the legs list above.


class RouteQuality(str):
    """Route quality classification derived from provenance.

    DESIGN DECISION: route_quality is a new derived concept introduced in T-022.
    It does not exist as a stored field in T-007 schema. Derivation logic:

    - "high": REAL or CALIBRATED provenance (actual data)
    - "medium": REPLAYED or DERIVED (historical/computed)
    - "low": SIMULATED or MOCK (synthetic)

    This provides a simple quality indicator for frontend map visualization
    without exposing raw provenance semantics to non-technical users.
    """

    pass


class RouteFeature(BaseModel):
    """One GeoJSON Feature in the route FeatureCollection."""

    type: str = Field(default="Feature", description="GeoJSON feature type")
    geometry: dict[str, Any] = Field(..., description="GeoJSON geometry (LineString expected)")
    properties: dict[str, Any] = Field(
        ..., description="Feature properties (mode, provenance, route_quality)"
    )


class RouteFeatureCollection(BaseModel):
    """GeoJSON FeatureCollection for shipment route visualization.

    Returned by GET /api/shipments/{id}/route.
    Each Feature represents one leg's geometry.
    """

    type: str = Field(default="FeatureCollection", description="GeoJSON type")
    features: list[RouteFeature] = Field(..., description="Array of route segment features")


class ShipmentEvent(BaseModel):
    """One event in a shipment's history.

    Sourced from AuditLog table (no dedicated events table exists in T-007 schema).
    Will be sparse until later tasks (T-044+ disruptions, alerts, decisions) begin
    writing real audit entries.
    """

    timestamp: datetime = Field(..., description="When the event occurred")
    event_type: str = Field(..., description="Event/action type (from AuditLog.action)")
    description: str = Field(..., description="Human-readable event description")
    actor: str | None = Field(None, description="Who/what triggered the event")
