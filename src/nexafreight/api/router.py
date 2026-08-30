"""Central API router aggregating all route modules."""

from __future__ import annotations

from fastapi import APIRouter

from nexafreight.api.routes import (
    alerts,
    analytics,
    auth,
    copilot,
    decisions,
    disruptions,
    health,
    shipments,
)
from nexafreight.api.routes import (
    map as map_routes,
)

# Central router that includes all sub-routers
api_router = APIRouter()

# Health check (available at /health and /health/)
api_router.include_router(
    health.router,
    prefix="/health",
    tags=["health"],
)

# Authentication and user management
api_router.include_router(
    auth.router,
    prefix="/auth",
    tags=["authentication"],
)

# Core business resources
api_router.include_router(
    shipments.router,
    prefix="/shipments",
    tags=["shipments"],
)

api_router.include_router(
    alerts.router,
    prefix="/alerts",
    tags=["alerts"],
)

api_router.include_router(
    disruptions.router,
    prefix="/disruptions",
    tags=["disruptions"],
)

api_router.include_router(
    decisions.router,
    prefix="/decisions",
    tags=["decisions"],
)

# Visualization and analytics
api_router.include_router(
    map_routes.router,
    prefix="/map",
    tags=["map"],
)

api_router.include_router(
    analytics.router,
    prefix="/analytics",
    tags=["analytics"],
)

# AI copilot
api_router.include_router(
    copilot.router,
    prefix="/copilot",
    tags=["copilot"],
)
