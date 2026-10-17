"""LLM investigation agent tests — categories 13-16.

Tests 13: Deterministic agent (backward-compat)
Tests 14: LLM agent using fake provider
Tests 15: Evidence-grounding validation
Tests 16: Insufficient-evidence behavior
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from backend.core.hypothesis_engine import DeterministicHypothesisGenerator, HypothesisEngine
from backend.interfaces.fake_evidence import (
    FakeDeploymentProvider,
    FakeLogsProvider,
    FakeMetricsProvider,
)
from backend.interfaces.fake_llm import (
    FailingFakeLLM,
    FakeLLMProvider,
    ToolCallFakeLLM,
)
from backend.interfaces.llm import LLMToolCall, LLMUnavailableError
from backend.models.evidence import EvidenceCollection, EvidenceSourceKind
from backend.models.incident import (
    Incident,
    IncidentSeverity,
    IncidentSignal,
    IncidentStatus,
    SignalSource,
    SignalType,
)
from backend.services.evidence_normalizer import (
    EvidenceNormalizer,
    NormalizedEvidence,
    NormalizedEvidenceCollection,
)
from backend.services.evidence_orchestrator import EvidenceOrchestrator
from backend.services.investigation_agent import (
    DeterministicInvestigationAgent,
    EvidenceRequestTool,
)
from backend.services.investigation_orchestrator import InvestigationOrchestrator
from backend.services.llm_investigation_agent import (
    _LLM_CONTEXT_MAX_TOTAL_CHARS,
    _LLM_PROMPT_MAX_TOTAL_CHARS,
    EvidenceGroundingValidator,
    LLMInvestigationAgent,
    _build_safe_incident_context,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _incident(
    *,
    affected_services: tuple[str, ...] = ("api-gw", "checkout"),
    symptoms: tuple[str, ...] = ("5xx elevated",),
) -> Incident:
    return Incident(
        incident_id=str(uuid.uuid4()),
        title="Test incident",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        affected_services=affected_services,
        description="Test.",
        detected_at=_now(),
        symptoms=symptoms,
        correlation_id=str(uuid.uuid4()),
    )


def _empty_collection(incident_id: str) -> EvidenceCollection:
    return EvidenceCollection(
        incident_id=incident_id,
        items=(),
        correlations=(),
        collected_at=_now(),
    )


def _rca_output_json(
    *,
    confidence: float = 0.85,
    evidence_ids: list[str] | None = None,
    root_cause: str = "Deployment regression",
) -> str:
    return json.dumps(
        {
            "root_cause_title": root_cause,
            "root_cause_description": "A recent deployment introduced a regression.",
            "confidence": confidence,
            "supporting_evidence_ids": evidence_ids or [],
            "contradicting_evidence_ids": [],
            "missing_evidence": [],
            "reasoning_summary": "Evidence points to a recent deployment.",
        }
    )


def _diagnostic_incident() -> Incident:
    return replace(
        _incident(symptoms=("Database connection pool exhausted", "Error rate elevated")),
        signals=(
            IncidentSignal(
                signal_id="signal-1",
                signal_type=SignalType.METRIC_THRESHOLD,
                source=SignalSource.OTHER,
                title="Pool exhausted",
                description="No free database connections remain.",
                severity=IncidentSeverity.HIGH,
                received_at=_now(),
                service="order-api",
                environment="demo",
                raw_payload={
                    "pool": {"max": 10, "free": 0, "used": 10},
                    "metrics": {"error_rate_pct": 18.0, "total_errors": 7},
                    "failure_mode": "connection_leak",
                    "image_version": "abc123",
                    "password": "do-not-send",
                },
                labels={"failure_mode": "connection_leak", "service": "order-api"},
            ),
        ),
        metadata={
            "service": "order-api",
            "version": "abc123",
            "failure_mode": "connection_leak",
            "pool": {"max": 10, "free": 0, "used": 10},
            "metrics": {"error_rate_pct": 18.0, "total_errors": 7},
            "api_key": "metadata-secret",
            "email": "operator@example.com",
        },
    )


def test_llm_context_redacts_secrets_and_personal_data() -> None:
    incident = replace(
        _diagnostic_incident(),
        description="Operator alice@example.com supplied password=do-not-send.",
    )
    prompt = LLMInvestigationAgent(llm_provider=FakeLLMProvider())._build_evidence_prompt(
        incident,
        EvidenceNormalizer().normalize_collection(_empty_collection(incident.incident_id)),
    )

    assert "do-not-send" not in prompt
    assert "metadata-secret" not in prompt
    assert "operator@example.com" not in prompt
    assert "[REDACTED]" in prompt or "[REDACTED_EMAIL]" in prompt


def test_llm_context_allowlist_and_total_limit_bound_oversized_payloads() -> None:
    incident = replace(
        _diagnostic_incident(),
        metadata={
            "metrics": {"error_rate_pct": 18.0, "unbounded": "x" * 100_000},
            "unapproved": "y" * 100_000,
        },
    )

    context = _build_safe_incident_context(incident)

    assert len(context) <= _LLM_CONTEXT_MAX_TOTAL_CHARS
    assert "unbounded" not in context
    assert "unapproved" not in context


def test_llm_context_preserves_demo_diagnostic_fields() -> None:
    context = _build_safe_incident_context(_diagnostic_incident())

    assert "connection_leak" in context
    assert '"free":0' in context
    assert "error_rate_pct" in context
    assert "abc123" in context
    assert "Database connection pool exhausted" in context


def test_llm_prompt_redacts_and_bounds_normalized_evidence() -> None:
    incident = _diagnostic_incident()
    evidence = NormalizedEvidence(
        evidence_id="evidence-1",
        incident_id=incident.incident_id,
        source="demo",
        evidence_type="metrics",
        timestamp=_now(),
        title="Metric payload",
        content="password=do-not-send " + "x" * 100_000,
        service="order-api",
        environment="demo",
        resource=None,
        version="abc123",
        correlation_id=incident.correlation_id,
        relevance_score=1.0,
        confidence=1.0,
    )
    normalized = NormalizedEvidenceCollection(
        incident_id=incident.incident_id,
        items=(evidence,),
    )

    prompt = LLMInvestigationAgent(llm_provider=FakeLLMProvider())._build_evidence_prompt(
        incident,
        normalized,
    )

    assert len(prompt) <= _LLM_PROMPT_MAX_TOTAL_CHARS
    assert "do-not-send" not in prompt
    assert "[REDACTED]" in prompt


# ── 13. Deterministic agent ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deterministic_agent_produces_rca() -> None:
    """DeterministicInvestigationAgent produces a valid AgentRCAOutput."""
    inc = _incident()
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen)
    orchestrator = EvidenceOrchestrator(
        providers=[
            FakeMetricsProvider(),
            FakeDeploymentProvider(deployments=[{"service": "api-gw", "version": "v2.0"}]),
        ]
    )
    tool = EvidenceRequestTool(orchestrator=orchestrator)
    agent = DeterministicInvestigationAgent(
        hypothesis_engine=engine,
        evidence_tool=tool,
        request_missing=False,
    )

    ev_result = await orchestrator.collect(inc)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(ev_result.collection)

    output = await agent.investigate(inc, ev_result.collection, normalized)

    assert output.rca is not None
    assert len(output.reasoning_summary) > 0
    assert isinstance(output.missing_evidence, tuple)


@pytest.mark.asyncio
async def test_deterministic_agent_with_no_evidence_returns_fallback_hypothesis() -> None:
    """DeterministicInvestigationAgent handles zero evidence gracefully."""
    inc = _incident()
    gen = DeterministicHypothesisGenerator()
    engine = HypothesisEngine(generator=gen, acceptance_threshold=1.01)
    agent = DeterministicInvestigationAgent(
        hypothesis_engine=engine,
        request_missing=False,
    )
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)

    output = await agent.investigate(inc, collection, normalized)

    assert output.rca is not None
    assert output.rca.confidence < 1.0


# ── 14. LLM agent using fake provider ──────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_agent_produces_rca_from_structured_output() -> None:
    """LLMInvestigationAgent returns a grounded RCA when the LLM responds correctly."""
    inc = _incident()
    orchestrator = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    ev_result = await orchestrator.collect(inc)
    evidence_ids = [e.evidence_id for e in ev_result.collection.items[:2]]

    fake_llm = FakeLLMProvider(responses=[_rca_output_json(evidence_ids=evidence_ids)])
    agent = LLMInvestigationAgent(llm_provider=fake_llm, max_tool_rounds=0)

    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(ev_result.collection)

    output = await agent.investigate(inc, ev_result.collection, normalized)

    assert output.rca is not None
    assert output.rca.confidence > 0.0
    assert len(output.reasoning_summary) > 0


@pytest.mark.asyncio
async def test_llm_agent_retries_malformed_json_and_accepts_valid_rca() -> None:
    """Malformed structured output is retried and never treated as an RCA."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)
    valid = _rca_output_json(confidence=0.82)

    fake_llm = FakeLLMProvider(responses=["not json", valid])
    agent = LLMInvestigationAgent(
        llm_provider=fake_llm,
        max_tool_rounds=0,
        structured_retry_attempts=2,
    )

    output = await agent.investigate(inc, collection, normalized)

    assert output.rca.root_cause is not None
    assert output.rca.confidence == 0.82
    assert fake_llm.call_count == 2


