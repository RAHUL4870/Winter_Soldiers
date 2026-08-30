"""Road routing adapter using OpenRouteService with offline fallback."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ._geometry import great_circle_geojson_str, haversine_km

log = logging.getLogger("nexafreight.routing.road")
DEFAULT_TRUCK_SPEED_KMH = 65.0
ROAD_CIRCUITY_FACTOR = 1.35


@dataclass
class RoadRouteResult:
    geometry_geojson: str
    distance_km: float
    duration_s: float
    route_quality: str = "COMPUTED"
    source: str = "OPENROUTESERVICE"


class RoadRouter:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key
        self._client = None
        if api_key:
            try:
                import openrouteservice  # type: ignore

                self._client = openrouteservice.Client(key=api_key)
            except Exception as exc:
                log.warning("Could not initialize openrouteservice client: %s", exc)

    def compute(
        self,
        origin: tuple[float, float],
        dest: tuple[float, float],
    ) -> RoadRouteResult:
        olat, olon = origin
        dlat, dlon = dest

        if self._client:
            try:
                # OpenRouteService expects coordinates in [lon, lat] order
                coords = [[olon, olat], [dlon, dlat]]
                routes = self._client.directions(
                    coordinates=coords,
                    profile="driving-hgv",
                    format="geojson",
                )
                features = routes.get("features", [])
                if features:
                    feat = features[0]
                    geom = json.dumps(feat.get("geometry", {}))
                    props = feat.get("properties", {}).get("summary", {})
                    dist_km = float(props.get("distance", 0.0)) / 1000.0
                    duration_s = float(props.get("duration", 0.0))
                    if dist_km > 0 and geom:
                        return RoadRouteResult(
                            geometry_geojson=geom,
                            distance_km=round(dist_km, 2),
                            duration_s=round(duration_s, 1),
                            route_quality="COMPUTED",
                            source="OPENROUTESERVICE",
                        )
            except Exception as exc:
                log.warning("OpenRouteService API call failed (%s); using road fallback", exc)

        # Fallback: Great-circle distance scaled by road circuity factor (1.35)
        direct_km = haversine_km(olat, olon, dlat, dlon)
        dist_km = direct_km * ROAD_CIRCUITY_FACTOR
        duration_s = (dist_km / DEFAULT_TRUCK_SPEED_KMH) * 3600.0
        geom = great_circle_geojson_str(olat, olon, dlat, dlon, n=16)

        return RoadRouteResult(
            geometry_geojson=geom,
            distance_km=round(dist_km, 2),
            duration_s=round(duration_s, 1),
            route_quality="APPROXIMATE",
            source="ROAD_FALLBACK",
        )
