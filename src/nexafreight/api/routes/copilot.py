"""AI Copilot endpoints (placeholder for T-025+)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.post("/ask")
async def ask() -> dict[str, object]:
    return {"status": "ok"}


@router.post("/narrate")
async def narrate() -> dict[str, object]:
    return {"status": "ok"}
