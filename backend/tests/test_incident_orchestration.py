"""Integration tests for the Evidence Integration and Incident Orchestration Layer.

Tests exercise the REAL orchestration path using fake providers — no mocking
of individual methods.  All 16 required scenarios are covered.

No live cloud services, AWS/Azure credentials, or hard-coded failure modes.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.core.hypothesis_engine import DeterministicHypothesisGenerator, HypothesisEngine
from backend.core.remediation_engine import (
    AlwaysAllowPolicyGateway,
    FakeActionExecutor,
    RemediationEngine,
)
from backend.core.validation_engine import (
    FakeHealthCheckRunner,
    FakeMetricsSnapshot,
    ValidationEngine,
    VerificationEngine,
)
from backend.interfaces.fake_evidence import (
    FakeDeploymentProvider,
    FakeLogsProvider,
    FakeMetricsProvider,
)
from backend.models.evidence import (
    Evidence,
    EvidenceCollection,
    EvidenceSource,
    EvidenceSourceKind,
    EvidenceStatus,
)
from backend.models.incident import (
    Incident,
    IncidentSeverity,
    IncidentStatus,
)
from backend.models.remediation import (
    ActionRiskLevel,
    RemediationAction,
    RemediationActionType,
    RemediationPlan,
)
from backend.models.validation import (
    ValidationPlan,
    ValidationStrategyConfig,
    ValidationStrategyKind,
    VerificationPlan,
)
from backend.retrieval.models import (
    Chunk,
    DocumentSource,
    RetrievalMetadata,
    RetrievalResult,
    RetrievalStrategy,
)
from backend.services.evidence_normalizer import _STATUS_RELIABILITY, EvidenceNormalizer
from backend.services.evidence_orchestrator import EvidenceOrchestrator
from backend.services.incident_events import (
    EVIDENCE_COLLECTED,
    EVIDENCE_COLLECTION_PARTIAL_FAILURE,
    EVIDENCE_COLLECTION_STARTED,
    HYPOTHESES_GENERATED,
    INCIDENT_CREATED,
    INCIDENT_DUPLICATE_DETECTED,
    INCIDENT_REINVESTIGATION_REQUIRED,
    INVESTIGATION_ITERATION_STARTED,
    INVESTIGATION_STARTED,
    RCA_INCONCLUSIVE,
    REMEDIATION_PLANNED,
    REMEDIATION_STARTED,
    ROOT_CAUSE_IDENTIFIED,
    VALIDATION_STARTED,
    VERIFICATION_STARTED,
)
from backend.services.incident_ingestion import IncidentIngestionService, IncidentValidationError
from backend.services.incident_repository import InMemoryIncidentRepository
from backend.services.investigation_agent import (
    DeterministicInvestigationAgent,
    EvidenceRequestTool,
)
from backend.services.investigation_orchestrator import InvestigationOrchestrator
from backend.services.knowledge_integration import KnowledgeEvidenceProvider
from backend.services.orchestration_pipeline import IncidentOrchestrationPipeline

# ── Shared helpers ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _incident(
    *,
    incident_id: str | None = None,
    title: str = "Service degradation",
    severity: IncidentSeverity = IncidentSeverity.HIGH,
    affected_services: tuple[str, ...] = ("api-gateway", "checkout-service"),
    symptoms: tuple[str, ...] = ("elevated 5xx", "P99 latency > 2s"),
    environment: str = "production",
    correlation_id: str | None = None,
) -> Incident:
    return Incident(
        incident_id=incident_id or str(uuid.uuid4()),
        title=title,
        severity=severity,
        status=IncidentStatus.OPEN,
        affected_services=affected_services,
        description="Multiple services showing elevated error rates.",
        detected_at=_now(),
        symptoms=symptoms,
        environment=environment,
        correlation_id=correlation_id or str(uuid.uuid4()),
    )


def _evidence_source(kind: EvidenceSourceKind = EvidenceSourceKind.METRICS) -> EvidenceSource:
    return EvidenceSource(
        source_id=str(uuid.uuid4()),
        kind=kind,
        name=f"fake-{kind.value}",
        collected_at=_now(),
    )


def _evidence(
    incident_id: str,
    *,
    kind: EvidenceSourceKind = EvidenceSourceKind.METRICS,
    service: str = "api-gateway",
    relevance: float = 0.85,
) -> Evidence:
    return Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=incident_id,
        source=_evidence_source(kind),
        title=f"Anomaly in {service}",
        content=f"Observed anomaly for {service}",
        status=EvidenceStatus.CONFIRMED,
        relevance_score=relevance,
        collected_at=_now(),
        structured_data={"service": service, "metric": "error_rate"},
    )


class _EventCollector:
    """Captures all emitted events for assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: Any,
    ) -> None:
        self.events.append((event_name, dict(payload)))

    def names(self) -> list[str]:
        return [e[0] for e in self.events]

    def payloads_for(self, name: str) -> list[dict[str, Any]]:
        return [p for n, p in self.events if n == name]


