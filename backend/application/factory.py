"""FastAPI application factory and dependency lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from backend.api.router import build_api_router
from backend.application.container import ApplicationContainer
from backend.configuration import AppSettings, get_settings
from backend.logging import configure_logging
from backend.middleware import RequestContextMiddleware
from backend.telemetry import TelemetryHandle, configure_telemetry


def create_application(settings: AppSettings | None = None) -> FastAPI:
    """Create a fully composed application without performing network I/O."""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.logging)
    logger = structlog.get_logger(__name__)
    telemetry: TelemetryHandle | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = ApplicationContainer.build(resolved_settings)
        app.state.container = container
        if telemetry is not None:
            telemetry.instrument_sqlalchemy(container.sqlalchemy_engine)
        logger.info(
            "application_started",
            service=resolved_settings.app_name,
            version=resolved_settings.app_version,
            environment=resolved_settings.environment,
        )
        try:
            yield
        finally:
            await container.close()
            if telemetry is not None:
                await telemetry.shutdown()
            logger.info("application_stopped", service=resolved_settings.app_name)

    app = FastAPI(
        title="Sentinel AI Platform API",
        version=resolved_settings.app_version,
        debug=resolved_settings.debug,
        docs_url="/docs" if not resolved_settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not resolved_settings.is_production else None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    app.include_router(build_api_router())
    telemetry = configure_telemetry(resolved_settings, app)
    return app
