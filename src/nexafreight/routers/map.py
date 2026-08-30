from fastapi import APIRouter

router = APIRouter(prefix="/api/map", tags=["map"])


@router.get("/positions/snapshot")
async def positions_snapshot():
    return {"status": "ok", "positions": []}


@router.get("/routes")
async def all_routes():
    return {"status": "ok", "type": "FeatureCollection", "features": []}


@router.get("/ports")
async def all_ports():
    return {"status": "ok", "items": []}


@router.get("/feed-health")
async def feed_health():
    return {"status": "ok", "items": []}
