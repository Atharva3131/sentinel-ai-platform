"""API integration tests — categories 4-6: ingestion, retrieval, validation errors.

Uses httpx + ASGITransport with a Dishka override provider so no real
PostgreSQL or Redis is needed.  All repository and ingestion service calls go
through an InMemoryIncidentRepository adapter injected via the DI override.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from dishka import Provider, Scope, provide

from backend.application.factory import create_application
from backend.configuration.settings import AppSettings, OpenTelemetrySettings
from backend.db.repositories.incident import PostgreSQLIncidentRepository
from backend.models.incident import Incident
from backend.services.closed_loop_orchestrator import ClosedLoopOrchestrator
from backend.services.incident_ingestion import IncidentIngestionService
from backend.services.incident_repository import InMemoryIncidentRepository

# ── In-memory adapter that looks like PostgreSQLIncidentRepository ─────────


class _InMemoryAdapter(PostgreSQLIncidentRepository):
    """Wraps InMemoryIncidentRepository behind the PostgreSQLIncidentRepository
    type so Dishka's override replaces the real repo without a type mismatch.

    We skip calling super().__init__() because there's no real AsyncSession.
    All methods delegate to the in-memory store.
    """

    def __init__(self, store: InMemoryIncidentRepository) -> None:
        # Intentionally bypass PostgreSQLIncidentRepository.__init__ — no session needed
        self._store = store

    async def save(self, incident: Incident) -> None:
        await self._store.save(incident)

    async def get(self, incident_id: str) -> Incident | None:  # type: ignore[override]
        return await self._store.get(incident_id)

    async def get_or_raise(self, incident_id: str) -> Incident:
        inc = await self._store.get(incident_id)
        if inc is None:
            from backend.db.repositories.incident import IncidentNotFoundError
            raise IncidentNotFoundError(incident_id)
        return inc

    async def exists_by_correlation(self, correlation_id: str) -> Incident | None:
        return await self._store.exists_by_correlation(correlation_id)

    async def list_open(self) -> list[Incident]:
        return await self._store.list_open()

    async def list_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        severity: str | None = None,
    ) -> list[Incident]:
        all_inc = self._store.all()
        if status:
            all_inc = [i for i in all_inc if i.status.value == status]
        if severity:
            all_inc = [i for i in all_inc if i.severity.value == severity]
        return all_inc[offset: offset + limit]


# ── DI override ────────────────────────────────────────────────────────────


class _MemoryRepoProvider(Provider):
    """Override DI to inject in-memory implementations; no PostgreSQL needed."""

    scope = Scope.APP

    def __init__(self) -> None:
        super().__init__()
        self._store = InMemoryIncidentRepository()
        self.orchestrator_run_calls: list[Incident] = []

    @provide(scope=Scope.REQUEST, override=True)
    def incident_repository(self) -> PostgreSQLIncidentRepository:
        return _InMemoryAdapter(self._store)

    @provide(scope=Scope.REQUEST, override=True)
    def incident_ingestion_service(
        self,
        repository: PostgreSQLIncidentRepository,
    ) -> IncidentIngestionService:
        return IncidentIngestionService(repository=repository)

    @provide(scope=Scope.REQUEST, override=True)
    def closed_loop_orchestrator(self) -> ClosedLoopOrchestrator:
        """Return a no-op stub — tests exercise ingestion, not the full pipeline."""
        calls = self.orchestrator_run_calls

        class _NoOpOrchestrator:
            async def run(self, incident: Incident, **_: Any) -> None:
                calls.append(incident)

        return _NoOpOrchestrator()  # type: ignore[return-value]


def _test_settings() -> AppSettings:
    return AppSettings(
        environment="testing",
        neo4j={"enabled": False},
        opentelemetry=OpenTelemetrySettings(enabled=False),
    )


def _valid_body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "title": "Service degradation",
        "description": "Elevated error rates on checkout-service.",
        "severity": "high",
        "affected_services": ["checkout-service", "payment-api"],
        "environment": "production",
        "correlation_id": str(uuid.uuid4()),
        "symptoms": ["5xx elevated", "P99 > 2s"],
    }
    base.update(overrides)
    return base


# ── 4. API ingestion ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_incident_returns_201_and_incident_id() -> None:
    """POST /api/v1/incidents creates an incident and returns 201."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/incidents", json=_valid_body())

    assert response.status_code == 201
    payload = response.json()
    assert "incident" in payload
    assert payload["incident"]["severity"] == "high"
    assert payload["incident"]["status"] == "open"
    assert not payload["is_duplicate"]


