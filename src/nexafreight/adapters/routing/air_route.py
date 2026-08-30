"""Air routing adapter (geodesic arc)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from geopy.distance import geodesic  # type: ignore

from ._geometry import great_circle_geojson_str

log = logging.getLogger("nexafreight.routing.air")
DEFAULT_CRUISE_KMH = 900.0


@dataclass
class AirRouteResult:
    geometry_geojson: str
    distance_km: float
    duration_s: float
    route_quality: str = "COMPUTED"


def compute_air_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    duration_hours: float | None = None,
    cruise_speed_kmh: float = DEFAULT_CRUISE_KMH,
) -> AirRouteResult:
    distance_km = geodesic((origin_lat, origin_lon), (dest_lat, dest_lon)).kilometers

    if duration_hours and duration_hours > 0:
        duration_s = duration_hours * 3600.0
    else:
        duration_s = (distance_km / cruise_speed_kmh) * 3600.0

    geometry = great_circle_geojson_str(origin_lat, origin_lon, dest_lat, dest_lon)
    return AirRouteResult(
        geometry_geojson=geometry,
        distance_km=round(distance_km, 2),
        duration_s=round(duration_s, 1),
        route_quality="COMPUTED",
    )