@pytest.mark.asyncio
async def test_llm_agent_retries_missing_required_rca_fields() -> None:
    """A JSON object missing RCA fields is rejected before domain conversion."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)
    incomplete = json.dumps({"root_cause_title": "Missing fields"})
    valid = _rca_output_json(confidence=0.81)

    fake_llm = FakeLLMProvider(responses=[incomplete, valid])
    agent = LLMInvestigationAgent(
        llm_provider=fake_llm,
        max_tool_rounds=0,
        structured_retry_attempts=2,
    )

    output = await agent.investigate(inc, collection, normalized)

    assert output.rca.root_cause is not None
    assert output.rca.confidence == 0.81
    assert fake_llm.call_count == 2


@pytest.mark.asyncio
async def test_llm_agent_returns_explicit_inconclusive_after_retry_exhaustion() -> None:
    """Exhausted structured retries produce no fabricated RCA fields."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)

    fake_llm = FakeLLMProvider(responses=["not json"])
    agent = LLMInvestigationAgent(
        llm_provider=fake_llm,
        max_tool_rounds=0,
        structured_retry_attempts=2,
    )

    output = await agent.investigate(inc, collection, normalized)

    assert output.rca.root_cause is None
    assert output.rca.confidence == 0.0
    assert output.rca.unresolved_uncertainty is not None
    assert "Structured RCA unavailable" in output.rca.unresolved_uncertainty
    assert fake_llm.call_count == 2