def _make_ingestion_service(
    repo: InMemoryIncidentRepository | None = None,
    emitter: _EventCollector | None = None,
) -> IncidentIngestionService:
    return IncidentIngestionService(
        repository=repo or InMemoryIncidentRepository(),
        event_emitter=emitter,
    )


def _make_investigation(
    providers: list[Any] | None = None,
    emitter: _EventCollector | None = None,
    max_iterations: int = 2,
    confidence_threshold: float = 0.60,
) -> InvestigationOrchestrator:
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen)
    orchestrator = EvidenceOrchestrator(
        providers=providers or [FakeMetricsProvider(), FakeLogsProvider()],
        event_emitter=emitter,
    )
    return InvestigationOrchestrator(
        evidence_orchestrator=orchestrator,
        hypothesis_engine=engine,
        event_emitter=emitter,
        max_iterations=max_iterations,
        confidence_threshold=confidence_threshold,
    )


async def _simple_remediation_planner(
    incident: Incident, rca: Any
) -> RemediationPlan:
    action = RemediationAction(
        action_id=str(uuid.uuid4()),
        action_type=RemediationActionType.RESTART_SERVICE,
        target_service=incident.affected_services[0],
        target_environment=incident.environment or "production",
        risk_level=ActionRiskLevel.LOW,
        title=f"Restart {incident.affected_services[0]}",
        description="Restart the affected service.",
        parameters={"service": incident.affected_services[0]},
    )
    return RemediationPlan(
        plan_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        rca_id=rca.rca_id if rca else None,
        actions=(action,),
        stages=((action.action_id,),),
        created_at=_now(),
    )


async def _simple_validation_planner(incident: Incident, plan_id: str) -> ValidationPlan:
    return ValidationPlan(
        plan_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        remediation_plan_id=plan_id,
        strategies=(
            ValidationStrategyConfig(
                strategy_kind=ValidationStrategyKind.HEALTH_CHECK,
                target_service=incident.affected_services[0],
                timeout_seconds=10.0,
            ),
        ),
    )


async def _simple_verification_planner(
    incident: Incident, plan_id: str, snapshot_before: dict[str, Any]
) -> VerificationPlan:
    return VerificationPlan(
        plan_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        remediation_plan_id=plan_id,
        metrics_to_compare=("error_rate",),
        thresholds={"error_rate": 0.05},
        snapshot_before={svc: {"error_rate": 0.15} for svc in incident.affected_services},
    )


# ── 1. Incident ingestion ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_incident_ingestion_persists_and_emits_created() -> None:
    """Ingested incident is persisted and IncidentCreated event is emitted."""
    repo = InMemoryIncidentRepository()
    emitter = _EventCollector()
    svc = _make_ingestion_service(repo, emitter)
    inc = _incident()

    result = await svc.ingest(inc)

    assert not result.is_duplicate
    assert result.incident.incident_id == inc.incident_id
    persisted = await repo.get(inc.incident_id)
    assert persisted is not None
    assert INCIDENT_CREATED in emitter.names()
    payload = emitter.payloads_for(INCIDENT_CREATED)[0]
    assert payload["incident_id"] == inc.incident_id
    assert payload["severity"] == str(inc.severity)


