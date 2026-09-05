"""Shipment management endpoints."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from nexafreight.database import get_db_session
from nexafreight.dependencies import get_current_user
from nexafreight.enums import ShipmentStatus, TransportMode
from nexafreight.models import Alert, AuditLog, Leg, Shipment, User
from nexafreight.models.event import Event
from nexafreight.enums import Provenance
from nexafreight.schemas.common import PaginatedResponse
from nexafreight.schemas.shipment import (
    LegDetail,
    OrderSummary,
    RouteFeature,
    RouteFeatureCollection,
    ShipmentDetail,
    ShipmentEvent,
    ShipmentListItem,
    VesselInfo,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ============================================================================
# T-021: List Endpoint
# ============================================================================


@router.get("", response_model=PaginatedResponse[ShipmentListItem])
@router.get("/", response_model=PaginatedResponse[ShipmentListItem])
async def list_shipments(
    status: ShipmentStatus | None = Query(None, description="Filter by status"),
    mode: TransportMode | None = Query(None, alias="mode", description="Filter by transport mode"),
    alert: bool | None = Query(None, description="Only shipments with active alerts"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> PaginatedResponse[ShipmentListItem]:
    """List shipments with optional filters and pagination.

    Returns a paginated list of shipments visible to the authenticated user.
    Supports filtering by status, transport mode, and presence of active alerts.

    Query Parameters:
        status: Filter to shipments with this exact status
        mode: Filter to shipments using this primary transport mode
        alert: If true, only shipments with at least one active alert
        page: Page number (1-indexed, default 1)
        size: Items per page (default 20, max 100)

    Returns:
        Paginated response with shipment list items and metadata

    Authentication:
        Requires any authenticated user (no specific role restriction for read-only list)

    Performance:
        - Uses composite index (status, primary_transport_mode) when both filters present
        - Eager-loads origin/destination locations to avoid N+1 queries
        - Alert filter uses EXISTS subquery to avoid row duplication
    """

    # Build base query with eager loading for locations (avoid N+1)
    query = select(Shipment).options(
        joinedload(Shipment.origin),
        joinedload(Shipment.destination),
    )

    # Apply filters (AND logic)
    filters = []
    if status is not None:
        filters.append(Shipment.status == status)
    if mode is not None:
        filters.append(Shipment.primary_transport_mode == mode)
    if alert is True:
        # EXISTS subquery: only shipments with at least one active alert
        # Avoids row duplication if shipment has multiple alerts
        alert_exists = exists(
            select(1)
            .select_from(Alert)
            .where(
                and_(
                    Alert.shipment_id == Shipment.id,
                    Alert.status.in_(["OPEN", "ACKNOWLEDGED"]),  # Active alert statuses
                )
            )
        )
        filters.append(alert_exists)

    if filters:
        query = query.where(and_(*filters))

    # Default ordering: most urgent SLA first (nulls last), then by created_at desc
    query = query.order_by(
        Shipment.strictest_sla_deadline.asc().nullslast(),
        Shipment.created_at.desc(),
    )

    # Get total count (before pagination)
    count_query = select(func.count()).select_from(Shipment)
    if filters:
        count_query = count_query.where(and_(*filters))
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Apply pagination
    offset = (page - 1) * size
    query = query.limit(size).offset(offset)

    # Execute paginated query
    result = await db.execute(query)
    shipments = result.scalars().unique().all()

    # Convert to response items
    items = []
    for shipment in shipments:
        # Get latest leg for naive ETA placeholder
        # PLACEHOLDER LOGIC: Uses planned_arrival from latest leg by sequence_number.
        # ML-based ETA prediction (T-040/T-043) will replace this later.
        leg_result = await db.execute(
            select(Leg.planned_arrival)
            .where(Leg.shipment_id == shipment.id)
            .order_by(Leg.sequence_number.desc())
            .limit(1)
        )
        latest_leg_eta = leg_result.scalar_one_or_none()

        items.append(
            ShipmentListItem(
                id=shipment.id,
                origin=shipment.origin.locode,  # Eager-loaded, no N+1
                destination=shipment.destination.locode,  # Eager-loaded, no N+1
                mode=shipment.primary_transport_mode,
                status=shipment.status,
                strictest_sla_deadline=shipment.strictest_sla_deadline,
                revised_eta=latest_leg_eta,  # Placeholder until T-040/T-043
            )
        )

    # Calculate total pages
    total_pages = (total + size - 1) // size if total > 0 else 0

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        size=size,
        total_pages=total_pages,
    )


# ============================================================================
# T-022: Detail Endpoints
# ============================================================================


def derive_route_quality(provenance: str) -> str:
    """Derive route quality classification from provenance.

    DESIGN DECISION: route_quality is a new derived field introduced in T-022.
    It does not exist in the T-007 database schema. Derivation logic:

    - "high": REAL or CALIBRATED (actual tracking data)
    - "medium": REPLAYED or DERIVED (historical/computed data)
    - "low": SIMULATED or MOCK (synthetic test data)

    Args:
        provenance: Provenance enum value as string

    Returns:
        Quality level: "high", "medium", or "low"
    """
    if provenance in ("REAL", "CALIBRATED"):
        return "high"
    elif provenance in ("REPLAYED", "DERIVED"):
        return "medium"
    else:  # SIMULATED, MOCK
        return "low"


@router.get("/{shipment_id}", response_model=ShipmentDetail)
async def get_shipment_detail(
    shipment_id: str = Path(..., description="Shipment UUID"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> ShipmentDetail:
    """Get full shipment detail including legs and orders.

    Loads complete shipment information with:
    - All route segments (legs) in sequence order
    - All associated orders with SLA status
    - Origin/destination location details

    Args:
        shipment_id: Shipment UUID
        db: Database session
        current_user: Authenticated user

    Returns:
        Full shipment detail with nested legs and orders

    Raises:
        HTTPException: 404 if shipment not found

    Performance:
        - Eager-loads legs, orders, and locations in single query (no N+1)
        - Legs ordered by sequence_number
    """
    # Load shipment with eager-loaded relationships
    query = (
        select(Shipment)
        .where(Shipment.id == shipment_id)
        .options(
            joinedload(Shipment.origin),
            joinedload(Shipment.destination),
            joinedload(Shipment.legs).joinedload(Leg.origin),
            joinedload(Shipment.legs).joinedload(Leg.destination),
            joinedload(Shipment.legs).joinedload(Leg.vessel),
            joinedload(Shipment.orders),
        )
    )

    result = await db.execute(query)
    shipment = result.unique().scalar_one_or_none()

    if not shipment:
        raise HTTPException(status_code=404, detail=f"Shipment {shipment_id} not found")

    # Convert legs to LegDetail schema (ordered by sequence)
    leg_details = []
    for leg in sorted(shipment.legs, key=lambda leg_item: leg_item.sequence_number):
        vessel_info = None
        if leg.vessel:
            vessel_info = VesselInfo(
                name=leg.vessel.name,
                mmsi=leg.vessel.mmsi,
            )

        leg_details.append(
            LegDetail(
                id=leg.id,
                sequence_number=leg.sequence_number,
                mode=leg.transport_mode,
                status=leg.status,
                route_version=leg.route_version,
                origin=leg.origin.locode,
                destination=leg.destination.locode,
                vessel=vessel_info,
                flight_number=leg.flight_number,
                planned_departure=leg.planned_departure,
                planned_arrival=leg.planned_arrival,
                actual_departure=leg.actual_departure,
                actual_arrival=leg.actual_arrival,
                distance_km=leg.distance_km,
                co2_kg=leg.co2_kg,
                provenance=leg.provenance,
            )
        )

    # Convert orders to OrderSummary schema
    order_summaries = [
        OrderSummary(
            id=order.id,
            order_number=order.order_number,
            sla_deadline=order.sla_deadline,
            revenue=order.revenue,
            sla_status=order.sla_status,
            provenance=Provenance.DERIVED,
        )
        for order in shipment.orders
    ]

    return ShipmentDetail(
        id=shipment.id,
        origin=shipment.origin.locode,
        destination=shipment.destination.locode,
        mode=shipment.primary_transport_mode,
        cargo_class=shipment.cargo_class,
        status=shipment.status,
        route_version=shipment.route_version,
        strictest_sla_deadline=shipment.strictest_sla_deadline,
        container_count=shipment.container_count,
        legs=leg_details,
        orders=order_summaries,
    )


@router.get("/{shipment_id}/route", response_model=RouteFeatureCollection)
async def get_shipment_route(
    shipment_id: str = Path(..., description="Shipment UUID"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> RouteFeatureCollection:
    """Get shipment route as GeoJSON FeatureCollection.

    Returns route geometry for map visualization. Each leg becomes one
    GeoJSON Feature with mode, provenance, and derived route_quality.

    Args:
        shipment_id: Shipment UUID
        db: Database session
        current_user: Authenticated user

    Returns:
        GeoJSON FeatureCollection with one Feature per leg

    Raises:
        HTTPException: 404 if shipment not found

    Behavior:
        - Returns empty FeatureCollection (not error) if shipment has no legs
        - Skips legs with malformed/missing geometry (logs warning, continues)
        - Features ordered by leg sequence_number

    DESIGN DECISION: Malformed geometry handling
    If a leg's stored route_geometry_json is invalid/missing, that leg is
    SKIPPED with a logged warning, and remaining valid legs are still returned.
    This "graceful degradation" approach is more useful for dashboards than
    failing the entire route view due to one corrupted leg.
    """
    # Verify shipment exists
    shipment_exists = await db.execute(select(exists().where(Shipment.id == shipment_id)))
    if not shipment_exists.scalar():
        raise HTTPException(status_code=404, detail=f"Shipment {shipment_id} not found")

    # Load legs with geometry
    legs_result = await db.execute(
        select(Leg).where(Leg.shipment_id == shipment_id).order_by(Leg.sequence_number)
    )
    legs = legs_result.scalars().all()

    # Build GeoJSON features
    features = []
    for leg in legs:
        # Parse stored geometry JSON
        if not leg.route_geometry_json:
            logger.warning(f"Leg {leg.id} (shipment {shipment_id}) has no route geometry, skipping")
            continue

        try:
            geometry = json.loads(leg.route_geometry_json)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Leg {leg.id} (shipment {shipment_id}) has malformed geometry JSON: {e}, skipping"
            )
            continue

        # Derive route quality from provenance
        provenance_str = str(leg.provenance)
        quality = derive_route_quality(provenance_str)

        features.append(
            RouteFeature(
                type="Feature",
                geometry=geometry,
                properties={
                    "leg_id": leg.id,
                    "sequence": leg.sequence_number,
                    "mode": leg.transport_mode,
                    "provenance": leg.provenance,
                    "route_quality": quality,
                    "status": leg.status,
                },
            )
        )

    return RouteFeatureCollection(
        type="FeatureCollection",
        features=features,
    )


@router.get("/{shipment_id}/events", response_model=PaginatedResponse[ShipmentEvent])
async def get_shipment_events(
    shipment_id: str = Path(..., description="Shipment UUID"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Page size (max 100)"),
    db: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> PaginatedResponse[ShipmentEvent]:
    """Get shipment event history from the events table.

    Returns paginated events from the events table, ordered by occurred_at DESC.

    Args:
        shipment_id: Shipment UUID
        page: Page number (1-indexed)
        size: Items per page (max 100)
        db: Database session
        current_user: Authenticated user

    Returns:
        Paginated events in descending timestamp order (most recent first)

    Raises:
        HTTPException: 404 if shipment not found
    """
    # Verify shipment exists
    shipment_exists = await db.execute(select(exists().where(Shipment.id == shipment_id)))
    if not shipment_exists.scalar():
        raise HTTPException(status_code=404, detail=f"Shipment {shipment_id} not found")

    # Query events table
    base_filter = Event.shipment_id == shipment_id
    count_result = await db.execute(
        select(func.count()).select_from(Event).where(base_filter)
    )
    total = count_result.scalar() or 0

    offset = (page - 1) * size
    rows = await db.execute(
        select(Event)
        .where(base_filter)
        .order_by(Event.occurred_at.desc())
        .limit(size)
        .offset(offset)
    )
    event_rows = rows.scalars().all()

    events = [
        ShipmentEvent(
            timestamp=ev.occurred_at,
            event_type=ev.event_type,
            description=ev.description or ev.event_type,
            actor=ev.source,
            location_locode=ev.location_locode,
        )
        for ev in event_rows
    ]

    total_pages = (total + size - 1) // size if total > 0 else 0

    return PaginatedResponse(
        items=events,
        total=total,
        page=page,
        size=size,
        total_pages=total_pages,
    )
