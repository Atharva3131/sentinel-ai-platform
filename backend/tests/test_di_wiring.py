"""DI wiring tests — categories 9 (PostgreSQL) and 10 (evaluator).

Verifies that:
  9.  PostgreSQL DI wiring — IncidentIngestionService + PostgreSQLIncidentRepository
      are resolvable from the Dishka container with correct types.
  10. Evaluator DI wiring — EvaluationEngine, EvaluationRegistry,
      InvestigationEvaluator are resolvable from the container.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from dishka import Provider, Scope, provide

from backend.application.factory import create_application
from backend.application.health import HealthService, ReadinessReport
from backend.configuration.settings import AppSettings, OpenTelemetrySettings
from backend.db.repositories.incident import PostgreSQLIncidentRepository
from backend.evaluation.engine import EvaluationEngine
from backend.evaluation.registry import EvaluationRegistry
from backend.interfaces.health import HealthCheckResult, HealthStatus
from backend.interfaces.llm import LLMProvider
from backend.models.evidence import EvidenceCollection
from backend.models.hypothesis import RootCauseAnalysis
from backend.models.incident import Incident, IncidentSeverity, IncidentStatus
from backend.services.evidence_normalizer import EvidenceNormalizer
from backend.services.evidence_orchestrator import EvidenceOrchestrationResult
from backend.services.incident_ingestion import IncidentIngestionService
from backend.services.incident_repository import InMemoryIncidentRepository
from backend.services.investigation_evaluation import InvestigationEvaluator
from backend.services.investigation_orchestrator import InvestigationResult

# ── Shared DI overrides ───────────────────────────────────────────────────


class _StubHealth(HealthService):
    def __init__(self) -> None:
        pass

    async def readiness(self) -> ReadinessReport:
        return ReadinessReport(
            ready=True,
            checks=(
                HealthCheckResult(
                    name="stub", status=HealthStatus.UP, latency_ms=0.0
                ),
            ),
        )


class _InMemoryRepo(PostgreSQLIncidentRepository):
    """Wraps InMemoryIncidentRepository behind PostgreSQLIncidentRepository for DI tests.

    Uses __new__ + __init_subclass__ bypass so no real AsyncSession is needed.
    All type overrides are annotated correctly to satisfy mypy strict mode.
    """

    def __init__(self, store: InMemoryIncidentRepository) -> None:
        # Bypass PostgreSQLIncidentRepository.__init__ (requires AsyncSession)
        self.__store = store

    async def save(self, incident: Incident) -> None:
        await self.__store.save(incident)

    async def get(self, incident_id: str) -> Incident | None:  # type: ignore[override]
        return await self.__store.get(incident_id)

    async def exists_by_correlation(
        self, correlation_id: str
    ) -> Incident | None:
        return await self.__store.exists_by_correlation(correlation_id)

    async def list_open(self) -> list[Incident]:
        return await self.__store.list_open()

    async def list_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        severity: str | None = None,
    ) -> list[Incident]:
        all_inc = self.__store.all()
        if status:
            all_inc = [i for i in all_inc if i.status.value == status]
        if severity:
            all_inc = [i for i in all_inc if i.severity.value == severity]
        return all_inc[offset : offset + limit]





class _BaseOverrideProvider(Provider):
    scope = Scope.APP

    @provide(scope=Scope.APP, override=True)
    def health_service(self) -> HealthService:
        return _StubHealth()

    @provide(scope=Scope.REQUEST, override=True)
    def incident_repository(self) -> PostgreSQLIncidentRepository:
        return _InMemoryRepo(InMemoryIncidentRepository())

    @provide(scope=Scope.REQUEST, override=True)
    def incident_ingestion_service(
        self, repository: PostgreSQLIncidentRepository
    ) -> IncidentIngestionService:
        return IncidentIngestionService(repository=repository)


def _test_settings() -> AppSettings:
    return AppSettings(
        environment="testing",
        neo4j={"enabled": False},
        opentelemetry=OpenTelemetrySettings(enabled=False),
    )


# ── 9. PostgreSQL DI wiring ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_postgres_incident_repository_resolvable_from_container() -> None:
    """PostgreSQLIncidentRepository is provided at REQUEST scope."""
    override = _BaseOverrideProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        container = app.state.dishka_container
        async with container() as request_container:
            repo = await request_container.get(PostgreSQLIncidentRepository)
            assert repo is not None


@pytest.mark.asyncio
async def test_incident_ingestion_service_resolvable_from_container() -> None:
    """IncidentIngestionService is provided at REQUEST scope."""
    override = _BaseOverrideProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        container = app.state.dishka_container
        async with container() as request_container:
            svc = await request_container.get(IncidentIngestionService)
            assert isinstance(svc, IncidentIngestionService)


@pytest.mark.asyncio
async def test_db_session_manager_resolvable_from_container() -> None:
    """DatabaseSessionManager is provided at APP scope."""
    from backend.db.session import DatabaseSessionManager
    override = _BaseOverrideProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        container = app.state.dishka_container
        manager = await container.get(DatabaseSessionManager)
        assert manager is not None


# ── 10. Evaluator DI wiring ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluation_registry_resolvable_from_container() -> None:
    """EvaluationRegistry is provided at APP scope with all 5 strategies."""
    override = _BaseOverrideProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        container = app.state.dishka_container
        registry = await container.get(EvaluationRegistry)
        assert isinstance(registry, EvaluationRegistry)
        assert "investigation_latency" in registry.names()
        assert "llm_token_usage" in registry.names()
        assert "tool_call_count" in registry.names()
        assert "evidence_utilization" in registry.names()
        assert "investigation_outcome" in registry.names()


@pytest.mark.asyncio
async def test_evaluation_engine_resolvable_from_container() -> None:
    """EvaluationEngine is provided at APP scope."""
    override = _BaseOverrideProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        container = app.state.dishka_container
        engine = await container.get(EvaluationEngine)
        assert isinstance(engine, EvaluationEngine)


@pytest.mark.asyncio
async def test_investigation_evaluator_resolvable_from_container() -> None:
    """InvestigationEvaluator is provided at APP scope."""
    override = _BaseOverrideProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        container = app.state.dishka_container
        evaluator = await container.get(InvestigationEvaluator)
        assert isinstance(evaluator, InvestigationEvaluator)


@pytest.mark.asyncio
async def test_investigation_evaluator_can_run_evaluation() -> None:
    """InvestigationEvaluator from DI container executes all 5 strategies."""
    override = _BaseOverrideProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        container = app.state.dishka_container
        evaluator = await container.get(InvestigationEvaluator)

    now = datetime.now(UTC)
    incident = Incident(
        incident_id=str(uuid.uuid4()),
        title="DI test",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        affected_services=("svc-a",),
        description="DI test.",
        detected_at=now,
        correlation_id=str(uuid.uuid4()),
    )
    rca = RootCauseAnalysis(
        rca_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        observed_symptoms=(),
        correlated_evidence_ids=(),
        candidate_hypotheses=(),
        evaluated_hypotheses=(),
        root_cause=None,
        confidence=0.5,
        unresolved_uncertainty=None,
        produced_at=now,
    )
    empty_coll = EvidenceCollection(
        incident_id=incident.incident_id,
        items=(),
        correlations=(),
        collected_at=now,
    )
    norm_coll = EvidenceNormalizer().normalize_collection(empty_coll)
    ev_result = EvidenceOrchestrationResult(
        collection=empty_coll,
        normalized=norm_coll,
        provider_errors={},
        partial=False,
        duration_ms=100.0,
    )
    inv_result = InvestigationResult(
        rca=rca,
        evidence_results=(ev_result,),
        iterations=1,
        inconclusive=True,
        cancelled=False,
        timed_out=False,
        duration_ms=2000.0,
        incident_id=incident.incident_id,
    )

    report = await evaluator.evaluate(incident, inv_result, latency_ms=2000.0)
    assert report is not None
    assert len(report.results) == 5


# ── LLM provider DI wiring ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_provider_resolvable_from_container() -> None:
    """LLMProvider is provided at APP scope (defaults to FakeLLMProvider)."""
    override = _BaseOverrideProvider()
    app = create_application(_test_settings(), override_providers=(override,))

    async with app.router.lifespan_context(app):
        container = app.state.dishka_container
        provider = await container.get(LLMProvider)
        assert isinstance(provider, LLMProvider)
        assert provider.name is not None