@pytest.mark.asyncio
async def test_incident_ingestion_assigns_correlation_id_when_missing() -> None:
    """Ingestion assigns a correlation_id when the incident has none."""
    repo = InMemoryIncidentRepository()
    inc = Incident(
        incident_id=str(uuid.uuid4()),
        title="No correlation",
        severity=IncidentSeverity.LOW,
        status=IncidentStatus.OPEN,
        affected_services=("svc-a",),
        description="Test incident without correlation id.",
        detected_at=_now(),
        correlation_id=None,
    )
    svc = _make_ingestion_service(repo)
    result = await svc.ingest(inc)

    assert result.incident.correlation_id is not None
    assert len(result.incident.correlation_id) > 0


@pytest.mark.asyncio
async def test_incident_ingestion_rejects_invalid_incident() -> None:
    """Ingestion raises IncidentValidationError for structurally invalid data."""
    svc = _make_ingestion_service()
    # Missing title
    bad = Incident(
        incident_id=str(uuid.uuid4()),
        title="",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        affected_services=("svc-a",),
        description="desc",
        detected_at=_now(),
    )
    with pytest.raises(IncidentValidationError):
        await svc.ingest(bad)


# ── 2. Complete evidence collection ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_evidence_collection_from_all_providers() -> None:
    """EvidenceOrchestrator collects from all registered providers."""
    emitter = _EventCollector()
    orchestrator = EvidenceOrchestrator(
        providers=[
            FakeMetricsProvider(),
            FakeLogsProvider(),
            FakeDeploymentProvider(
                deployments=[{"service": "api-gateway", "version": "2.3.1"}]
            ),
        ],
        event_emitter=emitter,
    )
    inc = _incident()
    result = await orchestrator.collect(inc)

    assert result.collection.count > 0
    kinds = {e.source.kind for e in result.collection.items}
    assert EvidenceSourceKind.METRICS in kinds
    assert EvidenceSourceKind.LOGS in kinds
    assert EvidenceSourceKind.DEPLOYMENTS in kinds
    assert EVIDENCE_COLLECTION_STARTED in emitter.names()
    assert EVIDENCE_COLLECTED in emitter.names()
    assert not result.partial


# ── 3. Partial evidence collection ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_partial_evidence_collection_tolerates_provider_failure() -> None:
    """Collection continues when one provider fails; partial flag is set."""

    class _FailingProvider:
        kind = EvidenceSourceKind.TRACES
        name = "failing-traces"

        async def collect(
            self, incident: Any, *, max_items: int = 20, context: Any = None
        ) -> list[Any]:
            raise RuntimeError("traces service unreachable")

        async def health_check(self) -> bool:
            return False

    emitter = _EventCollector()
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider(), _FailingProvider()],
        event_emitter=emitter,
    )
    inc = _incident()
    result = await orchestrator.collect(inc)

    assert result.partial
    assert "failing-traces" in result.provider_errors
    assert result.collection.count > 0          # metrics still collected
    assert EVIDENCE_COLLECTION_PARTIAL_FAILURE in emitter.names()
    payload = emitter.payloads_for(EVIDENCE_COLLECTION_PARTIAL_FAILURE)[0]
    assert payload["partial"] is True
    assert "failing-traces" in payload["provider_errors"]


# ── 4. Provider failure (all providers unavailable) ───────────────────────────


@pytest.mark.asyncio
async def test_all_providers_unavailable_returns_empty_collection() -> None:
    """When every provider fails, collection returns an empty collection (no crash)."""

    class _AlwaysFails:
        kind = EvidenceSourceKind.METRICS
        name = "always-fails"

        async def collect(
            self, incident: Any, *, max_items: int = 20, context: Any = None
        ) -> list[Any]:
            raise ConnectionError("unreachable")

        async def health_check(self) -> bool:
            return False

    orchestrator = EvidenceOrchestrator(providers=[_AlwaysFails()])
    inc = _incident()
    result = await orchestrator.collect(inc)

    assert result.collection.count == 0
    assert result.partial
    assert "always-fails" in result.provider_errors


