"""Health check endpoint for infrastructure monitoring."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from nexafreight.database import get_db_session

router = APIRouter()
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    """Health check response schema."""

    status: str
    database: str
    version: str


@router.get("/", response_model=HealthResponse, tags=["health"])
@router.get("", response_model=HealthResponse, tags=["health"], include_in_schema=False)
async def health_check(db: AsyncSession = Depends(get_db_session)) -> HealthResponse:
    """Check application and database health.

    Returns 200 if app and database are healthy, 503 if database is unreachable.
    This endpoint requires no authentication and is safe to expose for monitoring.

    Args:
        db: Database session (dependency)

    Returns:
        HealthResponse with status and database connectivity

    Raises:
        HTTPException: 503 if database is unreachable
    """
    # Check database connectivity
    try:
        result = await db.execute(text("SELECT 1"))
        result.scalar()
        database_status = "connected"
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        raise HTTPException(
            status_code=503,
            detail={"status": "unhealthy", "database": "unreachable", "error": str(e)},
        ) from e

    return HealthResponse(
        status="healthy",
        database=database_status,
        version="0.1.0",  # From package __version__
    )
