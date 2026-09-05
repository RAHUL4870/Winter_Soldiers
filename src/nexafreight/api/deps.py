"""
FastAPI dependency for ML model registry injection (T-039).

Usage in endpoints:
    from nexafreight.api.deps import get_registry

    @router.post("/predict/delay")
    async def predict_delay(
        body: DelayPredictionRequest,
        registry: ModelRegistry = Depends(get_registry),
    ):
        ...
"""
from __future__ import annotations

from fastapi import Depends, Request
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from nexafreight.ml.registry import ModelRegistry


async def get_registry(request: Request) -> ModelRegistry:
    """Retrieve the ModelRegistry singleton from app state.

    Returns 503 if the registry was not initialised during startup
    (e.g. model artifacts are missing).
    """
    registry: ModelRegistry | None = getattr(
        request.app.state, "ml_registry", None
    )
    if registry is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "MODELS_NOT_LOADED",
                "message": "ML models are not available. Check server startup logs.",
                "provenance": "SYSTEM",
            },
        )
    return registry
