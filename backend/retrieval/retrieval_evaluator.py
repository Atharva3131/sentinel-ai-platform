"""RetrievalEvaluator — coordinates retrieval metrics and evaluation pipeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from backend.evaluation.context import EvaluationContext
from backend.evaluation.engine import EvaluationEngine
from backend.evaluation.models import EvaluationReport, EvaluationSubject
from backend.evaluation.registry import EvaluationRegistry
from backend.retrieval.context_scorer import ContextQualityScorer
from backend.retrieval.groundedness_scorer import GroundednessScorer
from backend.retrieval.models import RetrievalMetadata, RetrievalResult, RetrievalStrategy
from backend.retrieval.retrieval_eval_models import JudgeProvider, RetrievalMetrics

_CHARS_PER_TOKEN = 4

# Scores decrease with traversal depth beyond 2 hops.
_GRAPH_DEPTH_SCORES: dict[int, float] = {1: 1.0, 2: 0.85, 3: 0.65, 4: 0.45}


def _graph_quality_score(depth: int | None) -> float | None:
    if depth is None:
        return None
    return _GRAPH_DEPTH_SCORES.get(depth, 0.25)


def _precision_recall_f1(
    retrieved: list[RetrievalResult],
    ground_truth_ids: frozenset[str],
) -> tuple[float | None, float | None, float | None]:
    if not ground_truth_ids:
        return None, None, None
    retrieved_ids = frozenset(r.chunk.document_id for r in retrieved)
    tp = len(retrieved_ids & ground_truth_ids)
    precision = tp / len(retrieved_ids) if retrieved_ids else 0.0
    recall = tp / len(ground_truth_ids)
    denom = precision + recall
    f1 = 2 * precision * recall / denom if denom > 0 else 0.0
    return precision, recall, f1


def register_retrieval_strategies(
    registry: EvaluationRegistry,
    *,
    context_scorer: ContextQualityScorer | None = None,
    groundedness_scorer: GroundednessScorer | None = None,
    judge: JudgeProvider | None = None,
    override: bool = False,
) -> None:
    """Register built-in retrieval evaluation strategies with an EvaluationRegistry.

    Call this once during application bootstrap, before constructing a
    RetrievalEvaluator, so the engine can resolve strategy names.

    Args:
        registry: The registry to populate.
        context_scorer: Optional custom ContextQualityScorer instance.
        groundedness_scorer: Optional custom GroundednessScorer instance.
        judge: JudgeProvider wired into the groundedness scorer when not
               explicitly supplying a custom scorer.
        override: Pass True to re-register already-registered versions.
    """
    cs = context_scorer or ContextQualityScorer()
    gs = groundedness_scorer or GroundednessScorer(judge=judge)

    registry.register(
        name=cs.name,
        version=cs.version or "1.0.0",
        provider=lambda _: cs,
        override=override,
    )
    registry.register(
        name=gs.name,
        version=gs.version or "1.0.0",
        provider=lambda _: gs,
        override=override,
    )


@dataclass(slots=True)
class RetrievalEvaluator:
    """Coordinates retrieval-specific metrics and evaluation pipeline execution.

    Two outputs per evaluation call:

    1. **RetrievalMetrics** — always computed; requires only retrieved results
       and retrieval metadata. Ground-truth labels are optional: precision,
       recall, and F1 are None when not supplied.

    2. **EvaluationReport** — produced by running the configured strategies
       through the evaluation engine. Requires strategies to be pre-registered
       via ``register_retrieval_strategies()``.

    Usage::

        registry = EvaluationRegistry()
        register_retrieval_strategies(registry)
        engine = EvaluationEngine(registry=registry)
        evaluator = RetrievalEvaluator(engine=engine)

        metrics, report = await evaluator.evaluate(
            query="...",
            retrieved_results=results,
            retrieval_metadata=meta,
            ground_truth_ids=frozenset({"doc-1", "doc-2"}),
        )
    """

    engine: EvaluationEngine
    default_strategies: tuple[str, ...] = (
        "retrieval.context_quality",
        "retrieval.groundedness",
    )

    async def evaluate(
        self,
        query: str,
        retrieved_results: list[RetrievalResult],
        retrieval_metadata: RetrievalMetadata,
        *,
        ground_truth_ids: frozenset[str] = frozenset(),
        token_budget: int | None = None,
        answer: str | None = None,
        strategy_names: list[str] | None = None,
        evaluation_id: str | None = None,
        tenant_id: str | None = None,
        extra_evidence: dict[str, Any] | None = None,
    ) -> tuple[RetrievalMetrics, EvaluationReport]:
        """Run retrieval evaluation and return metrics + evaluation report.

        Args:
            query: The query string used for retrieval.
            retrieved_results: Ordered list of RetrievalResult objects.
            retrieval_metadata: Provenance and timing metadata from the pipeline.
            ground_truth_ids: Document IDs considered relevant (enables P/R/F1).
            token_budget: Token budget available for context; used in scoring.
            answer: Candidate answer for groundedness scoring.
            strategy_names: Override the default set of evaluation strategies.
            evaluation_id: Stable ID for this evaluation run (auto-generated if absent).
            tenant_id: Optional tenant scope forwarded to the evaluation context.
            extra_evidence: Additional evidence merged into the EvaluationContext.
        """
        eval_id = evaluation_id or str(uuid.uuid4())
        metrics = self._compute_metrics(
            retrieved_results, retrieval_metadata, ground_truth_ids, token_budget
        )
        evidence: dict[str, Any] = {
            "query": query,
            "answer": answer,
            "retrieved_results": retrieved_results,
            "retrieval_metadata": retrieval_metadata,
            "ground_truth_ids": ground_truth_ids,
            "token_budget": token_budget,
            **(extra_evidence or {}),
        }
        eval_context = EvaluationContext(
            evaluation_id=eval_id,
            subject=EvaluationSubject.RETRIEVAL,
            subject_id=retrieval_metadata.retrieval_id,
            subject_version=retrieval_metadata.provider_version,
            evidence=evidence,
            tenant_id=tenant_id,
        )
        names = list(strategy_names or self.default_strategies)
        report = await self.engine.evaluate(names, eval_context)
        return metrics, report

    def _compute_metrics(
        self,
        retrieved: list[RetrievalResult],
        ret_meta: RetrievalMetadata,
        ground_truth_ids: frozenset[str],
        token_budget: int | None,
    ) -> RetrievalMetrics:
        precision, recall, f1 = _precision_recall_f1(retrieved, ground_truth_ids)
        total_chars = sum(len(r.chunk.content) for r in retrieved)
        token_count = total_chars // _CHARS_PER_TOKEN
        utilization = (
            token_count / token_budget
            if token_budget and token_budget > 0
            else None
        )
        depth = ret_meta.graph_depth
        strategy: RetrievalStrategy | None = ret_meta.strategy if isinstance(
            ret_meta.strategy, RetrievalStrategy
        ) else None
        return RetrievalMetrics(
            result_count=len(retrieved),
            context_char_count=total_chars,
            context_token_count=token_count,
            cache_hit=ret_meta.cached,
            precision=precision,
            recall=recall,
            f1=f1,
            latency_ms=ret_meta.latency_ms,
            token_utilization=utilization,
            graph_traversal_depth=depth,
            graph_quality_score=_graph_quality_score(depth),
            strategy=strategy,
        )