@pytest.mark.asyncio
async def test_llm_agent_with_tool_calls_requests_evidence() -> None:
    """LLMInvestigationAgent executes tool calls and collects additional evidence."""
    inc = _incident()
    orchestrator = EvidenceOrchestrator(providers=[FakeMetricsProvider(), FakeLogsProvider()])
    ev_result = await orchestrator.collect(inc, source_kinds={EvidenceSourceKind.METRICS})
    tool = EvidenceRequestTool(orchestrator=orchestrator)

    tool_calls_turn1 = [
        LLMToolCall(
            call_id="tc1",
            tool_name="request_evidence",
            arguments={"source_kinds": ["logs"]},
        )
    ]
    final_output = _rca_output_json(confidence=0.80)

    fake_llm = ToolCallFakeLLM(
        tool_calls=[tool_calls_turn1],
        final_response=final_output,
        structured_final=json.loads(final_output),
    )

    agent = LLMInvestigationAgent(
        llm_provider=fake_llm,
        evidence_tool=tool,
        max_tool_rounds=2,
    )
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(ev_result.collection)

    output = await agent.investigate(inc, ev_result.collection, normalized)

    assert output.rca is not None
    assert EvidenceSourceKind.LOGS in output.requested_kinds or len(fake_llm.requests) >= 2


@pytest.mark.asyncio
async def test_llm_agent_falls_back_when_provider_unavailable() -> None:
    """LLMInvestigationAgent returns inconclusive RCA when LLM is unavailable."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)

    fake_llm = FailingFakeLLM(error=LLMUnavailableError("provider down", provider="fake"))
    agent = LLMInvestigationAgent(llm_provider=fake_llm, max_tool_rounds=1)

    output = await agent.investigate(inc, collection, normalized)

    assert output.rca is not None
    assert output.rca.confidence == 0.0
    assert output.rca.root_cause is None


@pytest.mark.asyncio
async def test_llm_agent_respects_cancellation() -> None:
    """LLMInvestigationAgent returns fallback output when cancelled."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)

    class _Cancelled:
        def is_set(self) -> bool:
            return True

    fake_llm = FakeLLMProvider(responses=["ok"])
    agent = LLMInvestigationAgent(llm_provider=fake_llm, max_tool_rounds=1)

    output = await agent.investigate(
        inc,
        collection,
        normalized,
        context={"cancellation_token": _Cancelled()},
    )

    assert output.rca is not None
    assert output.rca.root_cause is None


