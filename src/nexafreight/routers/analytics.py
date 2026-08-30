from fastapi import APIRouter

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/scorecard")
async def scorecard():
    return {"status": "ok"}


@router.get("/demand")
async def demand():
    return {"status": "ok", "items": []}


@router.get("/esg")
async def esg():
    return {"status": "ok"}


@router.get("/sla")
async def sla():
    return {"status": "ok"}


@router.get("/financial")
async def financial():
    return {"status": "ok"}
