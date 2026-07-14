"""Liveness, readiness, and service health endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from backend.api.schemas.health import ComponentHealthResponse, HealthResponse
from backend.application.container import ApplicationContainer
from backend.application.dependencies import get_container

router = APIRouter(prefix="/health", tags=["health"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


def _base_response(container: ApplicationContainer, state: str) -> HealthResponse:
    return HealthResponse(
        status=state,
        service=container.settings.app_name,
        version=container.settings.app_version,
        environment=container.settings.environment,
    )


@router.get("", response_model=HealthResponse, summary="Service health")
async def health(container: Container) -> HealthResponse:
    """Return service identity and process health without dependency I/O."""
    return _base_response(container, "up")


@router.get("/live", response_model=HealthResponse, summary="Liveness probe")
async def liveness(container: Container) -> HealthResponse:
    """Return success when the application process can serve requests."""
    return _base_response(container, "up")


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    summary="Readiness probe",
)
async def readiness(container: Container) -> HealthResponse | JSONResponse:
    """Check every configured dependency using bounded concurrent probes."""
    report = await container.health_service.readiness()
    response = HealthResponse(
        status="ready" if report.ready else "not_ready",
        service=container.settings.app_name,
        version=container.settings.app_version,
        environment=container.settings.environment,
        checks=[
            ComponentHealthResponse(
                name=check.name,
                status=check.status.value,
                latency_ms=check.latency_ms,
                detail=check.detail,
            )
            for check in report.checks
        ],
    )
    if report.ready:
        return response
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=response.model_dump(mode="json"),
    )
