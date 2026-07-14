"""FastAPI dependency providers backed by the application lifespan."""

from fastapi import Request

from backend.application.container import ApplicationContainer
from backend.configuration import AppSettings


async def get_container(request: Request) -> ApplicationContainer:
    """Return the request application's dependency container."""
    container: ApplicationContainer = request.app.state.container
    return container


async def get_settings(request: Request) -> AppSettings:
    """Return typed application settings through FastAPI dependency injection."""
    container: ApplicationContainer = request.app.state.container
    return container.settings
