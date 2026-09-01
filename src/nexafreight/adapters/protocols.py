"""Adapter protocol definitions and shared value objects for position feeds.

This module defines the structural typing contract (Protocol) that every
position feed adapter — mock, AIS WebSocket, replay, truck simulator, flight
replay — must satisfy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from nexafreight.enums import Provenance, TransportMode

AssetType = TransportMode


@dataclass(frozen=True, init=False)
class AssetPosition:
    """Canonical immutable position DTO used across all feed adapters."""

    asset_id: str
    asset_type: TransportMode
    lat: float
    lon: float
    speed_knots: float | None
    heading_deg: float | None
    reported_at: datetime
    provenance: Provenance
    source: str

    def __init__(
        self,
        asset_id: str,
        asset_type: TransportMode,
        lat: float | None = None,
        lon: float | None = None,
        speed_knots: float | None = None,
        heading_deg: float | None = None,
        reported_at: datetime | None = None,
        provenance: Provenance = Provenance.REAL,
        source: str = "",
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        eff_lat = lat if lat is not None else (latitude if latitude is not None else 0.0)
        eff_lon = lon if lon is not None else (longitude if longitude is not None else 0.0)
        eff_time = (
            reported_at
            if reported_at is not None
            else (recorded_at if recorded_at is not None else datetime.now())
        )

        object.__setattr__(self, "asset_id", str(asset_id))
        object.__setattr__(self, "asset_type", asset_type)
        object.__setattr__(self, "lat", float(eff_lat))
        object.__setattr__(self, "lon", float(eff_lon))
        object.__setattr__(self, "speed_knots", speed_knots)
        object.__setattr__(self, "heading_deg", heading_deg)
        object.__setattr__(self, "reported_at", eff_time)
        object.__setattr__(self, "provenance", provenance)
        object.__setattr__(self, "source", str(source))

    @property
    def latitude(self) -> float:
        return self.lat

    @property
    def longitude(self) -> float:
        return self.lon

    @property
    def recorded_at(self) -> datetime:
        return self.reported_at


@dataclass(frozen=True, init=False)
class FeedHealth:
    """Canonical immutable health DTO used across all feed adapters."""

    adapter_name: str
    is_healthy: bool
    last_success_at: datetime | None
    messages_received: int
    provenance: Provenance

    def __init__(
        self,
        adapter_name: str,
        is_healthy: bool,
        last_success_at: datetime | None = None,
        messages_received: int = 0,
        provenance: Provenance = Provenance.REAL,
    ) -> None:
        object.__setattr__(self, "adapter_name", str(adapter_name))
        object.__setattr__(self, "is_healthy", bool(is_healthy))
        object.__setattr__(self, "last_success_at", last_success_at)
        object.__setattr__(self, "messages_received", int(messages_received))
        object.__setattr__(self, "provenance", provenance)


@runtime_checkable
class PositionFeedAdapter(Protocol):
    """Structural contract every position feed adapter must satisfy."""

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def get_current_positions(self) -> list[AssetPosition]: ...
    async def health(self) -> FeedHealth: ...


# Compatibility aliases for previous stubs if needed
PositionReport = AssetPosition
FeedAdapter = PositionFeedAdapter


class LLMAdapter(Protocol):
    """LLM Adapter protocol."""

    async def generate(self, system_prompt: str, user_message: str) -> str: ...
