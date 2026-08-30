from fastapi import APIRouter

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("")
async def list_alerts():
    return {"status": "ok", "items": []}


@router.get("/{alert_id}")
async def get_alert(alert_id: int):
    return {"status": "ok", "id": alert_id}


@router.patch("/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: int):
    return {"status": "ACKNOWLEDGED"}


@router.get("/{alert_id}/options")
async def alert_options(alert_id: int):
    return {"status": "ok", "items": []}


@router.post("/{alert_id}/approve")
async def approve_alert(alert_id: int):
    return {"status": "ok", "message": "reroute engine not yet implemented"}
