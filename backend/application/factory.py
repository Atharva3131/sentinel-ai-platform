"""FastAPI application factory and dependency lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import structlog
from dishka import Provider
from dishka.integrations.fastapi import setup_dishka
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.api.router import build_api_router
from backend.application.providers import build_root_container
from backend.configuration import AppSettings, get_settings
from backend.logging import configure_logging
from backend.middleware import ExceptionLoggingMiddleware, RequestContextMiddleware
from backend.telemetry import configure_telemetry


def create_application(
    settings: AppSettings | None = None,
    *,
    override_providers: Sequence[Provider] | None = None,
) -> FastAPI:
    """Create a fully composed application without performing network I/O."""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.logging)
    logger = structlog.get_logger(__name__)

    app = FastAPI(
        title="Sentinel AI Platform API",
        version=resolved_settings.app_version,
        debug=resolved_settings.debug,
        docs_url="/docs" if not resolved_settings.is_production else None,
        redoc_url=None,
        openapi_url="/openapi.json" if not resolved_settings.is_production else None,
    )
    app.add_middleware(ExceptionLoggingMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(build_api_router())

    telemetry = configure_telemetry(resolved_settings, app)

    root_container = build_root_container(
        resolved_settings,
        telemetry=telemetry,
        override_providers=tuple(override_providers or ()),
    )

    # Dishka's middleware must be installed before the application starts.
    setup_dishka(root_container, app)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with root_container as app_container:
            engine = await app_container.get(AsyncEngine)
            telemetry.instrument_sqlalchemy(engine)

            logger.info(
                "application_started",
                service=resolved_settings.app_name,
                version=resolved_settings.app_version,
                environment=resolved_settings.environment.value,
            )
            try:
                yield
            finally:
                await telemetry.shutdown()
                logger.info(
                    "application_stopped",
                    service=resolved_settings.app_name,
                )

    app.router.lifespan_context = lifespan

    return app