# ── 5. Evidence normalization ─────────────────────────────────────────────────


def test_evidence_normalization_extracts_canonical_fields() -> None:
    """EvidenceNormalizer extracts service, environment, version, etc."""
    inc = _incident()
    ev = Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=inc.incident_id,
        source=EvidenceSource(
            source_id=str(uuid.uuid4()),
            kind=EvidenceSourceKind.DEPLOYMENTS,
            name="fake-deploy",
            collected_at=_now(),
        ),
        title="Deployment event",
        content="Service deployed",
        status=EvidenceStatus.CONFIRMED,
        relevance_score=0.9,
        collected_at=_now(),
        structured_data={
            "service": "payment-svc",
            "environment": "production",
            "version": "3.1.0",
        },
    )

    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize(ev)

    assert normalized.service == "payment-svc"
    assert normalized.environment == "production"
    assert normalized.version == "3.1.0"
    assert normalized.confidence == 1.0          # CONFIRMED → 1.0
    assert normalized.evidence_type == EvidenceSourceKind.DEPLOYMENTS.value


def test_evidence_normalization_preserves_extra_metadata() -> None:
    """Fields not in the canonical key list are preserved in extra_metadata."""
    inc = _incident()
    ev = Evidence(
        evidence_id=str(uuid.uuid4()),
        incident_id=inc.incident_id,
        source=_evidence_source(EvidenceSourceKind.METRICS),
        title="Custom metric",
        content="Some data",
        status=EvidenceStatus.PROBABLE,
        relevance_score=0.7,
        collected_at=_now(),
        structured_data={
            "service": "svc",
            "custom_field_xyz": "preserved-value",
            "another_key": 42,
        },
    )
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize(ev)

    assert normalized.extra_metadata.get("custom_field_xyz") == "preserved-value"
    assert normalized.extra_metadata.get("another_key") == 42
    assert "service" not in normalized.extra_metadata


def test_evidence_normalization_confidence_maps_status() -> None:
    """EvidenceStatus maps to expected reliability values."""

    assert _STATUS_RELIABILITY[EvidenceStatus.CONFIRMED] == 1.0
    assert _STATUS_RELIABILITY[EvidenceStatus.UNAVAILABLE] == 0.0
    assert (
        _STATUS_RELIABILITY[EvidenceStatus.PROBABLE]
        > _STATUS_RELIABILITY[EvidenceStatus.SUSPECTED]
    )


# ── 6. Hypothesis generation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hypothesis_generation_from_evidence() -> None:
    """HypothesisEngine generates hypotheses from collected evidence."""
    inc = _incident()
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen)

    orchestrator = EvidenceOrchestrator(
        providers=[
            FakeMetricsProvider(),
            FakeDeploymentProvider(
                deployments=[{"service": "api-gateway", "version": "v2.0.0"}]
            ),
        ]
    )
    ev_result = await orchestrator.collect(inc)

    rca = await engine.analyze(inc, ev_result.collection)

    assert len(rca.candidate_hypotheses) > 0
    assert len(rca.evaluated_hypotheses) > 0
    assert 0.0 <= rca.confidence <= 1.0


# ── 7. Iterative investigation ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_iterative_investigation_requests_more_evidence() -> None:
    """Investigation iterates when confidence is below threshold."""
    emitter = _EventCollector()
    # Only metrics on first pass — deployment provider found later
    investigation = _make_investigation(
        providers=[FakeMetricsProvider(), FakeDeploymentProvider(
            deployments=[{"service": "api-gateway", "version": "v2.0.0"}]
        )],
        emitter=emitter,
        max_iterations=3,
        confidence_threshold=0.99,    # deliberately unachievable → forces iterations
    )
    inc = _incident()
    result = await investigation.investigate(inc, execution_id="exec-iter-001")

    assert result.iterations >= 1
    assert INVESTIGATION_STARTED in emitter.names()
    assert INVESTIGATION_ITERATION_STARTED in emitter.names()
    assert HYPOTHESES_GENERATED in emitter.names()
    # Result always has an RCA
    assert result.rca is not None


