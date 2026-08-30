"""Decisions endpoints (placeholder for T-019+)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("")
@router.get("/")
async def list_decisions() -> dict[str, object]:
    return {"status": "ok", "items": []}


@router.get("/{decision_id}")
async def get_decision(decision_id: int | str) -> dict[str, object]:
    return {"status": "ok", "id": decision_id}
