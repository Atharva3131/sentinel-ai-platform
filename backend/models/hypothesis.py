"""Hypothesis and RCA domain models — investigation and root cause analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class HypothesisStatus(StrEnum):
    """Evaluation state of a single hypothesis."""

    PROPOSED = "proposed"
    SUPPORTED = "supported"      # evidence supports it
    REFUTED = "refuted"          # evidence contradicts it
    INCONCLUSIVE = "inconclusive"  # insufficient evidence to decide
    CONFIRMED = "confirmed"      # accepted as root cause


@dataclass(frozen=True, slots=True)
class HypothesisEvaluation:
    """The result of evaluating one hypothesis against available evidence.

    ``confidence`` is a 0.0-1.0 score.  ``supporting_evidence_ids`` and
    ``contradicting_evidence_ids`` reference ``Evidence.evidence_id`` values
    from the ``EvidenceCollection``.  ``reasoning`` is a human-readable
    explanation of the evaluation decision.
    """

    hypothesis_id: str
    status: HypothesisStatus
    confidence: float
    supporting_evidence_ids: tuple[str, ...]
    contradicting_evidence_ids: tuple[str, ...]
    reasoning: str
    evaluated_at: datetime
    evaluator: str = "hypothesis-engine"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"HypothesisEvaluation confidence must be 0.0-1.0, got {self.confidence}"
            )


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """A candidate explanation for the observed incident.

    Hypotheses are generated from evidence patterns, retrieved knowledge,
    and model-assisted reasoning.  Each one is evaluated independently
    before the RCA engine selects the highest-confidence candidate.

    The platform does not hard-code specific failure types — hypotheses are
    free-form explanations that the investigation engine produces from
    whatever evidence is available.
    """

    hypothesis_id: str
    incident_id: str
    title: str
    description: str
    proposed_at: datetime
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    confidence: float = 0.0
    evaluation: HypothesisEvaluation | None = None
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    reasoning_metadata: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"Hypothesis confidence must be 0.0-1.0, got {self.confidence}"
            )

    def with_evaluation(self, evaluation: HypothesisEvaluation) -> Hypothesis:
        """Return a copy with the evaluation applied."""
        from dataclasses import replace
        return replace(
            self,
            evaluation=evaluation,
            status=evaluation.status,
            confidence=evaluation.confidence,
            supporting_evidence_ids=evaluation.supporting_evidence_ids,
            contradicting_evidence_ids=evaluation.contradicting_evidence_ids,
        )


@dataclass(frozen=True, slots=True)
class RootCauseAnalysis:
    """The output of the RCA engine for one incident investigation.

    ``root_cause`` is the accepted hypothesis when one could be determined.
    When no hypothesis reaches the confidence threshold, ``root_cause`` is
    None and ``unresolved_uncertainty`` describes what is missing.

    ``observed_symptoms``, ``correlated_evidence_ids``, ``candidate_hypotheses``,
    and ``evaluated_hypotheses`` provide the full investigation audit trail.
    """

    rca_id: str
    incident_id: str
    observed_symptoms: tuple[str, ...]
    correlated_evidence_ids: tuple[str, ...]
    candidate_hypotheses: tuple[Hypothesis, ...]
    evaluated_hypotheses: tuple[Hypothesis, ...]
    root_cause: Hypothesis | None
    confidence: float
    unresolved_uncertainty: str | None
    produced_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"RootCauseAnalysis confidence must be 0.0-1.0, got {self.confidence}"
            )

    @property
    def has_root_cause(self) -> bool:
        return self.root_cause is not None

    @property
    def root_cause_title(self) -> str | None:
        return self.root_cause.title if self.root_cause else None