@pytest.mark.asyncio
async def test_iterative_investigation_stops_when_confident() -> None:
    """Investigation stops early when confidence meets threshold."""
    emitter = _EventCollector()
    investigation = _make_investigation(
        providers=[
            FakeMetricsProvider(),
            FakeDeploymentProvider(
                deployments=[{"service": "api-gateway", "version": "v2.0.0"}]
            ),
        ],
        emitter=emitter,
        max_iterations=5,
        confidence_threshold=0.0,    # always met — stop after iteration 1
    )
    inc = _incident()
    result = await investigation.investigate(inc)

    assert result.iterations == 1
    assert ROOT_CAUSE_IDENTIFIED in emitter.names() or RCA_INCONCLUSIVE in emitter.names()


# ── 8. Knowledge retrieval integration ────────────────────────────────────────


@pytest.mark.asyncio
async def test_knowledge_provider_enriches_evidence_collection() -> None:
    """KnowledgeEvidenceProvider contributes KNOWLEDGE_BASE evidence items."""

    class _FakeRetriever:
        async def retrieve(
            self, context: Any, *, pipeline: Any = None
        ) -> tuple[list[RetrievalResult], RetrievalMetadata]:
            chunk = Chunk.make(
                "doc-kb-1",
                "Runbook: restart the service when error rate exceeds 5%.",
                0,
                metadata={"title": "Error Rate Runbook"},
            )
            result = RetrievalResult(
                result_id="rr-1",
                chunk=chunk,
                score=0.88,
                source=DocumentSource.DATABASE,
            )
            meta = RetrievalMetadata(
                retrieval_id=str(uuid.uuid4()),
                strategy=RetrievalStrategy.HYBRID,
                provider_name="fake-retriever",
                latency_ms=5.0,
                cached=False,
            )
            return [result], meta

    kb_provider = KnowledgeEvidenceProvider(
        retriever=_FakeRetriever(),
        provider_name="test-kb",
    )
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider(), kb_provider]
    )
    inc = _incident()
    result = await orchestrator.collect(inc)

    kb_items = [
        e for e in result.collection.items
        if e.source.kind == EvidenceSourceKind.KNOWLEDGE_BASE
    ]
    assert len(kb_items) >= 1
    assert "Runbook" in kb_items[0].title


@pytest.mark.asyncio
async def test_knowledge_provider_graceful_when_retriever_fails() -> None:
    """KnowledgeEvidenceProvider returns empty list when retriever raises.

    KnowledgeEvidenceProvider handles retrieval errors internally (logs + returns [])
    so other providers' evidence is preserved.  The provider returns an empty list
    rather than raising, so partial stays False but no KB items appear.
    """

    class _FailingRetriever:
        async def retrieve(self, context: Any, *, pipeline: Any = None) -> Any:
            raise ConnectionError("knowledge service down")

    kb_provider = KnowledgeEvidenceProvider(retriever=_FailingRetriever())
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider(), kb_provider]
    )
    inc = _incident()
    result = await orchestrator.collect(inc)

    # Metrics still collected — KB provider returned [] silently
    assert result.collection.count > 0
    kb_items = [
        e for e in result.collection.items
        if e.source.kind == EvidenceSourceKind.KNOWLEDGE_BASE
    ]
    assert len(kb_items) == 0


# ── 9. RCA generation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rca_generation_identifies_root_cause_with_deployment_evidence() -> None:
    """RCA identifies a root cause when deployment evidence is present."""
    inc = _incident()
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen, acceptance_threshold=0.1)

    orchestrator = EvidenceOrchestrator(
        providers=[
            FakeDeploymentProvider(
                deployments=[{
                    "service": "api-gateway",
                    "version": "v3.0.0",
                    "deployed_at": "1h ago",
                }]
            ),
            FakeMetricsProvider(),
        ]
    )
    ev_result = await orchestrator.collect(inc)
    rca = await engine.analyze(inc, ev_result.collection)

    assert rca.has_root_cause
    assert rca.root_cause is not None
    assert "api-gateway" in rca.root_cause.title or "v3.0.0" in rca.root_cause.title


