"""Sea routing adapter with searoute-py and fallback."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ._geometry import great_circle_geojson_str, haversine_nm

log = logging.getLogger("nexafreight.routing.sea")


@dataclass
class SeaRouteResult:
    geometry_geojson: str
    distance_nm: float
    route_quality: str
    source: str


def compute_sea_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
) -> SeaRouteResult:
    try:
        import searoute as sr

        route = sr.searoute(
            [origin_lon, origin_lat],
            [dest_lon, dest_lat],
            units="naut",
        )
        geometry = json.dumps(route["geometry"])
        distance_nm = float(route["properties"]["length"])
        if distance_nm <= 0 or not geometry:
            raise ValueError("searoute returned empty geometry/distance")
        return SeaRouteResult(
            geometry_geojson=geometry,
            distance_nm=round(distance_nm, 2),
            route_quality="COMPUTED",
            source="SEAROUTE",
        )
    except Exception as exc:
        log.warning("searoute failed (%s); using great-circle APPROXIMATE fallback", exc)
        distance_nm = haversine_nm(origin_lat, origin_lon, dest_lat, dest_lon)
        geometry = great_circle_geojson_str(origin_lat, origin_lon, dest_lat, dest_lon)
        return SeaRouteResult(
            geometry_geojson=geometry,
            distance_nm=round(distance_nm, 2),
            route_quality="APPROXIMATE",
            source="GREAT_CIRCLE_FALLBACK",
        )
