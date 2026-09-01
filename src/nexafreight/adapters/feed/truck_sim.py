"""
Truck simulation feed adapter.

Interpolates truck positions along pre-computed GeoJSON route geometries
based on elapsed time vs planned duration.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from shapely.geometry import LineString, shape

from nexafreight.adapters.protocols import (
    AssetPosition,
    AssetType,
    FeedHealth,
    Provenance,
)

logger = logging.getLogger(__name__)


def _calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate initial compass bearing from (lat1, lon1) to (lat2, lon2) in degrees."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(rlat2)
    x = math.cos(rlat1) * math.sin(rlat2) - math.sin(rlat1) * math.cos(rlat2) * math.cos(dlon)
    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360) % 360


class TruckSimAdapter:
    """Simulation adapter for ROAD transport legs."""

    def __init__(self, now_fn: Callable[[], datetime] | None = None) -> None:
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._legs: dict[str, dict[str, Any]] = {}
        self._is_running: bool = False

    async def start(self) -> None:
        self._is_running = True

    async def stop(self) -> None:
        self._is_running = False

    async def add_leg(
        self,
        leg_id: str,
        geometry_geojson: str,
        departure: datetime,
        planned_duration_s: float,
    ) -> None:
        """Register a road leg for simulation."""
        try:
            geo_dict = json.loads(geometry_geojson)
            geom = shape(geo_dict)
            if not isinstance(geom, LineString) and hasattr(geom, "geoms"):
                # If MultiLineString or GeometryCollection, take union or first line
                geom = list(geom.geoms)[0]
            dep_utc = departure.replace(tzinfo=UTC) if departure.tzinfo is None else departure
            self._legs[leg_id] = {
                "geom": geom,
                "departure": dep_utc,
                "duration_s": max(1.0, planned_duration_s),
            }

        except Exception as exc:
            logger.warning("Failed to parse geometry for leg %s: %s", leg_id, exc)

    async def remove_leg(self, leg_id: str) -> None:
        self._legs.pop(leg_id, None)

    async def get_current_positions(self) -> list[AssetPosition]:
        """Compute and return current positions for all active legs."""
        now = self._now_fn()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        positions: list[AssetPosition] = []
        for leg_id, data in list(self._legs.items()):
            departure = data["departure"]
            duration_s = data["duration_s"]
            geom: LineString = data["geom"]

            elapsed = (now - departure).total_seconds()
            if elapsed < 0:
                # Not yet departed
                fraction = 0.0
            elif elapsed > duration_s:
                # Completed
                fraction = 1.0
            else:
                fraction = elapsed / duration_s

            point = geom.interpolate(fraction, normalized=True)
            lon, lat = point.x, point.y

            # Calculate heading by sampling slightly ahead
            next_fraction = min(1.0, fraction + 0.01)
            next_point = geom.interpolate(next_fraction, normalized=True)
            heading = _calculate_bearing(lat, lon, next_point.y, next_point.x)

            # Approximate road speed in knots (~60 km/h = 32.4 knots)
            speed_knots = 32.4 if 0.0 < fraction < 1.0 else 0.0

            positions.append(
                AssetPosition(
                    asset_id=leg_id,
                    asset_type=AssetType.ROAD,
                    lat=lat,
                    lon=lon,
                    speed_knots=speed_knots,
                    heading_deg=heading,
                    reported_at=now,
                    provenance=Provenance.SIMULATED,
                    source="ROAD_INTERPOLATION",
                )
            )

        return positions

    async def health(self) -> FeedHealth:
        return FeedHealth(
            adapter_name="truck_sim",
            is_healthy=self._is_running,
            last_success_at=self._now_fn(),
            messages_received=len(self._legs),
            provenance=Provenance.SIMULATED,
        )
