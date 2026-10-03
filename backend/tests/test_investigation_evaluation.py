"""Evaluation framework tests — categories 17-18.

Tests 17: Evaluation metrics (all 5 strategies, correct scoring)
Tests 18: OTel/structured logging context propagation through evaluation
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.evaluation.context import EvaluationContext
from backend.evaluation.engine import EvaluationEngine
from backend.evaluation.models import (
    EvaluationStatus,
    EvaluationSubject,
    EvaluationVerdict,
)
from backend.evaluation.registry import EvaluationRegistry
from backend.evaluation.strategies.investigation import (
    EvidenceUtilizationStrategy,
    InvestigationLatencyStrategy,
    InvestigationOutcomeStrategy,
    LLMTokenUsageStrategy,
    ToolCallStrategy,
    register_investigation_strategies,
)
from backend.models.evidence import EvidenceCollection
from backend.models.hypothesis import RootCauseAnalysis
from backend.models.incident import Incident, IncidentSeverity, IncidentStatus
from backend.services.investigation_evaluation import InvestigationEvaluator, _build_evidence
from backend.services.investigation_orchestrator import InvestigationResult

# ── helpers ────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _incident() -> Incident:
    return Incident(
        incident_id=str(uuid.uuid4()),
        title="Eval test incident",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        affected_services=("svc-a",),
        description="Test.",
        detected_at=_now(),
        correlation_id=str(uuid.uuid4()),
    )


def _make_rca(incident: Incident, *, has_root_cause: bool, confidence: float) -> RootCauseAnalysis:
    return RootCauseAnalysis(
        rca_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        observed_symptoms=incident.symptoms,
        correlated_evidence_ids=(),
        candidate_hypotheses=(),
        evaluated_hypotheses=(),
        root_cause=None if not has_root_cause else _make_hypothesis(incident, confidence),
        confidence=confidence,
        unresolved_uncertainty=None if has_root_cause else "inconclusive",
        produced_at=_now(),
    )


def _make_hypothesis(incident: Incident, confidence: float) -> Any:
    from backend.models.hypothesis import Hypothesis, HypothesisStatus
    return Hypothesis(
        hypothesis_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        title="Test hypothesis",
        description="desc",
        proposed_at=_now(),
        status=HypothesisStatus.SUPPORTED,
        confidence=confidence,
    )


def _make_investigation_result(
    incident: Incident,
    *,
    has_root_cause: bool = True,
    confidence: float = 0.85,
    iterations: int = 1,
    cancelled: bool = False,
    timed_out: bool = False,
) -> InvestigationResult:
    rca = _make_rca(incident, has_root_cause=has_root_cause, confidence=confidence)
    empty_collection = EvidenceCollection(
        incident_id=incident.incident_id,
        items=(),
        correlations=(),
        collected_at=_now(),
    )
    from backend.services.evidence_normalizer import EvidenceNormalizer
    from backend.services.evidence_orchestrator import EvidenceOrchestrationResult
    norm_collection = EvidenceNormalizer().normalize_collection(empty_collection)
    ev_result = EvidenceOrchestrationResult(
        collection=empty_collection,
        normalized=norm_collection,
        provider_errors={},
        partial=False,
        duration_ms=100.0,
    )
    return InvestigationResult(
        rca=rca,
        evidence_results=(ev_result,),
        iterations=iterations,
        inconclusive=not has_root_cause,
        cancelled=cancelled,
        timed_out=timed_out,
        duration_ms=1500.0,
        incident_id=incident.incident_id,
        execution_id=str(uuid.uuid4()),
    )


def _context(evidence: dict[str, Any]) -> EvaluationContext:
    return EvaluationContext(
        evaluation_id=str(uuid.uuid4()),
        subject=EvaluationSubject.AGENT,
        subject_id="inc-test",
        evidence=evidence,
    )


def _make_engine() -> EvaluationEngine:
    registry = EvaluationRegistry()
    register_investigation_strategies(registry)
    return EvaluationEngine(registry=registry)


# ── 17. Evaluation metrics ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_latency_strategy_passes_below_threshold() -> None:
    """InvestigationLatencyStrategy passes when latency is below threshold."""
    strategy = InvestigationLatencyStrategy(threshold_ms=30_000.0)
    ctx = _context({"investigation_latency_ms": 5_000.0})

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.PASSED
    assert result.score > 0.0


@pytest.mark.asyncio
async def test_latency_strategy_fails_above_threshold() -> None:
    """InvestigationLatencyStrategy fails when latency exceeds threshold."""
    strategy = InvestigationLatencyStrategy(threshold_ms=10_000.0)
    ctx = _context({"investigation_latency_ms": 25_000.0})

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.FAILED


@pytest.mark.asyncio
async def test_token_usage_strategy_passes_within_budget() -> None:
    """LLMTokenUsageStrategy passes when total tokens are within budget."""
    strategy = LLMTokenUsageStrategy(max_total_tokens=10_000)
    ctx = _context({"tokens_input": 500, "tokens_output": 200, "tokens_total": 700})

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.PASSED
    assert len(result.metrics) == 3


@pytest.mark.asyncio
async def test_token_usage_strategy_fails_over_budget() -> None:
    """LLMTokenUsageStrategy fails when total tokens exceed budget."""
    strategy = LLMTokenUsageStrategy(max_total_tokens=1_000)
    ctx = _context({"tokens_total": 5_000, "tokens_input": 3000, "tokens_output": 2000})

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.FAILED


@pytest.mark.asyncio
async def test_tool_call_strategy_passes_within_limit() -> None:
    """ToolCallStrategy passes when tool calls are within the limit."""
    strategy = ToolCallStrategy(max_tool_calls=6)
    ctx = _context({"tool_calls": 2})

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.PASSED


@pytest.mark.asyncio
async def test_evidence_utilization_strategy_passes_with_evidence() -> None:
    """EvidenceUtilizationStrategy passes when evidence is present."""
    strategy = EvidenceUtilizationStrategy()
    ctx = _context({
        "evidence_count": 5,
        "evidence_kinds": ["metrics", "logs", "deployments"],
    })

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.PASSED
    assert result.score > 0.0


@pytest.mark.asyncio
async def test_evidence_utilization_strategy_fails_with_no_evidence() -> None:
    """EvidenceUtilizationStrategy fails when no evidence was collected."""
    strategy = EvidenceUtilizationStrategy()
    ctx = _context({"evidence_count": 0, "evidence_kinds": []})

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.FAILED


@pytest.mark.asyncio
async def test_outcome_strategy_passes_with_high_confidence_root_cause() -> None:
    """InvestigationOutcomeStrategy passes when root cause found with high confidence."""
    strategy = InvestigationOutcomeStrategy(confidence_threshold=0.60)
    ctx = _context({
        "has_root_cause": True,
        "confidence": 0.85,
        "hypothesis_count": 3,
        "iterations": 1,
    })

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.PASSED


@pytest.mark.asyncio
async def test_outcome_strategy_fails_when_no_root_cause() -> None:
    """InvestigationOutcomeStrategy fails when no root cause was identified."""
    strategy = InvestigationOutcomeStrategy(confidence_threshold=0.60)
    ctx = _context({
        "has_root_cause": False,
        "confidence": 0.30,
        "hypothesis_count": 1,
        "iterations": 3,
    })

    result = await strategy.evaluate(ctx)

    assert result.status == EvaluationStatus.FAILED


@pytest.mark.asyncio
async def test_full_evaluation_pipeline_passes_good_investigation() -> None:
    """EvaluationEngine produces PASSED verdict for a successful investigation."""
    engine = _make_engine()
    ctx = _context({
        "investigation_latency_ms": 8_000.0,
        "tokens_input": 1_000,
        "tokens_output": 500,
        "tokens_total": 1_500,
        "tool_calls": 1,
        "evidence_count": 6,
        "evidence_kinds": ["metrics", "logs", "deployments"],
        "has_root_cause": True,
        "confidence": 0.88,
        "hypothesis_count": 2,
        "iterations": 1,
        "outcome": "resolved",
    })

    report = await engine.evaluate(
        ["investigation_latency", "llm_token_usage", "tool_call_count",
         "evidence_utilization", "investigation_outcome"],
        ctx,
    )

    assert report.verdict == EvaluationVerdict.PASSED
    assert report.overall_score > 0.5


@pytest.mark.asyncio
async def test_full_evaluation_pipeline_fails_for_bad_investigation() -> None:
    """EvaluationEngine produces FAILED verdict when outcome strategy fails."""
    engine = _make_engine()
    ctx = _context({
        "investigation_latency_ms": 5_000.0,
        "tokens_total": 500,
        "tokens_input": 300,
        "tokens_output": 200,
        "tool_calls": 0,
        "evidence_count": 0,
        "evidence_kinds": [],
        "has_root_cause": False,
        "confidence": 0.10,
        "hypothesis_count": 0,
        "iterations": 3,
        "outcome": "inconclusive",
    })

    report = await engine.evaluate(
        ["evidence_utilization", "investigation_outcome"],
        ctx,
    )

    assert report.verdict == EvaluationVerdict.FAILED


@pytest.mark.asyncio
async def test_investigation_evaluator_builds_evidence_correctly() -> None:
    """InvestigationEvaluator builds evidence dict from InvestigationResult."""
    incident = _incident()
    result = _make_investigation_result(
        incident, has_root_cause=True, confidence=0.80, iterations=2
    )
    evidence = _build_evidence(result, None, latency_ms=12_000.0)

    assert evidence["investigation_latency_ms"] == 12_000.0
    assert evidence["has_root_cause"] is True
    assert evidence["confidence"] == 0.80
    assert evidence["iterations"] == 2
    assert evidence["outcome"] == "resolved"


@pytest.mark.asyncio
async def test_investigation_evaluator_runs_all_strategies() -> None:
    """InvestigationEvaluator runs all 5 strategies and returns a report."""
    engine = _make_engine()
    evaluator = InvestigationEvaluator(engine=engine)
    incident = _incident()
    result = _make_investigation_result(incident, has_root_cause=True, confidence=0.75)

    report = await evaluator.evaluate(incident, result, latency_ms=5_000.0)

    assert len(report.results) == 5
    assert report.evaluation_id
    assert report.subject == EvaluationSubject.AGENT
    assert report.subject_id == incident.incident_id


# ── 18. OTel / structured logging context propagation ─────────────────────


@pytest.mark.asyncio
async def test_evaluation_report_carries_correlation_id() -> None:
    """EvaluationReport propagates correlation_id from the context."""
    engine = _make_engine()
    ctx = EvaluationContext(
        evaluation_id="eval-001",
        subject=EvaluationSubject.AGENT,
        subject_id="inc-001",
        evidence={
            "investigation_latency_ms": 1_000.0,
            "tokens_total": 100,
            "tokens_input": 60,
            "tokens_output": 40,
            "tool_calls": 0,
            "evidence_count": 2,
            "evidence_kinds": ["metrics"],
            "has_root_cause": True,
            "confidence": 0.70,
            "hypothesis_count": 1,
            "iterations": 1,
            "outcome": "resolved",
        },
        correlation_id="corr-xyz-789",
        execution_id="exec-001",
    )

    report = await engine.evaluate(
        ["investigation_latency", "investigation_outcome"],
        ctx,
    )

    assert report.correlation_id == "corr-xyz-789"
    assert report.execution_id == "exec-001"
    assert report.evaluation_id == "eval-001"


@pytest.mark.asyncio
async def test_evaluation_result_has_timestamps() -> None:
    """Each EvaluationResult carries started_at and ended_at timestamps."""
    strategy = InvestigationLatencyStrategy()
    ctx = _context({"investigation_latency_ms": 2_000.0})

    result = await strategy.evaluate(ctx)

    assert result.started_at is not None
    assert result.ended_at is not None
    assert result.ended_at >= result.started_at


@pytest.mark.asyncio
async def test_evaluation_engine_propagates_context_to_report() -> None:
    """EvaluationEngine propagates workflow_id and execution_id to the report."""
    engine = _make_engine()
    ctx = EvaluationContext(
        evaluation_id="eval-prop-001",
        subject=EvaluationSubject.AGENT,
        subject_id="inc-prop",
        evidence={
            "investigation_latency_ms": 1_000.0,
            "has_root_cause": True,
            "confidence": 0.80,
            "hypothesis_count": 1,
            "iterations": 1,
        },
        workflow_id="wf-001",
        execution_id="exec-prop-001",
        correlation_id="corr-prop",
    )

    report = await engine.evaluate(
        ["investigation_latency", "investigation_outcome"],
        ctx,
    )

    assert report.workflow_id == "wf-001"
    assert report.execution_id == "exec-prop-001"
    assert report.correlation_id == "corr-prop"
    assert report.subject_id == "inc-prop"
