"""Top-level API router composition."""

from fastapi import APIRouter

from backend.api.routers.health import router as health_router
from backend.api.routers.incidents import router as incidents_router
from backend.api.routers.metrics import router as metrics_router


def build_api_router() -> APIRouter:
    """Build the API router without constructing application services."""
    router = APIRouter()
    router.include_router(health_router)
    router.include_router(metrics_router)
    router.include_router(incidents_router, prefix="/api/v1")
    return router
