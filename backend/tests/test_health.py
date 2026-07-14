"""Health endpoint smoke tests."""

import httpx
import pytest

from backend.application.factory import create_application
from backend.configuration.settings import AppSettings, OpenTelemetrySettings


@pytest.mark.asyncio
async def test_liveness_returns_service_metadata() -> None:
    """Liveness must not require an external dependency connection."""
    settings = AppSettings(
        environment="testing",
        neo4j={"enabled": False},
        opentelemetry=OpenTelemetrySettings(enabled=False),
    )
    app = create_application(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "up"
    assert response.headers["X-Correlation-ID"]
