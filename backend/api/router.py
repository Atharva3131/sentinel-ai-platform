"""Top-level API router composition."""

from fastapi import APIRouter

from backend.api.routers.health import router as health_router


def build_api_router() -> APIRouter:
    """Build the API router without constructing application services."""
    router = APIRouter()
    router.include_router(health_router)
    return router
