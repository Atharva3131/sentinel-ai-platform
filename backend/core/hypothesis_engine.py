"""HypothesisEngine — generates and evaluates hypotheses from collected evidence.

The engine is fully incident-type agnostic.  It does not contain any
hard-coded knowledge of specific failure modes (no "if connection_leak…").
It generates hypotheses from evidence patterns and evaluates them using
the scoring model.

Design:
  1. Pattern matching across evidence items produces candidate hypotheses.
  2. Each hypothesis is evaluated by examining supporting vs. contradicting evidence.
  3. The highest-confidence confirmed hypothesis becomes the root cause.
  4. When no hypothesis reaches the acceptance threshold, the RCA is inconclusive.

For production, the ``HypothesisGenerator`` protocol can be satisfied by an
LLM-backed agent that reasons over evidence.  The deterministic fallback
generates hypotheses from evidence tags and structured data — suitable for
testing and for cases where the LLM is unavailable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from backend.models.evidence import Evidence, EvidenceCollection, EvidenceStatus
from backend.models.hypothesis import (
    Hypothesis,
    HypothesisEvaluation,
    HypothesisStatus,
    RootCauseAnalysis,
)
from backend.models.incident import Incident

_ACCEPTANCE_THRESHOLD = 0.60   # minimum confidence to accept a hypothesis as root cause
_SUPPORTING_WEIGHT = 1.0
_CONTRADICTING_WEIGHT = -0.5


@runtime_checkable
class HypothesisGenerator(Protocol):
    """Port that produces candidate hypotheses from evidence.

    The deterministic engine uses heuristics; a production system wires an
    LLM-backed generator here.
    """

    async def generate(
        self,
        incident: Incident,
        evidence: EvidenceCollection,
        *,
        max_hypotheses: int = 5,
        context: dict[str, Any] | None = None,
    ) -> list[Hypothesis]:
        """Return a list of candidate hypotheses for *incident*."""
        ...


class DeterministicHypothesisGenerator:
    """Heuristic generator — produces hypotheses from evidence patterns.

    Used in tests, development, and as a fallback when no LLM is available.
    Does NOT contain any hard-coded failure-mode branching.
    """

    async def generate(
        self,
        incident: Incident,
        evidence: EvidenceCollection,
        *,
        max_hypotheses: int = 5,
        context: dict[str, Any] | None = None,
    ) -> list[Hypothesis]:
        now = datetime.now(UTC)
        hypotheses: list[Hypothesis] = []

        # Group evidence by source kind to detect multi-source patterns
        by_kind: dict[str, list[Evidence]] = {}
        for item in evidence.items:
            key = item.source.kind.value
            by_kind.setdefault(key, []).append(item)

        # Hypothesis 1: if we have deployment evidence, recent deployment may be root cause
        if "deployments" in by_kind:
            for dep_ev in by_kind["deployments"][:2]:
                svc = dep_ev.structured_data.get("service", "unknown")
                ver = dep_ev.structured_data.get("version", "unknown")
                hypotheses.append(Hypothesis(
                    hypothesis_id=str(uuid.uuid4()),
                    incident_id=incident.incident_id,
                    title=f"Recent deployment regression: {svc} @ {ver}",
                    description=(
                        f"A recent deployment of {svc} (version {ver}) may have "
                        f"introduced a regression causing the observed symptoms."
                    ),
                    proposed_at=now,
                    supporting_evidence_ids=(dep_ev.evidence_id,),
                ))

        # Hypothesis 2: if we have metrics evidence, resource exhaustion
        if "metrics" in by_kind:
            services = list({
                e.structured_data.get("service", "")
                for e in by_kind["metrics"]
                if e.structured_data.get("service")
            })
            if services:
                hypotheses.append(Hypothesis(
                    hypothesis_id=str(uuid.uuid4()),
                    incident_id=incident.incident_id,
                    title=f"Resource exhaustion: {', '.join(services[:3])}",
                    description=(
                        f"Metric anomalies detected across {len(services)} service(s). "
                        f"Resource exhaustion or saturation may be the root cause."
                    ),
                    proposed_at=now,
                    supporting_evidence_ids=tuple(e.evidence_id for e in by_kind["metrics"][:3]),
                ))

        # Hypothesis 3: if we have log evidence with error patterns
        if "logs" in by_kind:
            log_services = list({
                e.structured_data.get("service", "")
                for e in by_kind["logs"]
                if e.structured_data.get("service")
            })
            if log_services:
                hypotheses.append(Hypothesis(
                    hypothesis_id=str(uuid.uuid4()),
                    incident_id=incident.incident_id,
                    title=f"Application error pattern: {', '.join(log_services[:3])}",
                    description=(
                        f"Recurring error log patterns observed in "
                        f"{', '.join(log_services[:3])}. "
                        f"An application-level fault may be responsible."
                    ),
                    proposed_at=now,
                    supporting_evidence_ids=tuple(e.evidence_id for e in by_kind["logs"][:3]),
                ))

        # Hypothesis 4: if multiple symptoms and no specific pattern, general degradation
        if incident.symptoms and not hypotheses:
            hypotheses.append(Hypothesis(
                hypothesis_id=str(uuid.uuid4()),
                incident_id=incident.incident_id,
                title=f"Service degradation: {', '.join(incident.affected_services[:3])}",
                description=(
                    f"Symptoms observed: {'; '.join(incident.symptoms[:3])}. "
                    f"Cause requires further investigation."
                ),
                proposed_at=now,
            ))

        # Generic fallback
        if not hypotheses:
            hypotheses.append(Hypothesis(
                hypothesis_id=str(uuid.uuid4()),
                incident_id=incident.incident_id,
                title=f"Unknown cause for incident {incident.incident_id}",
                description="Insufficient evidence to determine root cause.",
                proposed_at=now,
            ))

        return hypotheses[:max_hypotheses]


@dataclass
class HypothesisEngine:
    """Generates and evaluates hypotheses, producing a RootCauseAnalysis.

    ``generator`` produces candidates; the engine scores each one against
    the collected evidence and selects the best-supported hypothesis.

    The acceptance threshold and scoring weights are configurable —
    no hard-coded values for specific incident types.
    """

    generator: HypothesisGenerator
    acceptance_threshold: float = _ACCEPTANCE_THRESHOLD
    max_hypotheses: int = 5

    async def analyze(
        self,
        incident: Incident,
        evidence: EvidenceCollection,
        *,
        context: dict[str, Any] | None = None,
    ) -> RootCauseAnalysis:
        """Run the full investigation cycle and produce a RootCauseAnalysis.

        Steps:
          1. Generate candidate hypotheses from evidence.
          2. Evaluate each candidate against the evidence collection.
          3. Select the highest-confidence accepted hypothesis as root cause.
          4. Return the RCA with full audit trail.
        """
        now = datetime.now(UTC)

        candidates = await self.generator.generate(
            incident,
            evidence,
            max_hypotheses=self.max_hypotheses,
            context=context,
        )

        evaluated: list[Hypothesis] = []
        for candidate in candidates:
            evaluation = self._evaluate(candidate, evidence)
            evaluated.append(candidate.with_evaluation(evaluation))

        # Sort by descending confidence; pick first that exceeds threshold
        evaluated.sort(key=lambda h: h.confidence, reverse=True)
        root_cause = next(
            (h for h in evaluated if h.confidence >= self.acceptance_threshold),
            None,
        )

        # If no hypothesis meets the threshold, pick the best one but mark inconclusive
        overall_confidence = evaluated[0].confidence if evaluated else 0.0
        uncertainty: str | None = None
        if root_cause is None:
            uncertainty = (
                f"No hypothesis reached the acceptance threshold "
                f"({self.acceptance_threshold:.0%}). "
                f"Best confidence: {overall_confidence:.0%}. "
                f"More evidence may be required."
            )

        return RootCauseAnalysis(
            rca_id=str(uuid.uuid4()),
            incident_id=incident.incident_id,
            observed_symptoms=incident.symptoms,
            correlated_evidence_ids=tuple(e.evidence_id for e in evidence.items),
            candidate_hypotheses=tuple(candidates),
            evaluated_hypotheses=tuple(evaluated),
            root_cause=root_cause,
            confidence=root_cause.confidence if root_cause else overall_confidence,
            unresolved_uncertainty=uncertainty,
            produced_at=now,
        )

    def _evaluate(
        self,
        hypothesis: Hypothesis,
        evidence: EvidenceCollection,
    ) -> HypothesisEvaluation:
        """Score a hypothesis against the evidence collection.

        Supporting evidence that is CONFIRMED adds positive weight.
        Evidence that contradicts (CONTRADICTORY status) subtracts weight.
        The raw score is normalised to 0.0-1.0.
        """
        now = datetime.now(UTC)
        evidence_by_id = {e.evidence_id: e for e in evidence.items}

        supporting_ids = list(hypothesis.supporting_evidence_ids)
        contradicting_ids = list(hypothesis.contradicting_evidence_ids)

        # Also check evidence correlations for co-located supporting items
        for corr in evidence.correlations:
            if corr.evidence_id_a in supporting_ids and corr.evidence_id_b not in supporting_ids:
                supporting_ids.append(corr.evidence_id_b)
            elif corr.evidence_id_b in supporting_ids and corr.evidence_id_a not in supporting_ids:
                supporting_ids.append(corr.evidence_id_a)

        # Score
        raw = 0.0
        for eid in supporting_ids:
            ev = evidence_by_id.get(eid)
            if ev and ev.status not in (EvidenceStatus.UNAVAILABLE, EvidenceStatus.STALE):
                raw += _SUPPORTING_WEIGHT * ev.relevance_score

        for eid in contradicting_ids:
            ev = evidence_by_id.get(eid)
            if ev and ev.status == EvidenceStatus.CONFIRMED:
                raw += _CONTRADICTING_WEIGHT * ev.relevance_score

        # Normalise: supporting_ids gives the max achievable; clamp to 0-1
        max_achievable = _SUPPORTING_WEIGHT * len(supporting_ids) if supporting_ids else 1.0
        confidence = max(0.0, min(1.0, raw / max_achievable)) if max_achievable > 0 else 0.0

        if confidence >= self.acceptance_threshold:
            status = HypothesisStatus.SUPPORTED
        elif confidence < 0.20:
            status = HypothesisStatus.REFUTED
        else:
            status = HypothesisStatus.INCONCLUSIVE

        reasoning = (
            f"Score {confidence:.3f} from {len(supporting_ids)} supporting "
            f"and {len(contradicting_ids)} contradicting evidence items."
        )

        return HypothesisEvaluation(
            hypothesis_id=hypothesis.hypothesis_id,
            status=status,
            confidence=round(confidence, 4),
            supporting_evidence_ids=tuple(supporting_ids),
            contradicting_evidence_ids=tuple(contradicting_ids),
            reasoning=reasoning,
            evaluated_at=now,
        )
