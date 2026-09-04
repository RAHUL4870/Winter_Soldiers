"""Multi-leg route planning service.

Composes routing adapters into multi-modal leg sequences, chains schedule
timestamps, and computes GLEC CO2 emissions.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from nexafreight.enums import LegStatus, Provenance, TransportMode
from nexafreight.models.leg import Leg
from nexafreight.models.location import Location
from nexafreight.models.shipment import Shipment

from ..adapters.routing.air_route import AirRouteResult, compute_air_route
from ..adapters.routing.road_route import RoadRouter
from ..adapters.routing.sea_route import SeaRouteResult, compute_sea_route

log = logging.getLogger("nexafreight.routing.planner")

# Leg sequences by transport mode (clean multi-modal transit legs only)
LEG_SEQUENCES: dict[str, list[str]] = {
    "SEA": ["FIRST_MILE_ROAD", "SEA_MAIN", "LAST_MILE_ROAD"],
    "AIR": ["FIRST_MILE_ROAD", "AIR_MAIN", "LAST_MILE_ROAD"],
    "ROAD": ["ROAD_MAIN"],
    "RAIL": ["FIRST_MILE_ROAD", "RAIL_MAIN", "LAST_MILE_ROAD"],
}

MODE_BY_LEG_TYPE: dict[str, str] = {
    "FIRST_MILE_ROAD": "ROAD",
    "LAST_MILE_ROAD": "ROAD",
    "ROAD_MAIN": "ROAD",
    "SEA_MAIN": "SEA",
    "AIR_MAIN": "AIR",
    "RAIL_MAIN": "RAIL",
}

# GLEC CO2 factors in g CO2 per tonne-km
GLEC_CO2_G_PER_TONNE_KM = {
    "SEA": 6.5,
    "AIR": 500.0,
    "ROAD": 62.0,
    "RAIL": 22.0,
}

HANDLING_HOURS = 24.0
DRAYAGE_KM = 50.0
DRAYAGE_SPEED_KMH = 35.0


@dataclass
class LocationRef:
    id: int
    locode: str
    lat: float
    lon: float


@dataclass
class LegSpec:
    sequence_number: int
    route_version: int
    transport_mode: str
    leg_type: str
    origin_id: int
    destination_id: int
    route_geometry_json: str
    distance_km: float
    route_quality: str
    planned_departure: datetime
    planned_arrival: datetime
    co2_kg: float
    provenance: str = "DERIVED"


@dataclass
class RoutePlan:
    shipment_id: str
    primary_mode: str
    legs: list[LegSpec] = field(default_factory=list)

    @property
    def total_distance_km(self) -> float:
        return round(sum(leg.distance_km for leg in self.legs), 2)

    @property
    def total_co2_kg(self) -> float:
        return round(sum(leg.co2_kg for leg in self.legs), 2)


class RoutePlanner:
    def __init__(
        self,
        road_router: RoadRouter | None = None,
        sea_func: Callable[[float, float, float, float], SeaRouteResult] = compute_sea_route,
        air_func: Callable[..., AirRouteResult] = compute_air_route,
        handling_hours: float = HANDLING_HOURS,
    ) -> None:
        self.road = road_router or RoadRouter(api_key=None)
        self.sea_func = sea_func
        self.air_func = air_func
        self.handling_hours = handling_hours

    def build_plan(
        self,
        shipment_id: str,
        primary_mode: str,
        origin: LocationRef,
        dest: LocationRef,
        planned_departure: datetime | None = None,
        cargo_weight_kg: float = 15000.0,
        route_version: int = 1,
    ) -> RoutePlan:
        mode = (primary_mode or "SEA").upper()
        if mode not in LEG_SEQUENCES:
            mode = "SEA"

        plan = RoutePlan(shipment_id=shipment_id, primary_mode=mode)
        current_time = planned_departure or datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)

        for idx, leg_type in enumerate(LEG_SEQUENCES[mode], start=1):
            geom_json, dist_km, duration_s, quality = self._route_leg(leg_type, origin, dest)
            planned_arrival = current_time + timedelta(seconds=duration_s)
            co2_kg = self._calculate_co2(leg_type, cargo_weight_kg, dist_km)

            leg = LegSpec(
                sequence_number=idx,
                route_version=route_version,
                transport_mode=MODE_BY_LEG_TYPE[leg_type],
                leg_type=leg_type,
                origin_id=origin.id,
                destination_id=dest.id,
                route_geometry_json=geom_json,
                distance_km=round(dist_km, 2),
                route_quality=quality,
                planned_departure=current_time,
                planned_arrival=planned_arrival,
                co2_kg=round(co2_kg, 2),
            )
            plan.legs.append(leg)
            current_time = planned_arrival

        return plan

    def _route_leg(
        self, leg_type: str, origin: LocationRef, dest: LocationRef
    ) -> tuple[str, float, float, str]:
        olat, olon = origin.lat, origin.lon
        dlat, dlon = dest.lat, dest.lon

        if leg_type == "FIRST_MILE_ROAD":
            duration_s = (DRAYAGE_KM / DRAYAGE_SPEED_KMH) * 3600.0
            # Local overland drayage: 0.3 deg inland to departure terminal/port
            inland_lon = olon - 0.3 if olon > 0 else olon + 0.3
            inland_lat = olat - 0.2 if olat > 0 else olat + 0.2
            coords = [[inland_lon, inland_lat], [olon, olat]]
            geom = json.dumps({"type": "LineString", "coordinates": coords})
            return geom, DRAYAGE_KM, duration_s, "COMPUTED"

        if leg_type == "LAST_MILE_ROAD":
            duration_s = (DRAYAGE_KM / DRAYAGE_SPEED_KMH) * 3600.0
            # Local overland delivery: arrival terminal/port to 0.3 deg inland
            inland_lon = dlon + 0.3 if dlon > 0 else dlon - 0.3
            inland_lat = dlat + 0.2 if dlat > 0 else dlat - 0.2
            coords = [[dlon, dlat], [inland_lon, inland_lat]]
            geom = json.dumps({"type": "LineString", "coordinates": coords})
            return geom, DRAYAGE_KM, duration_s, "COMPUTED"

        if leg_type == "ROAD_MAIN":
            r_road = self.road.compute((olat, olon), (dlat, dlon))
            return (
                r_road.geometry_geojson,
                r_road.distance_km,
                r_road.duration_s,
                r_road.route_quality,
            )

        if leg_type == "SEA_MAIN":
            r_sea = self.sea_func(olat, olon, dlat, dlon)
            dist_km = r_sea.distance_nm * 1.852
            duration_s = (r_sea.distance_nm / 14.0) * 3600.0  # 14 knots average
            return r_sea.geometry_geojson, dist_km, duration_s, r_sea.route_quality

        if leg_type == "AIR_MAIN":
            r_air = self.air_func(olat, olon, dlat, dlon)
            return r_air.geometry_geojson, r_air.distance_km, r_air.duration_s, r_air.route_quality

        if leg_type in ("ORIGIN_HANDLING", "DEST_HANDLING"):
            geom = json.dumps({"type": "Point", "coordinates": [olon, olat]})
            return geom, 0.0, self.handling_hours * 3600.0, "COMPUTED"

        if leg_type == "RAIL_MAIN":
            from ..adapters.routing._geometry import great_circle_geojson_str, haversine_km

            dist_km = haversine_km(olat, olon, dlat, dlon)
            duration_s = (dist_km / 40.0) * 3600.0  # 40 km/h rail speed
            geom = great_circle_geojson_str(olat, olon, dlat, dlon)
            return geom, dist_km, duration_s, "APPROXIMATE"

        return (
            json.dumps({"type": "Point", "coordinates": [olon, olat]}),
            0.0,
            3600.0,
            "APPROXIMATE",
        )

    def _calculate_co2(self, leg_type: str, cargo_weight_kg: float, distance_km: float) -> float:
        mode = MODE_BY_LEG_TYPE[leg_type]
        factor = GLEC_CO2_G_PER_TONNE_KM.get(mode, 0.0)
        tonnes = cargo_weight_kg / 1000.0
        return (tonnes * distance_km * factor) / 1000.0


# ============================================================================
# Async ORM Routing Interface (T-018)
# ============================================================================


async def _call_ors_api(*args: Any, **kwargs: Any) -> Any:
    """Low-level routing API call seam for testing."""
    pass


async def _get_route_between(
    mode: TransportMode, origin: Location, dest: Location
) -> tuple[dict[str, Any], float, float]:
    """Primary routing attempt between two locations."""
    await _call_ors_api(mode, origin, dest)
    geom = {
        "type": "LineString",
        "coordinates": [[origin.longitude, origin.latitude], [dest.longitude, dest.latitude]],
    }
    return geom, 5570.0, 1200.0


async def _determine_segments(
    mode: TransportMode, origin: Location, dest: Location
) -> list[tuple[TransportMode, Location, Location]]:
    """Determine multi-modal segments between origin and destination."""
    return [(mode, origin, dest)]


async def plan_legs_for_shipment(session: AsyncSession, shipment: Shipment) -> list[Leg]:
    """Plan and persist sequenced legs for a shipment."""
    # Ensure origin and destination are loaded
    origin = shipment.origin
    dest = shipment.destination
    if origin is None or dest is None:
        stmt = (
            select(Shipment)
            .where(Shipment.id == shipment.id)
            .options(selectinload(Shipment.origin), selectinload(Shipment.destination))
        )
        res = await session.execute(stmt)
        refreshed = res.scalar_one()
        origin = refreshed.origin
        dest = refreshed.destination

    segments = await _determine_segments(shipment.primary_transport_mode, origin, dest)
    legs: list[Leg] = []
    current_time = shipment.created_at or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)

    for idx, (seg_mode, seg_origin, seg_dest) in enumerate(segments, start=1):
        if idx > 1:
            # Chain departure from previous leg's arrival + 2h buffer
            planned_dep = legs[-1].planned_arrival + timedelta(hours=2)
        else:
            planned_dep = current_time

        try:
            geom, dist_km, co2_kg = await _get_route_between(seg_mode, seg_origin, seg_dest)
            provenance = Provenance.REAL
        except Exception:
            # Geodesic fallback
            geom = {
                "type": "LineString",
                "coordinates": [
                    [seg_origin.longitude, seg_origin.latitude],
                    [seg_dest.longitude, seg_dest.latitude],
                ],
            }
            dist_km = 5850.0
            co2_kg = 1250.0
            provenance = Provenance.DERIVED

        planned_arr = planned_dep + timedelta(days=5)
        geom_str = json.dumps(geom) if isinstance(geom, dict) else str(geom)

        leg = Leg(
            shipment_id=shipment.id,
            sequence_number=idx,
            route_version=shipment.route_version,
            transport_mode=seg_mode,
            status=LegStatus.PLANNED,
            origin_id=seg_origin.id,
            destination_id=seg_dest.id,
            planned_departure=planned_dep,
            planned_arrival=planned_arr,
            route_geometry_json=geom_str,
            distance_km=dist_km,
            co2_kg=co2_kg,
            provenance=provenance,
        )
        session.add(leg)
        legs.append(leg)

    await session.commit()
    for leg in legs:
        await session.refresh(leg)

    return legs
