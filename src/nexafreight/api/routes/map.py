"""Map visualization endpoints (placeholder for T-023+)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/positions/snapshot")
async def positions_snapshot() -> dict[str, object]:
    return {"status": "ok", "items": []}


@router.get("/corridors")
async def map_corridors() -> dict[str, object]:
    return {"status": "ok", "items": []}