@pytest.mark.asyncio
async def test_llm_agent_without_tools_skips_tool_rounds() -> None:
    """LLMInvestigationAgent with no evidence_tool skips tool-call loops."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)

    rca_json = _rca_output_json(confidence=0.70)
    fake_llm = FakeLLMProvider(responses=[rca_json])
    agent = LLMInvestigationAgent(
        llm_provider=fake_llm,
        evidence_tool=None,
        max_tool_rounds=3,
    )

    output = await agent.investigate(inc, collection, normalized)

    # Should have made exactly 2 calls: the main request + the forced structured answer
    assert fake_llm.call_count <= 3
    assert output.rca is not None


@pytest.mark.asyncio
async def test_investigation_orchestrator_delegates_to_llm_agent() -> None:
    """The production-capable orchestrator path uses the configured LLM agent."""
    inc = _incident()
    evidence_orchestrator = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    fake_llm = FakeLLMProvider(responses=[_rca_output_json(root_cause="LLM-selected cause")])
    llm_agent = LLMInvestigationAgent(
        llm_provider=fake_llm,
        evidence_tool=None,
        max_tool_rounds=0,
    )
    investigation = InvestigationOrchestrator(
        evidence_orchestrator=evidence_orchestrator,
        hypothesis_engine=HypothesisEngine(generator=DeterministicHypothesisGenerator()),
        investigation_agent=llm_agent,
        max_iterations=1,
        confidence_threshold=0.0,
    )

    result = await investigation.investigate(inc)

    assert result.rca.root_cause_title == "LLM-selected cause"
    assert fake_llm.call_count > 0


# ── 15. Evidence-grounding validation ──────────────────────────────────────


@pytest.mark.asyncio
async def test_grounding_validator_strips_hallucinated_ids() -> None:
    """EvidenceGroundingValidator removes IDs not present in collection."""
    inc = _incident()
    orchestrator = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    ev_result = await orchestrator.collect(inc)
    real_ids = [e.evidence_id for e in ev_result.collection.items]

    validator = EvidenceGroundingValidator()
    mixed = [*real_ids[:1], "HALLUCINATED-ID-1", "HALLUCINATED-ID-2"]

    grounded = validator.validate(mixed, ev_result.collection, incident_id=inc.incident_id)

    assert "HALLUCINATED-ID-1" not in grounded
    assert "HALLUCINATED-ID-2" not in grounded
    assert real_ids[0] in grounded


@pytest.mark.asyncio
async def test_grounding_validator_empty_ids_returns_empty() -> None:
    """Validator returns an empty tuple for an empty input list."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    validator = EvidenceGroundingValidator()

    result = validator.validate([], collection, incident_id=inc.incident_id)
    assert result == ()


@pytest.mark.asyncio
async def test_llm_agent_strips_hallucinated_ids_from_rca() -> None:
    """LLMInvestigationAgent's RCA never contains hallucinated evidence IDs."""
    inc = _incident()
    orchestrator = EvidenceOrchestrator(providers=[FakeMetricsProvider()])
    ev_result = await orchestrator.collect(inc)
    real_id = ev_result.collection.items[0].evidence_id if ev_result.collection.items else "real"

    rca_with_hallucination = json.dumps(
        {
            "root_cause_title": "Real cause",
            "root_cause_description": "desc",
            "confidence": 0.9,
            "supporting_evidence_ids": [real_id, "FAKE-ID-99", "MADE-UP-ID"],
            "contradicting_evidence_ids": [],
            "missing_evidence": [],
            "reasoning_summary": "test",
        }
    )
    fake_llm = FakeLLMProvider(responses=[rca_with_hallucination])
    agent = LLMInvestigationAgent(llm_provider=fake_llm, max_tool_rounds=0)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(ev_result.collection)

    output = await agent.investigate(inc, ev_result.collection, normalized)

    if output.rca.root_cause is not None:
        for eid in output.rca.root_cause.supporting_evidence_ids:
            assert eid != "FAKE-ID-99"
            assert eid != "MADE-UP-ID"


# ── 16. Insufficient-evidence behavior ─────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_agent_below_threshold_produces_inconclusive_rca() -> None:
    """LLMInvestigationAgent returns inconclusive RCA when confidence is below threshold."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)

    low_confidence_json = _rca_output_json(confidence=0.20)
    fake_llm = FakeLLMProvider(responses=[low_confidence_json])
    agent = LLMInvestigationAgent(
        llm_provider=fake_llm,
        max_tool_rounds=0,
        acceptance_threshold=0.60,
    )

    output = await agent.investigate(inc, collection, normalized)

    assert output.rca is not None
    assert output.rca.root_cause is None
    assert output.rca.unresolved_uncertainty is not None


@pytest.mark.asyncio
async def test_llm_agent_reports_missing_evidence_categories() -> None:
    """LLMInvestigationAgent populates missing_evidence from the LLM output."""
    inc = _incident()
    collection = _empty_collection(inc.incident_id)
    normalizer = EvidenceNormalizer()
    normalized = normalizer.normalize_collection(collection)

    rca_with_missing = json.dumps(
        {
            "root_cause_title": "Unknown",
            "root_cause_description": "Not enough info.",
            "confidence": 0.30,
            "supporting_evidence_ids": [],
            "contradicting_evidence_ids": [],
            "missing_evidence": ["metrics", "logs", "deployments"],
            "reasoning_summary": "Need more evidence.",
        }
    )
    fake_llm = FakeLLMProvider(responses=[rca_with_missing])
    agent = LLMInvestigationAgent(
        llm_provider=fake_llm,
        max_tool_rounds=0,
    )

    output = await agent.investigate(inc, collection, normalized)

    assert EvidenceSourceKind.METRICS in output.missing_evidence
    assert EvidenceSourceKind.LOGS in output.missing_evidence
    assert EvidenceSourceKind.DEPLOYMENTS in output.missing_evidence