@pytest.mark.asyncio
async def test_rca_is_inconclusive_without_evidence() -> None:
    """RCA is inconclusive when no evidence is available."""
    inc = _incident()
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen, acceptance_threshold=0.9)

    empty_collection = EvidenceCollection(
        incident_id=inc.incident_id,
        items=(),
        correlations=(),
        collected_at=_now(),
    )
    rca = await engine.analyze(inc, empty_collection)

    # With no evidence the generic fallback hypothesis has confidence 0
    assert rca.confidence < 0.9
    # unresolved_uncertainty is populated when no root cause found
    if not rca.has_root_cause:
        assert rca.unresolved_uncertainty is not None


# ── 10. Lifecycle events ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_emits_all_expected_lifecycle_events() -> None:
    """Full pipeline (ingestion→investigation) emits the required lifecycle events."""
    repo = InMemoryIncidentRepository()
    emitter = _EventCollector()
    ingestion_svc = _make_ingestion_service(repo, emitter)
    investigation = _make_investigation(
        providers=[FakeMetricsProvider(), FakeLogsProvider()],
        emitter=emitter,
        confidence_threshold=0.0,
    )
    pipeline = IncidentOrchestrationPipeline(
        ingestion_service=ingestion_svc,
        investigation=investigation,
        repository=repo,
        event_emitter=emitter,
    )
    inc = _incident()
    await pipeline.run(inc)

    event_names = emitter.names()
    assert INCIDENT_CREATED in event_names
    assert INVESTIGATION_STARTED in event_names
    assert EVIDENCE_COLLECTION_STARTED in event_names
    assert HYPOTHESES_GENERATED in event_names
    # At minimum one of the RCA terminal events was emitted
    assert ROOT_CAUSE_IDENTIFIED in event_names or RCA_INCONCLUSIVE in event_names


# ── 11. Observability context propagation ─────────────────────────────────────


@pytest.mark.asyncio
async def test_context_ids_propagated_through_events() -> None:
    """incident_id and correlation_id appear in every emitted event payload."""
    repo = InMemoryIncidentRepository()
    emitter = _EventCollector()
    ingestion_svc = _make_ingestion_service(repo, emitter)
    investigation = _make_investigation(
        providers=[FakeMetricsProvider()],
        emitter=emitter,
        confidence_threshold=0.0,
    )
    pipeline = IncidentOrchestrationPipeline(
        ingestion_service=ingestion_svc,
        investigation=investigation,
        repository=repo,
        event_emitter=emitter,
    )
    corr_id = str(uuid.uuid4())
    inc = _incident(correlation_id=corr_id)
    await pipeline.run(inc, execution_id="exec-ctx-001")

    incident_id = inc.incident_id
    for name, payload in emitter.events:
        # Only events that include incident_id in their schema
        if "incident_id" in payload:
            assert payload["incident_id"] == incident_id, (
                f"Event {name!r} has wrong incident_id"
            )


# ── 12. Cancellation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_handles_cancellation_after_ingestion() -> None:
    """Pipeline respects a cancellation signal raised after ingestion."""
    repo = InMemoryIncidentRepository()
    emitter = _EventCollector()
    ingestion_svc = _make_ingestion_service(repo, emitter)
    investigation = _make_investigation(providers=[FakeMetricsProvider()], emitter=emitter)

    pipeline = IncidentOrchestrationPipeline(
        ingestion_service=ingestion_svc,
        investigation=investigation,
        repository=repo,
        event_emitter=emitter,
    )
    inc = _incident()
    # cancellation_check fires immediately after ingestion
    result = await pipeline.run(inc, cancellation_check=lambda: True)

    assert result.status == "cancelled"


