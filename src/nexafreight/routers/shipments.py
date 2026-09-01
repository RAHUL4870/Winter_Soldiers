from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from nexafreight.database import get_db
from nexafreight.models.event import Event
from nexafreight.models.leg import Leg
from nexafreight.models.shipment import Shipment
from nexafreight.schemas.shipment import ShipmentDetail, ShipmentListItem

router = APIRouter(prefix="/api/shipments", tags=["shipments"])


@router.get("")
async def list_shipments(
    status: str | None = None,
    mode: str | None = None,
    page: int = 1,
    size: int = 20,
    db: AsyncSession = Depends(get_db),
):
    query = select(Shipment)

    if status:
        query = query.where(Shipment.status == status)
    if mode:
        query = query.where(Shipment.primary_mode == mode)

    result = await db.execute(query.offset((page - 1) * size).limit(size))
    shipments = result.scalars().all()

    # Count total
    count_result = await db.execute(select(Shipment))
    total = len(count_result.scalars().all())

    return {
        "items": [ShipmentListItem.from_orm(s) for s in shipments],
        "total": total,
        "page": page,
        "size": size,
    }


@router.get("/{shipment_id}")
async def get_shipment(
    shipment_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Shipment)
        .where(Shipment.id == shipment_id)
        .options(
            selectinload(Shipment.legs),
            selectinload(Shipment.orders),
        )
    )
    shipment = result.scalar_one_or_none()

    if not shipment:
        raise HTTPException(status_code=404, detail="Shipment not found")

    return ShipmentDetail.from_orm(shipment)


@router.get("/{shipment_id}/route")
async def get_shipment_route(
    shipment_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Leg).where(Leg.shipment_id == shipment_id).order_by(Leg.sequence)
    )
    legs = result.scalars().all()

    if not legs:
        raise HTTPException(status_code=404, detail="No legs found")

    features = []
    for leg in legs:
        if leg.route_geometry:
            features.append(
                {
                    "type": "Feature",
                    "geometry": leg.route_geometry,
                    "properties": {
                        "leg_id": leg.id,
                        "mode": leg.mode,
                        "sequence": leg.sequence,
                        "route_quality": leg.route_quality,
                        "provenance": leg.provenance,
                    },
                }
            )

    return {
        "type": "FeatureCollection",
        "features": features,
    }


@router.get("/{shipment_id}/events")
async def shipment_events(
    shipment_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Event).where(Event.shipment_id == shipment_id).order_by(Event.occurred_at.desc())
    )
    events = result.scalars().all()

    return {
        "items": [
            {
                "id": e.id,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at,
                "location_locode": e.location_locode,
                "description": e.description,
                "source": e.source,
            }
            for e in events
        ],
    }


@router.get("/{shipment_id}/predict")
async def predict_shipment(shipment_id: int):
    return {"status": "ok", "message": "ML model not yet loaded"}


@router.get("/{shipment_id}/financials")
async def shipment_financials(shipment_id: int):
    return {"status": "ok"}
