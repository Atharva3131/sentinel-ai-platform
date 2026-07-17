"""ContextQualityScorer — EvaluationStrategy for retrieved context quality."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.evaluation.context import EvaluationContext
from backend.evaluation.models import (
    EvaluationMetric,
    EvaluationMetricKind,
    EvaluationResult,
    EvaluationStatus,
)
from backend.retrieval.models import RetrievalMetadata, RetrievalResult

_EV_RETRIEVED_RESULTS = "retrieved_results"
_EV_TOKEN_BUDGET = "token_budget"
_EV_RETRIEVAL_METADATA = "retrieval_metadata"

_CHARS_PER_TOKEN = 4
_DEFAULT_CONTEXT_TOKEN_THRESHOLD = 4000
_IDEAL_UTILIZATION = 0.8


@dataclass(slots=True)
class ContextQualityScorer:
    """Scores the quality of retrieved context along three dimensions:

    1. **context_relevance** — mean normalized score of retrieved results.
       Measures whether retrieved chunks are ranked highly by the provider.

    2. **context_size** — token count versus available budget.
       Scores 1.0 when within budget, falls off above it.

    3. **token_utilization** — distance from the ideal utilization ratio (0.8).
       Penalizes both over-retrieval (budget waste) and under-retrieval.

    When retrieval metadata is present in evidence, also emits:

    4. **retrieval_latency** — normalised inverse-latency score.

    Evidence keys consumed:
        retrieved_results: list[RetrievalResult]
        token_budget: int | None
        retrieval_metadata: RetrievalMetadata | None

    Implements EvaluationStrategy (structural, no inheritance).
    """

    context_size_threshold: int = _DEFAULT_CONTEXT_TOKEN_THRESHOLD
    relevance_threshold: float = 0.5
    latency_threshold_ms: float = 2000.0
    weight: float = 1.0

    @property
    def name(self) -> str:
        return "retrieval.context_quality"

    @property
    def version(self) -> str | None:
        return "1.0.0"

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        started_at = datetime.now(UTC)
        evidence = context.evidence
        retrieved: list[RetrievalResult] = evidence.get(_EV_RETRIEVED_RESULTS, [])
        token_budget: int | None = evidence.get(_EV_TOKEN_BUDGET)
        ret_meta: RetrievalMetadata | None = evidence.get(_EV_RETRIEVAL_METADATA)

        metrics = self._compute_metrics(retrieved, token_budget, ret_meta)
        score = EvaluationResult.aggregate_score(tuple(metrics))
        status = (
            EvaluationStatus.PASSED
            if all(m.passed for m in metrics)
            else EvaluationStatus.FAILED
        )

        ev_refs: tuple[str, ...] = (_EV_RETRIEVED_RESULTS, _EV_TOKEN_BUDGET)
        if ret_meta is not None:
            ev_refs = (*ev_refs, _EV_RETRIEVAL_METADATA)
        evidence_refs = ev_refs

        return EvaluationResult(
            strategy_name=self.name,
            strategy_version=self.version,
            status=status,
            metrics=tuple(metrics),
            score=score,
            confidence=1.0,
            evidence_refs=evidence_refs,
            started_at=started_at,
            ended_at=datetime.now(UTC),
        )

    def _compute_metrics(
        self,
        retrieved: list[RetrievalResult],
        token_budget: int | None,
        ret_meta: RetrievalMetadata | None,
    ) -> list[EvaluationMetric]:
        metrics: list[EvaluationMetric] = []
        budget = token_budget or self.context_size_threshold

        # 1. context_relevance
        mean_score = (
            sum(r.score for r in retrieved) / len(retrieved)
            if retrieved
            else 0.0
        )
        metrics.append(
            EvaluationMetric(
                name="context_relevance",
                kind=EvaluationMetricKind.CUSTOM,
                value=mean_score,
                score=mean_score,
                passed=mean_score >= self.relevance_threshold,
                threshold=self.relevance_threshold,
                weight=2.0,
            )
        )

        # 2. context_size
        total_chars = sum(len(r.chunk.content) for r in retrieved)
        token_count = total_chars // _CHARS_PER_TOKEN
        utilization = token_count / budget if budget > 0 else 0.0
        over = max(0.0, utilization - 1.0)
        size_score = max(0.0, 1.0 - over)
        metrics.append(
            EvaluationMetric(
                name="context_size",
                kind=EvaluationMetricKind.TOKEN_USAGE,
                value=float(token_count),
                score=size_score,
                passed=utilization <= 1.0,
                unit="tokens",
                threshold=float(budget),
                weight=1.0,
                metadata={"char_count": total_chars},
            )
        )

        # 3. token_utilization (ideal ≈ 80 %)
        util_score = max(0.0, 1.0 - abs(utilization - _IDEAL_UTILIZATION))
        metrics.append(
            EvaluationMetric(
                name="token_utilization",
                kind=EvaluationMetricKind.TOKEN_USAGE,
                value=utilization,
                score=min(1.0, util_score),
                passed=True,
                weight=0.5,
            )
        )

        # 4. retrieval_latency (optional)
        if ret_meta is not None and ret_meta.latency_ms is not None:
            lat = ret_meta.latency_ms
            latency_score = max(0.0, 1.0 - lat / self.latency_threshold_ms)
            metrics.append(
                EvaluationMetric(
                    name="retrieval_latency",
                    kind=EvaluationMetricKind.LATENCY,
                    value=lat,
                    score=latency_score,
                    passed=lat <= self.latency_threshold_ms,
                    unit="ms",
                    threshold=self.latency_threshold_ms,
                    weight=0.5,
                    metadata={"cached": ret_meta.cached},
                )
            )

        return metrics

    def _extra_metadata(
        self, retrieved: list[RetrievalResult], ret_meta: RetrievalMetadata | None
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"result_count": len(retrieved)}
        if ret_meta is not None:
            data["strategy"] = str(ret_meta.strategy)
            data["provider"] = ret_meta.provider_name
            data["cached"] = ret_meta.cached
        return data
