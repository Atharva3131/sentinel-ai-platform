"""ConfidenceEvaluator — scores workflow confidence using the Evaluation Framework."""

from __future__ import annotations

from dataclasses import dataclass

from backend.evaluation.engine import EvaluationEngine
from backend.retrieval.models import RetrievalMetadata, RetrievalResult
from backend.retrieval.retrieval_evaluator import RetrievalEvaluator, register_retrieval_strategies
from backend.workflows.sre.exceptions import SREPhaseError
from backend.workflows.sre.models import (
    AnalysisResult,
    ConfidenceAssessment,
    ConfidenceLevel,
    IncidentContext,
)

_HIGH_THRESHOLD = 0.75
_MEDIUM_THRESHOLD = 0.45

# Weights for the composite confidence score
_W_RETRIEVAL = 0.40
_W_EVIDENCE = 0.25
_W_ANALYSIS = 0.25
_W_SEVERITY = 0.10


def _level(score: float) -> ConfidenceLevel:
    if score >= _HIGH_THRESHOLD:
        return ConfidenceLevel.HIGH
    if score >= _MEDIUM_THRESHOLD:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def _evidence_score(chunk_count: int) -> float:
    """Sigmoid-ish score: 0 chunks → 0.0, 10+ chunks → ~1.0."""
    return min(1.0, chunk_count / 10.0)


def _analysis_score(analysis: AnalysisResult) -> float:
    """Score based on analysis completeness."""
    base = 0.0
    if analysis.root_cause_hypothesis:
        base += 0.6
    if analysis.findings:
        base += 0.3
    if analysis.health_status in ("healthy", "degraded"):
        base += 0.1
    return min(1.0, base)


def _severity_penalty(incident: IncidentContext) -> float:
    """Higher severity → slightly lower confidence floor (more caution)."""
    from backend.workflows.sre.models import IncidentSeverity

    penalties = {
        IncidentSeverity.CRITICAL: 0.85,
        IncidentSeverity.HIGH: 0.90,
        IncidentSeverity.MEDIUM: 0.95,
        IncidentSeverity.LOW: 1.00,
    }
    return penalties.get(incident.severity, 1.0)


@dataclass(slots=True)
class ConfidenceEvaluator:
    """Produces a composite confidence score from retrieval quality and analysis.

    Combines four signals with fixed weights:
        - retrieval_score: from RetrievalEvaluator (context quality)
        - evidence_score:  from retrieved chunk count
        - analysis_score:  from root-cause and findings completeness
        - severity_factor: cautionary multiplier for critical incidents

    When ``score < approval_threshold``, ``requires_approval`` is True and the
    orchestrator will route the workflow through the ApprovalGateway before recovery.

    The full ``EvaluationReport`` is attached to the result for downstream
    consumption (audit logs, dashboards).
    """

    engine: EvaluationEngine
    approval_threshold: float = 0.65

    async def evaluate(
        self,
        incident: IncidentContext,
        analysis: AnalysisResult,
        retrieved_results: list[RetrievalResult],
        retrieval_metadata: RetrievalMetadata,
        *,
        evaluation_id: str | None = None,
    ) -> ConfidenceAssessment:
        """Compute confidence and decide whether approval is required.

        Raises:
            SREPhaseError: If the evaluation pipeline raises unexpectedly.
        """
        try:
            evaluator = RetrievalEvaluator(
                engine=self.engine,
                default_strategies=("retrieval.context_quality",),
            )
            _retrieval_metrics, report = await evaluator.evaluate(
                incident.title,
                retrieved_results,
                retrieval_metadata,
                evaluation_id=evaluation_id,
            )
            retrieval_score = report.overall_score
        except Exception as exc:
            raise SREPhaseError(
                f"Confidence evaluation failed for incident '{incident.incident_id}': {exc}",
                phase="confidence",
                incident_id=incident.incident_id,
                retryable=False,
            ) from exc

        ev_score = _evidence_score(analysis.retrieved_chunk_count)
        an_score = _analysis_score(analysis)
        sev_factor = _severity_penalty(incident)

        composite = (
            _W_RETRIEVAL * retrieval_score
            + _W_EVIDENCE * ev_score
            + _W_ANALYSIS * an_score
            + _W_SEVERITY * sev_factor
        )
        score = round(min(1.0, max(0.0, composite)), 4)
        level = _level(score)
        requires_approval = score < self.approval_threshold

        reasoning = (
            f"Composite score {score:.3f} (retrieval={retrieval_score:.2f}, "
            f"evidence={ev_score:.2f}, analysis={an_score:.2f}, "
            f"severity_factor={sev_factor:.2f}). "
            f"Level={level}. "
            f"{'Approval required.' if requires_approval else 'Autonomous recovery authorised.'}"
        )

        return ConfidenceAssessment(
            score=score,
            level=level,
            requires_approval=requires_approval,
            reasoning=reasoning,
            evaluation_report=report,
        )

    @classmethod
    def with_retrieval_strategies(
        cls,
        engine: EvaluationEngine,
        *,
        approval_threshold: float = 0.65,
        override: bool = False,
    ) -> ConfidenceEvaluator:
        """Factory that registers retrieval strategies before constructing.

        Use this when creating a ``ConfidenceEvaluator`` from a fresh registry.
        """
        register_retrieval_strategies(engine.registry, override=override)
        return cls(engine=engine, approval_threshold=approval_threshold)
