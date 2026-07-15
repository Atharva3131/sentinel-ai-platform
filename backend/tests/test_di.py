"""Dishka integration tests."""

from __future__ import annotations

import httpx
import pytest
from dishka import Provider, Scope, provide

from backend.application.factory import create_application
from backend.application.health import HealthService, ReadinessReport
from backend.configuration.settings import AppSettings, OpenTelemetrySettings
from backend.interfaces.health import HealthCheckResult, HealthStatus
from backend.runtime import RuntimeFactory


class StubHealthService(HealthService):
    """Test double for readiness overrides."""

    def __init__(self) -> None:
        pass

    async def readiness(self) -> ReadinessReport:
        return ReadinessReport(
            ready=True,
            checks=(
                HealthCheckResult(
                    name="override",
                    status=HealthStatus.UP,
                    latency_ms=0.1,
                    detail=None,
                ),
            ),
        )


class OverrideProvider(Provider):
    """Testing override provider for app-scoped dependencies."""

    scope = Scope.APP

    @provide(scope=Scope.APP, override=True)
    def health_service(self) -> HealthService:
        return StubHealthService()


@pytest.mark.asyncio
async def test_override_provider_replaces_readiness_service() -> None:
    """The application factory must support Dishka testing overrides."""
    settings = AppSettings(
        environment="testing",
        neo4j={"enabled": False},
        opentelemetry=OpenTelemetrySettings(enabled=False),
    )
    app = create_application(settings, override_providers=(OverrideProvider(),))
    async with app.router.lifespan_context(app):
        runtime_factory = await app.state.dishka_container.get(RuntimeFactory)
        assert isinstance(runtime_factory, RuntimeFactory)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/health/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["checks"][0]["name"] == "override"
