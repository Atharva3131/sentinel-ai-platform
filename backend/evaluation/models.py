"""Evaluation value objects — metrics, results, reports, and status enumerations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.evaluation.exceptions import EvaluationError


class EvaluationSubject(StrEnum):
    """The entity type being evaluated."""

    WORKFLOW = "workflow"
    AGENT = "agent"
    TOOL = "tool"
    MODEL = "model"
    RETRIEVAL = "retrieval"


class EvaluationStatus(StrEnum):
    """Outcome status of a single strategy evaluation."""

    PASSED = "passed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"
    SKIPPED = "skipped"
    ERROR = "error"


class EvaluationVerdict(StrEnum):
    """Aggregated verdict across all strategies in a pipeline run."""

    PASSED = "passed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class EvaluationMetricKind(StrEnum):
    """Semantic category of an evaluation metric.

    Naming is intentionally stable so evaluation dashboards can group by kind
    without parsing metric names.
    """

    LATENCY = "latency"
    COST = "cost"
    TOOL_SUCCESS = "tool_success"
    WORKFLOW_SUCCESS = "workflow_success"
    AGENT_SUCCESS = "agent_success"
    MEMORY_USAGE = "memory_usage"
    TOKEN_USAGE = "token_usage"
    GROUNDEDNESS = "groundedness"
    HALLUCINATION = "hallucination"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class EvaluationMetric:
    """Immutable measurement from a single evaluation criterion.

    ``score`` is the normalized 0.0-1.0 representation of ``value``.
    ``passed`` is set by the strategy based on its threshold logic.
    ``weight`` determines contribution to the enclosing EvaluationResult's aggregate score.
    """

    name: str
    kind: EvaluationMetricKind
    value: float
    score: float
    passed: bool
    unit: str | None = None
    threshold: float | None = None
    weight: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"EvaluationMetric score must be 0.0-1.0, got {self.score}")
        if self.weight <= 0.0:
            raise ValueError(f"EvaluationMetric weight must be positive, got {self.weight}")


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Immutable output produced by one EvaluationStrategy for a given EvaluationContext.

    ``score`` is the weighted average of metric scores.
    ``confidence`` is the epistemic certainty (1.0 = deterministic, <1.0 = model-assisted).
    ``evidence_refs`` lists the evidence keys consumed from EvaluationContext.evidence.
    """

    strategy_name: str
    strategy_version: str | None
    status: EvaluationStatus
    metrics: tuple[EvaluationMetric, ...]
    score: float
    confidence: float
    evidence_refs: tuple[str, ...]
    started_at: datetime
    ended_at: datetime
    reasoning: str | None = None
    error: EvaluationError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"EvaluationResult score must be 0.0-1.0, got {self.score}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"EvaluationResult confidence must be 0.0-1.0, got {self.confidence}"
            )

    def duration_seconds(self) -> float:
        """Return wall-clock duration of strategy execution."""
        return max((self.ended_at - self.started_at).total_seconds(), 0.0)

    @classmethod
    def aggregate_score(cls, metrics: tuple[EvaluationMetric, ...]) -> float:
        """Compute weighted-average score across a tuple of metrics."""
        if not metrics:
            return 0.0
        total_weight = sum(m.weight for m in metrics)
        if total_weight == 0.0:
            return 0.0
        return sum(m.score * m.weight for m in metrics) / total_weight


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Immutable, self-contained evaluation report produced by an EvaluationPipeline run.

    ``overall_score`` is the strategy-weight-averaged score across active results.
    ``overall_confidence`` is the minimum confidence across active results.
    Active results exclude those with status SKIPPED or ERROR.
    """

    evaluation_id: str
    subject: EvaluationSubject
    subject_id: str
    results: tuple[EvaluationResult, ...]
    verdict: EvaluationVerdict
    overall_score: float
    overall_confidence: float
    started_at: datetime
    ended_at: datetime
    subject_version: str | None = None
    workflow_id: str | None = None
    execution_id: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def duration_seconds(self) -> float:
        """Return total pipeline wall-clock duration."""
        return max((self.ended_at - self.started_at).total_seconds(), 0.0)

    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.status == EvaluationStatus.PASSED)

    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.status == EvaluationStatus.FAILED)

    def indeterminate_count(self) -> int:
        return sum(1 for r in self.results if r.status == EvaluationStatus.INDETERMINATE)

    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.status == EvaluationStatus.SKIPPED)

    def error_count(self) -> int:
        return sum(1 for r in self.results if r.status == EvaluationStatus.ERROR)