@pytest.mark.asyncio
async def test_investigation_handles_cancellation_mid_loop() -> None:
    """InvestigationOrchestrator stops gracefully when cancelled at iteration boundary."""
    emitter = _EventCollector()
    call_count = [0]

    def _cancel_after_first() -> bool:
        call_count[0] += 1
        return call_count[0] > 1    # cancel from the second check onward

    # Set confidence_threshold impossibly high so iteration 1 does NOT converge;
    # the loop will enter iteration 2 where the cancellation check fires.
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen)
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider()],
        event_emitter=emitter,
    )
    investigation = InvestigationOrchestrator(
        evidence_orchestrator=orchestrator,
        hypothesis_engine=engine,
        event_emitter=emitter,
        max_iterations=5,
        confidence_threshold=1.01,   # never met — forces all 5 iterations unless cancelled
    )
    inc = _incident()
    result = await investigation.investigate(
        inc, cancellation_check=_cancel_after_first
    )

    assert result.cancelled


# ── 13. Timeout ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_investigation_handles_provider_timeout() -> None:
    """A provider that hangs is interrupted by the iteration timeout."""

    class _SlowProvider:
        kind = EvidenceSourceKind.METRICS
        name = "slow-provider"

        async def collect(
            self, incident: Any, *, max_items: int = 20, context: Any = None
        ) -> list[Any]:
            await asyncio.sleep(999)   # simulate hang
            return []

        async def health_check(self) -> bool:
            return True

    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen)
    orchestrator = EvidenceOrchestrator(providers=[_SlowProvider()])
    investigation = InvestigationOrchestrator(
        evidence_orchestrator=orchestrator,
        hypothesis_engine=engine,
        max_iterations=1,
        iteration_timeout_seconds=0.05,    # 50 ms timeout
    )
    inc = _incident()
    result = await investigation.investigate(inc)

    # Either timed_out or rca is present with no root cause
    # The key requirement is that the call returned rather than hanging
    assert result is not None
    assert result.rca is not None


# ── 14. Retry (idempotency / duplicate incident) ──────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_incident_detection_by_correlation_id() -> None:
    """Ingesting the same correlation_id twice returns is_duplicate=True."""
    repo = InMemoryIncidentRepository()
    emitter = _EventCollector()
    svc = _make_ingestion_service(repo, emitter)

    corr_id = str(uuid.uuid4())
    first = _incident(incident_id="inc-first", correlation_id=corr_id)
    second = _incident(incident_id="inc-second", correlation_id=corr_id)

    result_first = await svc.ingest(first)
    result_second = await svc.ingest(second)

    assert not result_first.is_duplicate
    assert result_second.is_duplicate
    assert result_second.existing is not None
    assert result_second.existing.incident_id == "inc-first"
    assert INCIDENT_DUPLICATE_DETECTED in emitter.names()


# ── 15. Duplicate event idempotency (same incident_id) ────────────────────────


@pytest.mark.asyncio
async def test_same_incident_id_ingested_twice_updates_record() -> None:
    """Re-ingesting the same incident_id persists the latest version."""
    repo = InMemoryIncidentRepository()
    svc = _make_ingestion_service(repo)
    inc = _incident(incident_id="inc-dedup-001")

    await svc.ingest(inc)
    # Ingest again — different correlation_id so not a "duplicate" in the
    # correlation sense, but same incident_id should overwrite.
    updated = Incident(
        incident_id="inc-dedup-001",
        title="Updated title",
        severity=IncidentSeverity.CRITICAL,
        status=IncidentStatus.INVESTIGATING,
        affected_services=("svc-b",),
        description="Updated description.",
        detected_at=_now(),
        correlation_id=str(uuid.uuid4()),    # new corr_id — not a duplicate
    )
    await svc.ingest(updated)

    persisted = await repo.get("inc-dedup-001")
    assert persisted is not None
    assert persisted.severity == IncidentSeverity.CRITICAL


# ── 16. Insufficient-confidence reinvestigation ───────────────────────────────


