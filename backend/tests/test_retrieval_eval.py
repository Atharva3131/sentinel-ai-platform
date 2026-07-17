"""Tests for retrieval evaluation — RetrievalEvaluator, scorers, BenchmarkRunner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from backend.evaluation.engine import EvaluationEngine
from backend.evaluation.models import EvaluationStatus
from backend.evaluation.registry import EvaluationRegistry
from backend.retrieval.benchmark_runner import BenchmarkRunner
from backend.retrieval.context_scorer import ContextQualityScorer
from backend.retrieval.groundedness_scorer import GroundednessScorer, _heuristic_groundedness
from backend.retrieval.models import (
    Chunk,
    DocumentSource,
    RetrievalMetadata,
    RetrievalResult,
    RetrievalStrategy,
)
from backend.retrieval.retrieval_eval_models import (
    BenchmarkCase,
    RetrievalMetrics,
)
from backend.retrieval.retrieval_evaluator import (
    RetrievalEvaluator,
    _graph_quality_score,
    _precision_recall_f1,
    register_retrieval_strategies,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeJudgeProvider:
    _score: float = 0.8

    async def judge(
        self, query: str, answer: str, context_chunks: list[str]
    ) -> float:
        return self._score


def _make_chunk(document_id: str, content: str, index: int = 0) -> Chunk:
    return Chunk.make(document_id=document_id, content=content, index=index)


def _make_result(document_id: str, content: str, score: float = 0.8) -> RetrievalResult:
    chunk = _make_chunk(document_id, content)
    return RetrievalResult.from_chunk(
        chunk,
        score,
        retrieval_id="test-retrieval",
        source=DocumentSource.DATABASE,
    )


def _make_metadata(
    *,
    cached: bool = False,
    latency_ms: float | None = 100.0,
    graph_depth: int | None = None,
    strategy: RetrievalStrategy = RetrievalStrategy.VECTOR,
) -> RetrievalMetadata:
    return RetrievalMetadata(
        retrieval_id="test-retrieval",
        strategy=strategy,
        provider_name="fake-provider",
        latency_ms=latency_ms,
        total_candidates=10,
        cached=cached,
        graph_depth=graph_depth,
    )


def _make_engine(override: bool = False) -> tuple[EvaluationEngine, EvaluationRegistry]:
    registry = EvaluationRegistry()
    register_retrieval_strategies(registry, override=override)
    engine = EvaluationEngine(registry=registry)
    return engine, registry


def _make_evaluator(**kwargs: Any) -> RetrievalEvaluator:
    engine, _ = _make_engine()
    return RetrievalEvaluator(engine=engine, **kwargs)


# ---------------------------------------------------------------------------
# _heuristic_groundedness
# ---------------------------------------------------------------------------


class TestHeuristicGroundedness:
    def test_full_overlap(self) -> None:
        score = _heuristic_groundedness("the cat sat on the mat", ["the cat sat on the mat"])
        assert score == pytest.approx(1.0)

    def test_no_overlap(self) -> None:
        score = _heuristic_groundedness("dogs bark loudly", ["cats meow quietly"])
        assert score == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        score = _heuristic_groundedness("the cat sat", ["the cat is here"])
        # "the", "cat" match out of "the", "cat", "sat" → 2/3
        assert 0.0 < score < 1.0

    def test_empty_answer(self) -> None:
        assert _heuristic_groundedness("", ["some context"]) == pytest.approx(0.0)

    def test_empty_chunks(self) -> None:
        assert _heuristic_groundedness("some answer", []) == pytest.approx(0.0)

    def test_case_insensitive(self) -> None:
        s1 = _heuristic_groundedness("The Cat", ["the cat"])
        s2 = _heuristic_groundedness("the cat", ["the cat"])
        assert s1 == pytest.approx(s2)


# ---------------------------------------------------------------------------
# _precision_recall_f1
# ---------------------------------------------------------------------------


class TestPrecisionRecallF1:
    def test_no_ground_truth_returns_none(self) -> None:
        results = [_make_result("doc-1", "content")]
        p, r, f = _precision_recall_f1(results, frozenset())
        assert p is None and r is None and f is None

    def test_perfect_precision_and_recall(self) -> None:
        results = [_make_result("doc-1", "a"), _make_result("doc-2", "b")]
        gt = frozenset({"doc-1", "doc-2"})
        p, r, f = _precision_recall_f1(results, gt)
        assert p == pytest.approx(1.0)
        assert r == pytest.approx(1.0)
        assert f == pytest.approx(1.0)

    def test_zero_precision_and_recall(self) -> None:
        results = [_make_result("doc-x", "content")]
        gt = frozenset({"doc-1", "doc-2"})
        p, r, f = _precision_recall_f1(results, gt)
        assert p == pytest.approx(0.0)
        assert r == pytest.approx(0.0)
        assert f == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        results = [_make_result("doc-1", "a"), _make_result("doc-x", "b")]
        gt = frozenset({"doc-1", "doc-2"})
        p, r, f = _precision_recall_f1(results, gt)
        assert p == pytest.approx(0.5)  # 1 of 2 retrieved is relevant
        assert r == pytest.approx(0.5)  # 1 of 2 relevant retrieved
        assert f is not None and 0.0 < f < 1.0

    def test_empty_retrieved_returns_zero_precision(self) -> None:
        p, r, f = _precision_recall_f1([], frozenset({"doc-1"}))
        assert p == pytest.approx(0.0)
        assert r == pytest.approx(0.0)
        assert f == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _graph_quality_score
# ---------------------------------------------------------------------------


class TestGraphQualityScore:
    def test_none_depth_returns_none(self) -> None:
        assert _graph_quality_score(None) is None

    def test_depth_1_is_highest(self) -> None:
        assert _graph_quality_score(1) == pytest.approx(1.0)

    def test_depth_2_lower_than_1(self) -> None:
        assert _graph_quality_score(2) < _graph_quality_score(1)  # type: ignore[operator]

    def test_depth_5_is_low(self) -> None:
        assert _graph_quality_score(5) == pytest.approx(0.25)

    def test_deep_depth_is_minimum(self) -> None:
        assert _graph_quality_score(10) == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# ContextQualityScorer
# ---------------------------------------------------------------------------


class TestContextQualityScorer:
    @pytest.fixture()
    def scorer(self) -> ContextQualityScorer:
        return ContextQualityScorer()

    @pytest.mark.asyncio()
    async def test_no_results_fails_relevance(
        self, scorer: ContextQualityScorer
    ) -> None:
        from backend.evaluation.context import EvaluationContext
        from backend.evaluation.models import EvaluationSubject

        ctx = EvaluationContext(
            evaluation_id="e1",
            subject=EvaluationSubject.RETRIEVAL,
            subject_id="s1",
            evidence={"retrieved_results": []},
        )
        result = await scorer.evaluate(ctx)
        assert result.status == EvaluationStatus.FAILED
        relevance = next(m for m in result.metrics if m.name == "context_relevance")
        assert relevance.score == pytest.approx(0.0)

    @pytest.mark.asyncio()
    async def test_high_relevance_passes(self, scorer: ContextQualityScorer) -> None:
        from backend.evaluation.context import EvaluationContext
        from backend.evaluation.models import EvaluationSubject

        results = [_make_result("d1", "content", score=0.9)]
        ctx = EvaluationContext(
            evaluation_id="e2",
            subject=EvaluationSubject.RETRIEVAL,
            subject_id="s1",
            evidence={"retrieved_results": results},
        )
        result = await scorer.evaluate(ctx)
        rel = next(m for m in result.metrics if m.name == "context_relevance")
        assert rel.passed

    @pytest.mark.asyncio()
    async def test_latency_metric_added_when_metadata_present(
        self, scorer: ContextQualityScorer
    ) -> None:
        from backend.evaluation.context import EvaluationContext
        from backend.evaluation.models import EvaluationSubject

        meta = _make_metadata(latency_ms=500.0)
        ctx = EvaluationContext(
            evaluation_id="e3",
            subject=EvaluationSubject.RETRIEVAL,
            subject_id="s1",
            evidence={"retrieved_results": [], "retrieval_metadata": meta},
        )
        result = await scorer.evaluate(ctx)
        names = {m.name for m in result.metrics}
        assert "retrieval_latency" in names

    @pytest.mark.asyncio()
    async def test_over_budget_fails_size_metric(
        self, scorer: ContextQualityScorer
    ) -> None:
        from backend.evaluation.context import EvaluationContext
        from backend.evaluation.models import EvaluationSubject

        # ~500 tokens of content, budget = 100 tokens
        results = [_make_result("d1", "x" * 2000)]
        ctx = EvaluationContext(
            evaluation_id="e4",
            subject=EvaluationSubject.RETRIEVAL,
            subject_id="s1",
            evidence={"retrieved_results": results, "token_budget": 100},
        )
        result = await scorer.evaluate(ctx)
        size_m = next(m for m in result.metrics if m.name == "context_size")
        assert not size_m.passed

    @pytest.mark.asyncio()
    async def test_score_is_0_to_1(self, scorer: ContextQualityScorer) -> None:
        from backend.evaluation.context import EvaluationContext
        from backend.evaluation.models import EvaluationSubject

        ctx = EvaluationContext(
            evaluation_id="e5",
            subject=EvaluationSubject.RETRIEVAL,
            subject_id="s1",
            evidence={"retrieved_results": [_make_result("d1", "hello")]},
        )
        result = await scorer.evaluate(ctx)
        assert 0.0 <= result.score <= 1.0

    @pytest.mark.asyncio()
    async def test_high_latency_fails_latency_metric(
        self, scorer: ContextQualityScorer
    ) -> None:
        from backend.evaluation.context import EvaluationContext
        from backend.evaluation.models import EvaluationSubject

        meta = _make_metadata(latency_ms=5000.0)
        ctx = EvaluationContext(
            evaluation_id="e6",
            subject=EvaluationSubject.RETRIEVAL,
            subject_id="s1",
            evidence={"retrieved_results": [], "retrieval_metadata": meta},
        )
        result = await scorer.evaluate(ctx)
        lat = next(m for m in result.metrics if m.name == "retrieval_latency")
        assert not lat.passed


# ---------------------------------------------------------------------------
# GroundednessScorer
# ---------------------------------------------------------------------------


class TestGroundednessScorer:
    @pytest.fixture()
    def scorer(self) -> GroundednessScorer:
        return GroundednessScorer()

    def _ctx(self, evidence: dict[str, Any]) -> Any:
        from backend.evaluation.context import EvaluationContext
        from backend.evaluation.models import EvaluationSubject

        return EvaluationContext(
            evaluation_id="g1",
            subject=EvaluationSubject.RETRIEVAL,
            subject_id="s1",
            evidence=evidence,
        )

    @pytest.mark.asyncio()
    async def test_no_answer_returns_skipped(
        self, scorer: GroundednessScorer
    ) -> None:
        ctx = self._ctx({"retrieved_results": []})
        result = await scorer.evaluate(ctx)
        assert result.status == EvaluationStatus.SKIPPED

    @pytest.mark.asyncio()
    async def test_high_overlap_passes(self, scorer: GroundednessScorer) -> None:
        results = [_make_result("d1", "the cat sat on the mat")]
        ctx = self._ctx(
            {"answer": "the cat sat on the mat", "retrieved_results": results}
        )
        result = await scorer.evaluate(ctx)
        assert result.status == EvaluationStatus.PASSED
        assert result.score == pytest.approx(1.0)

    @pytest.mark.asyncio()
    async def test_low_overlap_fails(self, scorer: GroundednessScorer) -> None:
        results = [_make_result("d1", "dogs bark loudly in the park")]
        ctx = self._ctx(
            {"answer": "cats meow", "retrieved_results": results}
        )
        result = await scorer.evaluate(ctx)
        # "cats" and "meow" are not in context → score = 0.0
        assert result.score == pytest.approx(0.0)
        assert result.status == EvaluationStatus.FAILED

    @pytest.mark.asyncio()
    async def test_llm_judge_used_when_provided(self) -> None:
        judge = FakeJudgeProvider(_score=0.95)
        scorer = GroundednessScorer(judge=judge)
        results = [_make_result("d1", "irrelevant content")]
        ctx = self._ctx(
            {"answer": "completely different", "retrieved_results": results}
        )
        result = await scorer.evaluate(ctx)
        assert result.score == pytest.approx(0.95)
        assert result.confidence == pytest.approx(0.9)

    @pytest.mark.asyncio()
    async def test_heuristic_mode_has_full_confidence(
        self, scorer: GroundednessScorer
    ) -> None:
        results = [_make_result("d1", "hello world")]
        ctx = self._ctx({"answer": "hello", "retrieved_results": results})
        result = await scorer.evaluate(ctx)
        assert result.confidence == pytest.approx(1.0)

    @pytest.mark.asyncio()
    async def test_score_clamped_to_unit_interval(self) -> None:
        judge = FakeJudgeProvider(_score=1.5)
        scorer = GroundednessScorer(judge=judge)
        results = [_make_result("d1", "content")]
        ctx = self._ctx({"answer": "answer", "retrieved_results": results})
        result = await scorer.evaluate(ctx)
        assert result.score <= 1.0

    @pytest.mark.asyncio()
    async def test_evidence_refs_include_all_keys(
        self, scorer: GroundednessScorer
    ) -> None:
        results = [_make_result("d1", "content")]
        ctx = self._ctx(
            {"query": "q", "answer": "answer", "retrieved_results": results}
        )
        result = await scorer.evaluate(ctx)
        assert "query" in result.evidence_refs
        assert "answer" in result.evidence_refs
        assert "retrieved_results" in result.evidence_refs


# ---------------------------------------------------------------------------
# RetrievalEvaluator
# ---------------------------------------------------------------------------


class TestRetrievalEvaluator:
    @pytest.mark.asyncio()
    async def test_returns_metrics_and_report(self) -> None:
        evaluator = _make_evaluator()
        results = [_make_result("d1", "content about kubernetes")]
        meta = _make_metadata(latency_ms=50.0)
        metrics, report = await evaluator.evaluate(
            "kubernetes pods",
            results,
            meta,
        )
        assert isinstance(metrics, RetrievalMetrics)
        assert metrics.result_count == 1
        assert metrics.latency_ms == pytest.approx(50.0)
        assert report is not None

    @pytest.mark.asyncio()
    async def test_precision_recall_computed_with_ground_truth(self) -> None:
        evaluator = _make_evaluator()
        results = [
            _make_result("doc-1", "relevant content"),
            _make_result("doc-x", "irrelevant"),
        ]
        meta = _make_metadata()
        metrics, _ = await evaluator.evaluate(
            "query",
            results,
            meta,
            ground_truth_ids=frozenset({"doc-1", "doc-2"}),
        )
        assert metrics.precision == pytest.approx(0.5)
        assert metrics.recall == pytest.approx(0.5)
        assert metrics.f1 is not None

    @pytest.mark.asyncio()
    async def test_no_ground_truth_yields_none_pr(self) -> None:
        evaluator = _make_evaluator()
        results = [_make_result("d1", "content")]
        meta = _make_metadata()
        metrics, _ = await evaluator.evaluate("q", results, meta)
        assert metrics.precision is None
        assert metrics.recall is None
        assert metrics.f1 is None

    @pytest.mark.asyncio()
    async def test_cache_hit_recorded(self) -> None:
        evaluator = _make_evaluator()
        meta = _make_metadata(cached=True)
        metrics, _ = await evaluator.evaluate("q", [], meta)
        assert metrics.cache_hit is True

    @pytest.mark.asyncio()
    async def test_graph_depth_and_quality_recorded(self) -> None:
        evaluator = _make_evaluator()
        meta = _make_metadata(graph_depth=2, strategy=RetrievalStrategy.GRAPH)
        metrics, _ = await evaluator.evaluate("q", [], meta)
        assert metrics.graph_traversal_depth == 2
        assert metrics.graph_quality_score == pytest.approx(0.85)

    @pytest.mark.asyncio()
    async def test_token_utilization_computed(self) -> None:
        evaluator = _make_evaluator()
        results = [_make_result("d1", "a" * 400)]  # 400 chars = 100 tokens
        meta = _make_metadata()
        metrics, _ = await evaluator.evaluate(
            "q", results, meta, token_budget=200
        )
        assert metrics.token_utilization == pytest.approx(0.5)

    @pytest.mark.asyncio()
    async def test_custom_strategy_names_respected(self) -> None:
        evaluator = _make_evaluator()
        results = [_make_result("d1", "content")]
        meta = _make_metadata()
        _, report = await evaluator.evaluate(
            "q",
            results,
            meta,
            strategy_names=["retrieval.context_quality"],
        )
        names = {r.strategy_name for r in report.results}
        assert "retrieval.context_quality" in names
        assert "retrieval.groundedness" not in names

    @pytest.mark.asyncio()
    async def test_evaluation_report_has_retrieval_subject(self) -> None:
        from backend.evaluation.models import EvaluationSubject

        evaluator = _make_evaluator()
        meta = _make_metadata()
        _, report = await evaluator.evaluate("q", [], meta)
        assert report.subject == EvaluationSubject.RETRIEVAL


# ---------------------------------------------------------------------------
# register_retrieval_strategies
# ---------------------------------------------------------------------------


class TestRegisterRetrievalStrategies:
    def test_registers_both_strategies(self) -> None:
        registry = EvaluationRegistry()
        register_retrieval_strategies(registry)
        assert "retrieval.context_quality" in registry.names()
        assert "retrieval.groundedness" in registry.names()

    def test_override_false_raises_on_duplicate(self) -> None:
        registry = EvaluationRegistry()
        register_retrieval_strategies(registry)
        with pytest.raises(ValueError, match="already registered"):
            register_retrieval_strategies(registry, override=False)

    def test_override_true_replaces_registration(self) -> None:
        registry = EvaluationRegistry()
        register_retrieval_strategies(registry)
        custom_cs = ContextQualityScorer(relevance_threshold=0.9)
        register_retrieval_strategies(registry, context_scorer=custom_cs, override=True)
        strategy = registry.create("retrieval.context_quality")
        assert isinstance(strategy, ContextQualityScorer)
        assert strategy.relevance_threshold == pytest.approx(0.9)

    def test_custom_judge_wired_to_groundedness_scorer(self) -> None:
        registry = EvaluationRegistry()
        judge = FakeJudgeProvider()
        register_retrieval_strategies(registry, judge=judge)
        gs = registry.create("retrieval.groundedness")
        assert isinstance(gs, GroundednessScorer)
        assert gs.judge is judge


# ---------------------------------------------------------------------------
# BenchmarkRunner
# ---------------------------------------------------------------------------


class TestBenchmarkRunner:
    def _make_runner(self) -> BenchmarkRunner:
        evaluator = _make_evaluator()
        return BenchmarkRunner(evaluator=evaluator, concurrency=2)

    async def _retrieval_fn(
        self, query: str
    ) -> tuple[list[RetrievalResult], RetrievalMetadata]:
        results = [_make_result("doc-1", f"content about {query}", score=0.8)]
        meta = _make_metadata(latency_ms=80.0)
        return results, meta

    @pytest.mark.asyncio()
    async def test_run_produces_report(self) -> None:
        runner = self._make_runner()
        cases = [
            BenchmarkCase(
                case_id="c1",
                query="kubernetes",
                relevant_document_ids=frozenset({"doc-1"}),
            ),
            BenchmarkCase(
                case_id="c2",
                query="prometheus",
                relevant_document_ids=frozenset({"doc-1"}),
            ),
        ]
        report = await runner.run(cases, self._retrieval_fn)
        assert report.cases_run == 2
        assert report.cases_succeeded == 2
        assert report.cases_errored == 0
        assert len(report.results) == 2

    @pytest.mark.asyncio()
    async def test_mean_precision_computed(self) -> None:
        runner = self._make_runner()
        cases = [
            BenchmarkCase(
                case_id="c1",
                query="q",
                relevant_document_ids=frozenset({"doc-1"}),
            )
        ]
        report = await runner.run(cases, self._retrieval_fn)
        assert report.mean_precision == pytest.approx(1.0)

    @pytest.mark.asyncio()
    async def test_failing_retrieval_fn_recorded_as_error(self) -> None:
        runner = self._make_runner()

        async def bad_fn(query: str) -> tuple[list[RetrievalResult], RetrievalMetadata]:
            raise RuntimeError("retrieval unavailable")

        cases = [
            BenchmarkCase(
                case_id="c1",
                query="q",
                relevant_document_ids=frozenset(),
            )
        ]
        report = await runner.run(cases, bad_fn)
        assert report.cases_errored == 1
        assert report.cases_succeeded == 0
        assert report.results[0].error is not None

    @pytest.mark.asyncio()
    async def test_partial_failure_does_not_abort_batch(self) -> None:
        runner = self._make_runner()
        call_count = 0

        async def mixed_fn(
            query: str,
        ) -> tuple[list[RetrievalResult], RetrievalMetadata]:
            nonlocal call_count
            call_count += 1
            if query == "fail":
                raise RuntimeError("boom")
            return [_make_result("d1", "content")], _make_metadata()

        cases = [
            BenchmarkCase("c1", "ok", frozenset()),
            BenchmarkCase("c2", "fail", frozenset()),
            BenchmarkCase("c3", "ok", frozenset()),
        ]
        report = await runner.run(cases, mixed_fn)
        assert report.cases_run == 3
        assert report.cases_succeeded == 2
        assert report.cases_errored == 1
        assert call_count == 3

    @pytest.mark.asyncio()
    async def test_cache_hit_ratio_computed(self) -> None:
        runner = self._make_runner()

        async def cached_fn(
            query: str,
        ) -> tuple[list[RetrievalResult], RetrievalMetadata]:
            return [], _make_metadata(cached=True)

        cases = [BenchmarkCase(f"c{i}", "q", frozenset()) for i in range(4)]
        report = await runner.run(cases, cached_fn)
        assert report.cache_hit_ratio == pytest.approx(1.0)

    @pytest.mark.asyncio()
    async def test_report_duration_is_non_negative(self) -> None:
        runner = self._make_runner()
        report = await runner.run([], self._retrieval_fn)
        assert report.duration_seconds() >= 0.0

    @pytest.mark.asyncio()
    async def test_empty_cases_returns_empty_report(self) -> None:
        runner = self._make_runner()
        report = await runner.run([], self._retrieval_fn)
        assert report.cases_run == 0
        assert report.cases_succeeded == 0
        assert len(report.results) == 0
        assert report.mean_precision is None
