"""FastAPI dependency providers backed by the application lifespan."""

from dishka.async_container import AsyncContainer
from fastapi import Request

from backend.configuration import AppSettings


async def get_container(request: Request) -> AsyncContainer:
    """Return the request application's Dishka container."""
    container: AsyncContainer = request.app.state.dishka_container
    return container


async def get_settings(request: Request) -> AppSettings:
    """Return typed application settings through FastAPI dependency injection."""
    container = await get_container(request)
    return await container.get(AppSettings)
