"""Disruptions endpoints (placeholder for T-016+)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("")
@router.get("/")
async def list_disruptions() -> dict[str, object]:
    return {"status": "ok", "items": []}


@router.get("/{disruption_id}")
async def get_disruption(disruption_id: int | str) -> dict[str, object]:
    return {"status": "ok", "id": disruption_id}


@router.post("")
@router.post("/")
async def report_disruption() -> dict[str, object]:
    return {"status": "ok", "message": "created"}
