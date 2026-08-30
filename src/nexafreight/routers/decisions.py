from fastapi import APIRouter

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router.get("")
async def list_decisions():
    return {"status": "ok", "items": []}


@router.get("/{decision_id}")
async def get_decision(decision_id: int):
    return {"status": "ok", "id": decision_id}
