from fastapi import APIRouter

router = APIRouter(prefix="/api/shipments", tags=["shipments"])


@router.get("")
async def list_shipments():
    return {"status": "ok", "items": [], "total": 0, "page": 1}


@router.get("/{shipment_id}")
async def get_shipment(shipment_id: int):
    return {"status": "ok", "id": shipment_id}


@router.get("/{shipment_id}/route")
async def get_shipment_route(shipment_id: int):
    return {"status": "ok", "legs": []}


@router.get("/{shipment_id}/predict")
async def predict_shipment(shipment_id: int):
    return {"status": "ok", "message": "ML model not yet loaded"}


@router.get("/{shipment_id}/financials")
async def shipment_financials(shipment_id: int):
    return {"status": "ok"}


@router.get("/{shipment_id}/events")
async def shipment_events(shipment_id: int):
    return {"status": "ok", "items": []}
