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

    Shutdown:
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

    logger.info("Application startup complete")

    # Yield control during application runtime
    yield

    # Shutdown
    logger.info("Shutting down NexaFreight Control Tower")

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
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
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
