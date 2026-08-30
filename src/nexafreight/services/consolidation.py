"""Shipment consolidation service.

Groups line-level / order-level records into containerized multi-modal shipments.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.enums import CargoClass, LocationType, ShipmentStatus, TransportMode
from nexafreight.models.location import Location
from nexafreight.models.order import Order
from nexafreight.models.shipment import Shipment


@dataclass
class OrderView:
    id: int
    order_number: str
    origin_country_code: str
    dest_country_code: str
    shipping_mode: str
    cargo_class: str
    sla_deadline: datetime
    revenue: float
    shipping_cost: float


@dataclass
class ShipmentSpec:
    id: str  # UUID string
    origin_id: int
    destination_id: int
    primary_transport_mode: str
    cargo_class: str
    container_count: int
    strictest_sla_deadline: datetime
    order_ids: list[int] = field(default_factory=list)
    total_revenue: float = 0.0


def _consolidate_orders_sync(
    orders: Sequence[OrderView],
    country_to_location: dict[str, int],
    default_origin_id: int,
    default_dest_id: int,
    max_orders_per_shipment: int = 20,
) -> list[ShipmentSpec]:
    groups: dict[tuple[str, str, str, str, int, int], list[OrderView]] = defaultdict(list)

    for order in orders:
        orig_cc = order.origin_country_code.upper() if order.origin_country_code else "US"
        dest_cc = order.dest_country_code.upper() if order.dest_country_code else "US"
        mode = order.shipping_mode.upper() if order.shipping_mode else "SEA"
        cargo = order.cargo_class.upper() if order.cargo_class else "STANDARD"

        d = order.sla_deadline
        year, week, _ = d.isocalendar()

        key = (orig_cc, dest_cc, mode, cargo, year, week)
        groups[key].append(order)

    shipments: list[ShipmentSpec] = []

    for (orig_cc, dest_cc, mode, cargo, _year, _week), group_orders in groups.items():
        for i in range(0, len(group_orders), max_orders_per_shipment):
            chunk = group_orders[i : i + max_orders_per_shipment]
            strictest_deadline = min(o.sla_deadline for o in chunk)
            tot_rev = sum(o.revenue for o in chunk)

            orig_id = country_to_location.get(orig_cc, default_origin_id)
            dest_id = country_to_location.get(dest_cc, default_dest_id)
            if orig_id == dest_id:
                dest_id = default_dest_id if orig_id != default_dest_id else default_origin_id

            shipment_id = str(uuid.uuid4())
            shipments.append(
                ShipmentSpec(
                    id=shipment_id,
                    origin_id=orig_id,
                    destination_id=dest_id,
                    primary_transport_mode=mode,
                    cargo_class=cargo,
                    container_count=max(1, math.ceil(len(chunk) / 20)),
                    strictest_sla_deadline=strictest_deadline,
                    order_ids=[o.id for o in chunk],
                    total_revenue=round(tot_rev, 2),
                )
            )

    return shipments


async def _ensure_locations_exist(session: AsyncSession, location_ids: set[int]) -> None:
    for loc_id in location_ids:
        loc = await session.get(Location, loc_id)
        if loc is None:
            loc = Location(
                id=loc_id,
                locode=f"L{loc_id:04d}",
                name=f"Location {loc_id}",
                country_code="US",
                location_type=LocationType.PORT,
                latitude=0.0,
                longitude=0.0,
            )
            session.add(loc)
    await session.flush()


def _normalize_dt(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _consolidate_orders_async(
    session: AsyncSession,
    max_orders_per_shipment: int = 20,
) -> list[Shipment]:
    result = await session.execute(select(Order).where(Order.shipment_id.is_(None)))
    orders = list(result.scalars().all())
    if not orders:
        return []

    needed_loc_ids: set[int] = set()
    groups: dict[tuple[int, int, Any, Any, int, int], list[Order]] = defaultdict(list)
    for order in orders:
        orig_id = getattr(order, "origin_id", 1) or 1
        dest_id = getattr(order, "destination_id", 2) or 2
        needed_loc_ids.add(orig_id)
        needed_loc_ids.add(dest_id)
        mode = order.shipping_mode
        cargo = order.cargo_class
        dt = _normalize_dt(order.created_at if order.created_at is not None else order.sla_deadline)
        year, week, _ = dt.isocalendar()
        key = (orig_id, dest_id, mode, cargo, year, week)
        groups[key].append(order)

    await _ensure_locations_exist(session, needed_loc_ids)

    created_shipments: list[Shipment] = []
    for (orig_id, dest_id, mode, cargo, _year, _week), group_orders in groups.items():
        for i in range(0, len(group_orders), max_orders_per_shipment):
            chunk = group_orders[i : i + max_orders_per_shipment]
            strictest = min(_normalize_dt(o.sla_deadline) for o in chunk)

            transport_mode = TransportMode(mode) if isinstance(mode, str) else mode
            cargo_class_val = CargoClass(cargo) if isinstance(cargo, str) else cargo

            shipment = Shipment(
                origin_id=orig_id,
                destination_id=dest_id,
                primary_transport_mode=transport_mode,
                cargo_class=cargo_class_val,
                status=ShipmentStatus.PLANNED,
                container_count=len(chunk),
                strictest_sla_deadline=strictest,
                route_version=1,
            )
            session.add(shipment)
            await session.flush()

            for order in chunk:
                order.shipment_id = shipment.id

            created_shipments.append(shipment)

    await session.commit()
    for s in created_shipments:
        await session.refresh(s)
        if s.strictest_sla_deadline and s.strictest_sla_deadline.tzinfo is None:
            s.strictest_sla_deadline = s.strictest_sla_deadline.replace(tzinfo=UTC)

    return created_shipments


def consolidate_orders(
    orders_or_session: Any,
    country_to_location: dict[str, int] | None = None,
    default_origin_id: int = 1,
    default_dest_id: int = 2,
    max_orders_per_shipment: int = 20,
) -> Any:
    """Consolidate orders into containerized shipments.

    Supports:
    - AsyncSession: await consolidate_orders(session) -> list[Shipment]
    - Sequence[OrderView]: consolidate_orders(order_views, ...) -> list[ShipmentSpec]
    """
    if isinstance(orders_or_session, AsyncSession):
        return _consolidate_orders_async(orders_or_session, max_orders_per_shipment)
    return _consolidate_orders_sync(
        orders_or_session,
        country_to_location or {},
        default_origin_id,
        default_dest_id,
        max_orders_per_shipment,
    )