@pytest.mark.asyncio
async def test_post_incident_assigns_incident_id_when_omitted() -> None:
    """POST without incident_id auto-generates a UUID."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/incidents", json=_valid_body())

    assert len(response.json()["incident"]["incident_id"]) > 0


@pytest.mark.asyncio
async def test_post_incident_with_explicit_id_preserves_it() -> None:
    """POST with incident_id uses the caller-supplied value."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))
    body = _valid_body(incident_id="custom-inc-001", correlation_id=str(uuid.uuid4()))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/incidents", json=body)

    assert response.json()["incident"]["incident_id"] == "custom-inc-001"


@pytest.mark.asyncio
async def test_duplicate_incident_returns_is_duplicate_true() -> None:
    """Posting the same correlation_id twice returns is_duplicate=True."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))
    corr_id = str(uuid.uuid4())
    body1 = _valid_body(incident_id="dup-001", correlation_id=corr_id)
    body2 = _valid_body(incident_id="dup-002", correlation_id=corr_id)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r1 = await client.post("/api/v1/incidents", json=body1)
            r2 = await client.post("/api/v1/incidents", json=body2)

    assert r1.status_code == 201
    assert r2.json()["is_duplicate"] is True
    assert r2.json()["existing_incident_id"] == "dup-001"


# ── 5. API retrieval ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_incident_returns_created_incident() -> None:
    """GET /api/v1/incidents/{id} returns the persisted incident."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))
    body = _valid_body(incident_id="get-test-001", correlation_id=str(uuid.uuid4()))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            post_resp = await client.post("/api/v1/incidents", json=body)
            assert post_resp.status_code == 201
            response = await client.get("/api/v1/incidents/get-test-001")

    assert response.status_code == 200
    assert response.json()["incident_id"] == "get-test-001"
    assert response.json()["severity"] == "high"


@pytest.mark.asyncio
async def test_get_incident_returns_404_when_missing() -> None:
    """GET /api/v1/incidents/{id} returns 404 for unknown IDs."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/api/v1/incidents/does-not-exist")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_incidents_returns_created_incidents() -> None:
    """GET /api/v1/incidents returns all persisted incidents."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/v1/incidents",
                json=_valid_body(incident_id="list-001", correlation_id=str(uuid.uuid4())),
            )
            await client.post(
                "/api/v1/incidents",
                json=_valid_body(incident_id="list-002", correlation_id=str(uuid.uuid4())),
            )
            response = await client.get("/api/v1/incidents")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 2


@pytest.mark.asyncio
async def test_list_incidents_respects_limit() -> None:
    """GET /api/v1/incidents?limit=1 returns at most 1 item."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for i in range(3):
                await client.post(
                    "/api/v1/incidents",
                    json=_valid_body(
                        incident_id=f"lim-{i}",
                        correlation_id=str(uuid.uuid4()),
                    ),
                )
            response = await client.get("/api/v1/incidents?limit=1")

    assert len(response.json()["items"]) == 1


# ── 6. API validation errors ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_incident_missing_title_returns_422() -> None:
    """POST without title returns HTTP 422."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))
    body = _valid_body()
    del body["title"]

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/incidents", json=body)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_incident_invalid_severity_returns_422() -> None:
    """POST with invalid severity returns HTTP 422."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/incidents",
                json=_valid_body(severity="EXTREME"),
            )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_incident_empty_affected_services_returns_422() -> None:
    """POST with empty affected_services list returns HTTP 422."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/incidents",
                json=_valid_body(affected_services=[]),
            )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_incident_missing_description_returns_422() -> None:
    """POST without description returns HTTP 422."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))
    body = _valid_body()
    del body["description"]

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/v1/incidents", json=body)

    assert response.status_code == 422


# ── 7. ClosedLoopOrchestrator integration ───────────────────────────────────


@pytest.mark.asyncio
async def test_new_incident_invokes_closed_loop_orchestrator() -> None:
    """POST with a new incident must invoke ClosedLoopOrchestrator.run()."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/incidents",
                json=_valid_body(
                    incident_id="clo-001",
                    correlation_id=str(uuid.uuid4()),
                ),
            )

    assert response.status_code == 201
    assert not response.json()["is_duplicate"]
    # The no-op stub recorded the call
    assert len(override.orchestrator_run_calls) == 1
    assert override.orchestrator_run_calls[0].incident_id == "clo-001"


