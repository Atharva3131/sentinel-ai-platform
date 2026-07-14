"""Liveness, readiness, and service health endpoints."""

from __future__ import annotations

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from backend.api.schemas.health import ComponentHealthResponse, HealthResponse
from backend.application.health import HealthService
from backend.configuration import AppSettings

router = APIRouter(prefix="/health", tags=["health"], route_class=DishkaRoute)


def _base_response(settings: AppSettings, state: str) -> HealthResponse:
    return HealthResponse(
        status=state,
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
    )


@router.get("", response_model=HealthResponse, summary="Service health")
async def health(settings: FromDishka[AppSettings]) -> HealthResponse:
    """Return service identity and process health without dependency I/O."""
    return _base_response(settings, "up")


@router.get("/live", response_model=HealthResponse, summary="Liveness probe")
async def liveness(settings: FromDishka[AppSettings]) -> HealthResponse:
    """Return success when the application process can serve requests."""
    return _base_response(settings, "up")


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": HealthResponse}},
    summary="Readiness probe",
)
async def readiness(
    settings: FromDishka[AppSettings],
    health_service: FromDishka[HealthService],
) -> HealthResponse | JSONResponse:
    """Check every configured dependency using bounded concurrent probes."""
    report = await health_service.readiness()
    response = HealthResponse(
        status="ready" if report.ready else "not_ready",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
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