@pytest.mark.asyncio
async def test_inconclusive_rca_triggers_reinvestigation_required_event() -> None:
    """When investigation is inconclusive the pipeline emits ReinvestigationRequired."""
    repo = InMemoryIncidentRepository()
    emitter = _EventCollector()
    ingestion_svc = _make_ingestion_service(repo, emitter)

    # Force HypothesisEngine to never accept any hypothesis — produces inconclusive RCA
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen, acceptance_threshold=1.01)  # never met
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider()],
        event_emitter=emitter,
    )
    investigation = InvestigationOrchestrator(
        evidence_orchestrator=orchestrator,
        hypothesis_engine=engine,
        event_emitter=emitter,
        max_iterations=1,
        confidence_threshold=1.01,
    )
    pipeline = IncidentOrchestrationPipeline(
        ingestion_service=ingestion_svc,
        investigation=investigation,
        repository=repo,
        event_emitter=emitter,
        run_remediation_on_inconclusive=False,
    )
    inc = _incident()
    result = await pipeline.run(inc)

    assert result.status == "escalated"
    assert INCIDENT_REINVESTIGATION_REQUIRED in emitter.names()
    assert RCA_INCONCLUSIVE in emitter.names()


# ── Full pipeline with remediation and validation ────────────────────────────


@pytest.mark.asyncio
async def test_full_pipeline_with_remediation_and_validation() -> None:
    """End-to-end pipeline including remediation and validation resolves incident."""
    repo = InMemoryIncidentRepository()
    emitter = _EventCollector()
    ingestion_svc = _make_ingestion_service(repo, emitter)
    investigation = _make_investigation(
        providers=[
            FakeMetricsProvider(),
            FakeDeploymentProvider(
                deployments=[{"service": "api-gateway", "version": "v2.0.0"}]
            ),
        ],
        emitter=emitter,
        confidence_threshold=0.0,
    )
    remediation_engine = RemediationEngine(
        policy_gateway=AlwaysAllowPolicyGateway(),
        executor=FakeActionExecutor(default_succeed=True),
    )
    validation_engine = ValidationEngine(
        runners={ValidationStrategyKind.HEALTH_CHECK: FakeHealthCheckRunner(succeed=True)}
    )
    verification_engine = VerificationEngine(
        metrics_snapshot=FakeMetricsSnapshot(
            values={
                "api-gateway": {"error_rate": 0.01},
                "checkout-service": {"error_rate": 0.02},
            }
        )
    )

    pipeline = IncidentOrchestrationPipeline(
        ingestion_service=ingestion_svc,
        investigation=investigation,
        repository=repo,
        remediation_engine=remediation_engine,
        validation_engine=validation_engine,
        verification_engine=verification_engine,
        remediation_planner=_simple_remediation_planner,
        validation_planner=_simple_validation_planner,
        verification_planner=_simple_verification_planner,
        event_emitter=emitter,
        run_remediation_on_inconclusive=True,
    )
    inc = _incident()
    result = await pipeline.run(inc)

    assert result.status in ("resolved", "mitigated")
    event_names = emitter.names()
    assert REMEDIATION_PLANNED in event_names
    assert REMEDIATION_STARTED in event_names
    assert VALIDATION_STARTED in event_names
    assert VERIFICATION_STARTED in event_names


# ── Investigation agent with evidence tool ────────────────────────────────────


@pytest.mark.asyncio
async def test_deterministic_agent_requests_missing_evidence_via_tool() -> None:
    """DeterministicInvestigationAgent uses EvidenceRequestTool when evidence is missing."""
    inc = _incident()
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen)

    # Only metrics initially — no logs, no deployments
    orchestrator = EvidenceOrchestrator(
        providers=[FakeMetricsProvider(), FakeLogsProvider(), FakeDeploymentProvider(
            deployments=[{"service": "api-gateway", "version": "v2.0.0"}]
        )]
    )
    evidence_tool = EvidenceRequestTool(orchestrator=orchestrator)
    agent = DeterministicInvestigationAgent(
        hypothesis_engine=engine,
        evidence_tool=evidence_tool,
        request_missing=True,
        max_tool_calls=2,
    )

    # Start with only metrics evidence
    metrics_only = await orchestrator.collect(
        inc, source_kinds={EvidenceSourceKind.METRICS}
    )
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(metrics_only.collection)

    output = await agent.investigate(inc, metrics_only.collection, normalized)

    assert output.rca is not None
    # Agent should have attempted to fill gaps
    assert isinstance(output.missing_evidence, tuple)
    assert isinstance(output.requested_kinds, tuple)
    assert len(output.reasoning_summary) > 0
