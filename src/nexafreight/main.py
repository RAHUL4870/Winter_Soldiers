"""FastAPI application factory and entrypoint for NexaFreight Control Tower."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import email_validator
from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from nexafreight.api.router import api_router
from nexafreight.api.routes import health
from nexafreight.api.routes.health import HealthResponse
from nexafreight.config import Settings, ensure_directories, get_settings
from nexafreight.database import create_engine, dispose_engine, get_engine
from nexafreight.exceptions import NexaFreightException
from nexafreight.logging import configure_logging
from nexafreight.workers.ais_listener import get_position_tracker, get_worker
from nexafreight.workers.position_interpolator import get_interpolator_worker

email_validator.TEST_ENVIRONMENT = True
email_validator.SPECIAL_USE_DOMAIN_NAMES = []

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan: startup and shutdown logic.

    Startup:
    - Initialize logging configuration
    - Ensure required directories exist
    - Verify database connectivity
    - Start AIS listener worker and populate position tracker
    - Start position interpolator worker

    Shutdown:
    - Stop position interpolator worker
    - Stop AIS listener worker
    - Dispose database engine cleanly

    Args:
        app: FastAPI application instance

    Yields:
        Control during application runtime
    """
    # Startup
    settings: Settings = getattr(app.state, "settings", None) or get_settings()

    # Configure logging first, before any other startup activity
    configure_logging(settings)
    logger.info(f"Starting NexaFreight Control Tower (env={settings.environment})")

    # Ensure required directories exist
    try:
        ensure_directories()
        logger.info("Application directories verified")
    except Exception as e:
        logger.error(f"Failed to create required directories: {e}")
        raise

    # Verify database connectivity
    engine = None
    try:
        engine = get_engine() if (settings == get_settings()) else create_engine(settings)
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info(f"Database connectivity verified: {settings.database_url}")
    except Exception as e:
        logger.error(f"Database connectivity check failed: {e}")
        raise RuntimeError(f"Cannot connect to database: {e}") from e

    # Initialize position tracker singleton and start AIS listener (T-029).
    try:
        tracker = get_position_tracker()
        worker = get_worker()
        await worker.start(poll_interval_s=5.0)
        if worker.adapter is not None:
            tracker.adapter = worker.adapter
            logger.info("AIS listener worker started; adapter initialized.")
        else:
            logger.warning("AIS listener worker started in no-feed mode; no adapter active.")
    except Exception as exc:
        logger.error(
            f"Failed to start AIS listener worker: {exc}",
            exc_info=True,
        )

    # Start position interpolator worker (T-030).
    try:
        interpolator = get_interpolator_worker()
        await interpolator.start()
        logger.info("Position interpolator worker started.")
    except Exception as exc:
        logger.error(
            f"Failed to start position interpolator worker: {exc}",
            exc_info=True,
        )

    logger.info("Application startup complete")

    # Yield control during application runtime
    yield

    # Shutdown
    logger.info("Shutting down NexaFreight Control Tower")

    # Stop position interpolator worker (T-030 cleanup).
    try:
        interpolator = get_interpolator_worker()
        await interpolator.stop()
        logger.info("Position interpolator worker shut down.")
    except Exception as exc:
        logger.warning(f"Error during position interpolator worker shutdown: {exc}")

    # Stop AIS listener worker (T-029 cleanup).
    try:
        worker = get_worker()
        await worker.stop()
        logger.info("AIS listener worker shut down.")
    except Exception as exc:
        logger.warning(f"Error during AIS listener worker shutdown: {exc}")

    try:
        if engine is not None:
            await dispose_engine(engine)
        else:
            await dispose_engine()
        logger.info("Database engine disposed")
    except Exception as e:
        logger.error(f"Error during engine disposal: {e}")

    logger.info("Shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory: construct configured FastAPI instance.

    Args:
        settings: Application settings (defaults to cached singleton if None).
                 Tests can provide custom settings to avoid global state mutation.

    Returns:
        Configured FastAPI application ready to serve requests.
    """
    if settings is None:
        settings = get_settings()

    # Create FastAPI app with lifespan
    app = FastAPI(
        title="NexaFreight Control Tower API",
        version="0.1.0",
        description="Real-time shipment tracking and disruption management",
        lifespan=lifespan,
    )

    # Store settings on app state for lifespan access
    app.state.settings = settings

    # Configure CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,  # Required for JWT in Authorization header
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register exception handlers
    @app.exception_handler(NexaFreightException)
    async def nexafreight_exception_handler(
        request: Request,
        exc: NexaFreightException,
    ) -> JSONResponse:
        """Handle application-level exceptions with clean JSON responses.

        Args:
            request: Incoming request
            exc: Application exception

        Returns:
            JSON error response with appropriate status code
        """
        logger.warning(
            f"Application error: {exc.message} (status={exc.status_code}, path={request.url.path})"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.message,
                "details": exc.details,
            },
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        """Handle unexpected exceptions with safe, generic error response.

        Logs full error details server-side but returns generic message
        to client to avoid leaking internal implementation details.

        Args:
            request: Incoming request
            exc: Unhandled exception

        Returns:
            Generic 500 error response
        """
        logger.error(
            f"Unhandled exception: {exc}",
            exc_info=True,  # Include full stack trace in logs
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Internal server error",
                "details": {},
            },
        )

    # Include central API router
    app.include_router(api_router, prefix="/api")

    # Top-level health check endpoint for /health and /health/
    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["health"],
        include_in_schema=False,
    )
    @app.get(
        "/health/",
        response_model=HealthResponse,
        tags=["health"],
        include_in_schema=False,
    )
    async def root_health_check(
        health_resp: HealthResponse = Depends(health.health_check),
    ) -> HealthResponse:
        return health_resp

    return app


# Module-level app instance for ASGI server (Uvicorn entrypoint)
app = create_app()
