"""BenchmarkRunner — batch retrieval benchmark with bounded concurrency."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.retrieval.models import RetrievalMetadata, RetrievalResult
from backend.retrieval.retrieval_eval_models import (
    BenchmarkCase,
    BenchmarkResult,
    RetrievalEvaluationReport,
    RetrievalMetrics,
)
from backend.retrieval.retrieval_evaluator import RetrievalEvaluator

RetrievalFn = Callable[
    [str],
    Awaitable[tuple[list[RetrievalResult], RetrievalMetadata]],
]


def _safe_mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass(slots=True)
class BenchmarkRunner:
    """Runs a batch of BenchmarkCase objects against a retrieval function.

    Each case is evaluated independently by ``RetrievalEvaluator.evaluate()``.
    Concurrency is bounded by a semaphore to avoid overwhelming connection pools.
    A failing case is recorded as a BenchmarkResult with ``error`` set; it does
    not abort the remaining batch.

    Usage::

        runner = BenchmarkRunner(evaluator=evaluator, concurrency=8)
        report = await runner.run(cases, retrieval_fn)
    """

    evaluator: RetrievalEvaluator
    concurrency: int = 4

    async def run(
        self,
        cases: list[BenchmarkCase],
        retrieval_fn: RetrievalFn,
        *,
        run_id: str | None = None,
        strategy_names: list[str] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> RetrievalEvaluationReport:
        """Run all benchmark cases and return an aggregated report.

        Args:
            cases: Benchmark test cases to run.
            retrieval_fn: Async callable ``(query) → (results, metadata)``.
            run_id: Stable ID for this benchmark run (auto-generated if absent).
            strategy_names: Override strategies forwarded to the evaluator.
            extra_metadata: Merged into the final report metadata field.
        """
        rid = run_id or str(uuid.uuid4())
        started_at = datetime.now(UTC)

        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [
            asyncio.create_task(
                self._run_one(case, retrieval_fn, semaphore, strategy_names)
            )
            for case in cases
        ]
        results: list[BenchmarkResult] = list(await asyncio.gather(*tasks))

        ended_at = datetime.now(UTC)
        return self._build_report(rid, results, started_at, ended_at, extra_metadata)

    async def _run_one(
        self,
        case: BenchmarkCase,
        retrieval_fn: RetrievalFn,
        semaphore: asyncio.Semaphore,
        strategy_names: list[str] | None,
    ) -> BenchmarkResult:
        async with semaphore:
            try:
                retrieved, meta = await retrieval_fn(case.query)
                metrics, report = await self.evaluator.evaluate(
                    case.query,
                    retrieved,
                    meta,
                    ground_truth_ids=case.relevant_document_ids,
                    token_budget=case.token_budget,
                    answer=case.answer,
                    strategy_names=strategy_names,
                )
                doc_ids = tuple(
                    dict.fromkeys(r.chunk.document_id for r in retrieved)
                )
                return BenchmarkResult(
                    case_id=case.case_id,
                    query=case.query,
                    metrics=metrics,
                    retrieved_document_ids=doc_ids,
                    evaluation_report=report,
                )
            except Exception as exc:
                _empty = RetrievalMetrics(
                    result_count=0,
                    context_char_count=0,
                    context_token_count=0,
                    cache_hit=False,
                )
                return BenchmarkResult(
                    case_id=case.case_id,
                    query=case.query,
                    metrics=_empty,
                    retrieved_document_ids=(),
                    error=str(exc),
                )

    def _build_report(
        self,
        run_id: str,
        results: list[BenchmarkResult],
        started_at: datetime,
        ended_at: datetime,
        extra_metadata: dict[str, Any] | None,
    ) -> RetrievalEvaluationReport:
        succeeded = [r for r in results if r.succeeded]
        errored = [r for r in results if not r.succeeded]

        precisions = [r.metrics.precision for r in succeeded if r.metrics.precision is not None]
        recalls = [r.metrics.recall for r in succeeded if r.metrics.recall is not None]
        f1s = [r.metrics.f1 for r in succeeded if r.metrics.f1 is not None]
        latencies = [r.metrics.latency_ms for r in succeeded if r.metrics.latency_ms is not None]
        cache_hits = sum(1 for r in succeeded if r.metrics.cache_hit)
        cache_hit_ratio = cache_hits / len(succeeded) if succeeded else 0.0

        return RetrievalEvaluationReport(
            run_id=run_id,
            cases_run=len(results),
            cases_succeeded=len(succeeded),
            cases_errored=len(errored),
            mean_precision=_safe_mean(precisions),
            mean_recall=_safe_mean(recalls),
            mean_f1=_safe_mean(f1s),
            mean_latency_ms=_safe_mean(latencies),
            cache_hit_ratio=cache_hit_ratio,
            results=tuple(results),
            started_at=started_at,
            ended_at=ended_at,
            metadata=dict(extra_metadata or {}),
        )
