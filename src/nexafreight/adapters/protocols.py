"""Adapter protocol definitions and shared value objects for position feeds.

This module defines the structural typing contract (Protocol) that every
position feed adapter — mock, AIS WebSocket, replay, truck simulator, flight
replay — must satisfy. No inheritance is required; adapters conform simply
by implementing matching method signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from nexafreight.enums import Provenance, TransportMode


@dataclass(frozen=True, slots=True)
class AssetPosition:
    """A single point-in-time position report for a tracked asset.

    asset_id is deliberately generic — it carries whatever identifier
    concept the source adapter uses (MMSI for vessels, VIN/truck_id for
    trucks, flight_number for flights). No adapter-specific identifier
    fields exist on this shared structure; each adapter is responsible
    for mapping its own domain identifier into this single string field.
    """

    asset_id: str
    asset_type: TransportMode
    latitude: float
    longitude: float
    heading_deg: float | None
    speed_knots: float | None
    recorded_at: datetime
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class FeedHealth:
    """Health/liveness snapshot for a position feed adapter."""

    adapter_name: str
    is_healthy: bool
    last_success_at: datetime | None
    messages_received: int


@runtime_checkable
class PositionFeedAdapter(Protocol):
    """Structural contract every position feed adapter must satisfy.

    All methods are async, even for adapters (like MockFeedAdapter) whose
    internals are trivially synchronous, so that calling code (workers,
    services in later tasks) can treat every adapter — mock or real,
    WebSocket-driven or file-replay-driven — uniformly.

    This is intentionally a Protocol, not an abstract base class: adapters
    conform structurally by implementing these method signatures, with no
    inheritance relationship required.
    """

    async def start(self) -> None:
        """Begin feeding positions (connect, open file, start simulation clock)."""
        ...

    async def stop(self) -> None:
        """Stop feeding positions and release any held resources."""
        ...

    async def get_current_positions(self) -> list[AssetPosition]:
        """Return the most recent known position for each tracked asset."""
        ...

    async def get_health(self) -> FeedHealth:
        """Return the adapter's current health/liveness status."""
        ...


# Compatibility aliases for previous stubs if needed
PositionReport = AssetPosition
FeedAdapter = PositionFeedAdapter


class LLMAdapter(Protocol):
    """LLM Adapter protocol."""

    async def generate(self, system_prompt: str, user_message: str) -> str: ...
