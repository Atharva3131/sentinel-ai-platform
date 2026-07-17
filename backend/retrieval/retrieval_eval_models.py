"""Value objects and port definitions for retrieval evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from backend.evaluation.models import EvaluationReport
from backend.retrieval.models import RetrievalStrategy


@runtime_checkable
class JudgeProvider(Protocol):
    """LLM-as-a-judge port for groundedness scoring.

    Narrow interface so callers can substitute test fakes or any LLM backend
    without coupling to a specific model SDK.
    """

    async def judge(
        self,
        query: str,
        answer: str,
        context_chunks: list[str],
    ) -> float:
        """Return a groundedness score in [0.0, 1.0].

        1.0 = answer is fully grounded in context_chunks.
        0.0 = answer contradicts or ignores context_chunks.
        """
        ...


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    """Scalar summary of one retrieval evaluation run.

    Fields that require ground-truth labels (precision, recall, f1) are None
    when no ground-truth set is provided.
    """

    result_count: int
    context_char_count: int
    context_token_count: int
    cache_hit: bool
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    latency_ms: float | None = None
    token_utilization: float | None = None
    graph_traversal_depth: int | None = None
    graph_quality_score: float | None = None
    strategy: RetrievalStrategy | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """Single retrieval benchmark test case with ground-truth relevance labels."""

    case_id: str
    query: str
    relevant_document_ids: frozenset[str]
    token_budget: int | None = None
    answer: str | None = None
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Outcome of running one BenchmarkCase through the retrieval + evaluation pipeline."""

    case_id: str
    query: str
    metrics: RetrievalMetrics
    retrieved_document_ids: tuple[str, ...]
    evaluation_report: EvaluationReport | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class RetrievalEvaluationReport:
    """Aggregated benchmark report across all BenchmarkCase runs."""

    run_id: str
    cases_run: int
    cases_succeeded: int
    cases_errored: int
    mean_precision: float | None
    mean_recall: float | None
    mean_f1: float | None
    mean_latency_ms: float | None
    cache_hit_ratio: float
    results: tuple[BenchmarkResult, ...]
    started_at: datetime
    ended_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def duration_seconds(self) -> float:
        return max((self.ended_at - self.started_at).total_seconds(), 0.0)