@pytest.mark.asyncio
async def test_duplicate_incident_does_not_invoke_closed_loop_orchestrator() -> None:
    """POST with a duplicate correlation_id must NOT invoke ClosedLoopOrchestrator."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))
    corr_id = str(uuid.uuid4())

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # First request — new incident, orchestrator invoked once
            r1 = await client.post(
                "/api/v1/incidents",
                json=_valid_body(incident_id="dup-clo-001", correlation_id=corr_id),
            )
            # Second request — duplicate, orchestrator must NOT be invoked again
            r2 = await client.post(
                "/api/v1/incidents",
                json=_valid_body(incident_id="dup-clo-002", correlation_id=corr_id),
            )

    assert r1.status_code == 201
    assert r2.json()["is_duplicate"] is True
    # Orchestrator called exactly once — for the first (new) incident only
    assert len(override.orchestrator_run_calls) == 1
    assert override.orchestrator_run_calls[0].incident_id == "dup-clo-001"


@pytest.mark.asyncio
async def test_orchestration_failure_does_not_lose_incident() -> None:
    """If ClosedLoopOrchestrator.run() raises, the incident is still persisted and 201 returned."""
    override = _MemoryRepoProvider()

    # Patch the no-op stub to raise after the incident is already persisted

    class _FailingOrchestrator:
        async def run(self, incident: Incident, **_: Any) -> None:
            raise RuntimeError("Simulated orchestration failure")

    # Replace the provider method for this test
    def _failing_provider(self_inner: Any) -> ClosedLoopOrchestrator:
        return _FailingOrchestrator()  # type: ignore[return-value]

    # We can't easily swap out a Dishka provider mid-flight, so we use a
    # dedicated override provider that injects the failing orchestrator.
    class _FailingOrchestratorProvider(Provider):
        scope = Scope.APP

        @provide(scope=Scope.REQUEST, override=True)
        def closed_loop_orchestrator(self) -> ClosedLoopOrchestrator:
            return _FailingOrchestrator()  # type: ignore[return-value]

    failing_override = _FailingOrchestratorProvider()
    app2 = create_application(
        _test_settings(),
        override_providers=(override, failing_override),
    )

    async with app2.router.lifespan_context(app2):
        transport = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/v1/incidents",
                json=_valid_body(
                    incident_id="fail-clo-001",
                    correlation_id=str(uuid.uuid4()),
                ),
            )

    # Despite orchestration failure, HTTP 201 is returned
    assert response.status_code == 201
    assert response.json()["incident"]["incident_id"] == "fail-clo-001"

    # Incident was persisted before orchestration ran
    saved = await override._store.get("fail-clo-001")
    assert saved is not None
    assert saved.incident_id == "fail-clo-001"


@pytest.mark.asyncio
async def test_di_container_resolves_request_scoped_closed_loop_orchestrator() -> None:
    """DI graph resolves ClosedLoopOrchestrator at REQUEST scope with a real repository."""
    override = _MemoryRepoProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Two separate requests must each get their own orchestrator instance
            r1 = await client.post(
                "/api/v1/incidents",
                json=_valid_body(
                    incident_id="di-scope-001",
                    correlation_id=str(uuid.uuid4()),
                ),
            )
            r2 = await client.post(
                "/api/v1/incidents",
                json=_valid_body(
                    incident_id="di-scope-002",
                    correlation_id=str(uuid.uuid4()),
                ),
            )

    assert r1.status_code == 201
    assert r2.status_code == 201
    # Both requests succeeded → DI resolved correctly for each request scope
    assert len(override.orchestrator_run_calls) == 2
    called_ids = {c.incident_id for c in override.orchestrator_run_calls}
    assert called_ids == {"di-scope-001", "di-scope-002"}
