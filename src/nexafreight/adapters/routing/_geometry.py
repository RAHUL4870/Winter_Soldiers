"""Shared geodesic geometry helpers used by the routing adapters."""

from __future__ import annotations

import json
import math
from typing import Any

EARTH_RADIUS_KM = 6371.0
NM_PER_KM = 1.0 / 1.852


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (haversine formula)."""
    rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
    rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return haversine_km(lat1, lon1, lat2, lon2) * NM_PER_KM


def _to_unit_vec(lat: float, lon: float) -> tuple[float, float, float]:
    rlat, rlon = math.radians(lat), math.radians(lon)
    return (
        math.cos(rlat) * math.cos(rlon),
        math.cos(rlat) * math.sin(rlon),
        math.sin(rlat),
    )


def _from_unit_vec(x: float, y: float, z: float) -> tuple[float, float]:
    lat = math.degrees(math.asin(max(-1.0, min(1.0, z))))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon


def great_circle_points(
    lat1: float, lon1: float, lat2: float, lon2: float, n: int = 24
) -> list[tuple[float, float]]:
    """Sample `n` points along great-circle arc using spherical linear interpolation."""
    ax, ay, az = _to_unit_vec(lat1, lon1)
    bx, by, bz = _to_unit_vec(lat2, lon2)

    dot = max(-1.0, min(1.0, ax * bx + ay * by + az * bz))
    omega = math.acos(dot)
    pts: list[tuple[float, float]] = []

    if omega < 1e-9:
        for i in range(n):
            t = i / max(1, n - 1)
            pts.append((lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t))
        return pts

    sin_omega = math.sin(omega)
    for i in range(n):
        t = i / max(1, n - 1)
        sa = math.sin((1 - t) * omega) / sin_omega
        sb = math.sin(t * omega) / sin_omega
        vx = ax * sa + bx * sb
        vy = ay * sa + by * sb
        vz = az * sa + bz * sb
        pts.append(_from_unit_vec(vx, vy, vz))

    return pts


def great_circle_geojson(
    lat1: float, lon1: float, lat2: float, lon2: float, n: int = 24
) -> dict[str, Any]:
    """Return a GeoJSON LineString dict following the great-circle arc."""
    pts = great_circle_points(lat1, lon1, lat2, lon2, n)
    coords = [[lon, lat] for lat, lon in pts]  # GeoJSON: [lon, lat]
    return {"type": "LineString", "coordinates": coords}


def great_circle_geojson_str(
    lat1: float, lon1: float, lat2: float, lon2: float, n: int = 24
) -> str:
    return json.dumps(great_circle_geojson(lat1, lon1, lat2, lon2, n))
