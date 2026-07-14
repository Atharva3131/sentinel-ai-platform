"""FastAPI dependency providers backed by the application lifespan."""

from fastapi import Request

from backend.application.container import ApplicationContainer


async def get_container(request: Request) -> ApplicationContainer:
    """Return the request application's dependency container."""
    container: ApplicationContainer = request.app.state.container
    return container
