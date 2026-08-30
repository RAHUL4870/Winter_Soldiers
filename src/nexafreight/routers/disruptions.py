from fastapi import APIRouter

router = APIRouter(prefix="/api/disruptions", tags=["disruptions"])


@router.get("")
async def list_disruptions():
    return {"status": "ok", "items": []}


@router.post("")
async def create_disruption():
    return {"status": "ok", "message": "disruption detection not yet implemented"}


@router.get("/{disruption_id}")
async def get_disruption(disruption_id: int):
    return {"status": "ok", "id": disruption_id}
