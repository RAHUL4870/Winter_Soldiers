"""
Pydantic response schemas for /api/map/* endpoints (T-031).

All schemas are read-only (response models only). No request bodies
are defined here — all map endpoints are GET requests.

Position serialization note
───────────────────────────
AssetPosition is a frozen dataclass (T-011), not a Pydantic model.
PositionOut wraps it into a Pydantic-serializable form.

GeoJSON note
────────────
GeoJSON objects are represented as plain Python dicts (dict[str, Any])
because their structure varies by geometry type and FastAPI/Pydantic
serializes them correctly. A strict Pydantic model would duplicate
the GeoJSON spec for marginal benefit.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PositionOut(BaseModel):
    """
    Canonical position DTO for all map endpoints.

    Field names support both external frontend-facing conventions
    (latitude / longitude, recorded_at) and internal conventions (lat / lon, reported_at).
    """

    asset_id: str
    asset_type: str  # SEA | ROAD | AIR | RAIL
    latitude: float
    longitude: float
    lat: float | None = None
    lon: float | None = None
    speed_knots: float | None = None
    heading_deg: float | None = None
    provenance: str  # REAL | REPLAYED | SIMULATED | ...
    recorded_at: datetime
    reported_at: datetime | None = None
    source: str

    model_config = {"from_attributes": True}


class GeoJSONFeature(BaseModel):
    """One GeoJSON Feature (geometry + properties)."""

    type: str = "Feature"
    geometry: dict[str, Any]  # GeoJSON geometry object
    properties: dict[str, Any]  # Flexible — varies by feature type


class GeoJSONFeatureCollection(BaseModel):
    """Top-level GeoJSON FeatureCollection returned by routes and ports."""

    type: str = "FeatureCollection"
    features: list[GeoJSONFeature] = Field(default_factory=list)


class FeedHealthOut(BaseModel):
    """Health status of one position feed adapter."""

    adapter_name: str
    is_healthy: bool
    last_success_at: datetime | None = None
    messages_received: int = 0
    provenance: str


class FeedHealthResponse(BaseModel):
    """Response for GET /api/map/feed-health."""

    adapters: list[FeedHealthOut] = Field(default_factory=list)
