"""GroundednessScorer — EvaluationStrategy measuring answer-context grounding."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from backend.evaluation.context import EvaluationContext
from backend.evaluation.models import (
    EvaluationMetric,
    EvaluationMetricKind,
    EvaluationResult,
    EvaluationStatus,
)
from backend.retrieval.models import RetrievalResult
from backend.retrieval.retrieval_eval_models import JudgeProvider

_EV_QUERY = "query"
_EV_ANSWER = "answer"
_EV_RETRIEVED_RESULTS = "retrieved_results"

_NON_WORD = re.compile(r"[^\w\s]")


def _word_set(text: str) -> frozenset[str]:
    return frozenset(_NON_WORD.sub("", text.lower()).split())


def _heuristic_groundedness(
    answer: str,
    chunks: list[str],
) -> float:
    """Word-recall heuristic: fraction of answer words that appear in any chunk.

    Returns 0.0 when the answer or all chunks are empty.
    Used when no JudgeProvider is configured.
    """
    answer_words = _word_set(answer)
    if not answer_words or not chunks:
        return 0.0
    context_words: frozenset[str] = frozenset()
    for chunk in chunks:
        context_words |= _word_set(chunk)
    supported = answer_words & context_words
    return len(supported) / len(answer_words)


@dataclass(slots=True)
class GroundednessScorer:
    """Scores how well a candidate answer is grounded in retrieved context.

    Two modes:

    Heuristic (default, ``judge=None``):
        Measures the fraction of answer words that appear in the retrieved
        chunks — a fast, deterministic proxy for grounding.

    LLM-as-a-judge (``judge`` provided):
        Delegates to a JudgeProvider that returns a calibrated 0-1 score.
        Confidence is set to 0.9 to reflect model uncertainty.

    Evidence keys consumed:
        query: str
        answer: str
        retrieved_results: list[RetrievalResult]

    When ``answer`` is absent from evidence the strategy returns SKIPPED.

    Implements EvaluationStrategy (structural, no inheritance).
    """

    judge: JudgeProvider | None = None
    groundedness_threshold: float = 0.6
    weight: float = 1.5

    @property
    def name(self) -> str:
        return "retrieval.groundedness"

    @property
    def version(self) -> str | None:
        return "1.0.0"

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        started_at = datetime.now(UTC)
        evidence = context.evidence
        answer: str | None = evidence.get(_EV_ANSWER)

        if not answer:
            now = datetime.now(UTC)
            return EvaluationResult(
                strategy_name=self.name,
                strategy_version=self.version,
                status=EvaluationStatus.SKIPPED,
                metrics=(),
                score=0.0,
                confidence=1.0,
                evidence_refs=(_EV_ANSWER,),
                started_at=started_at,
                ended_at=now,
                reasoning="No answer provided in evidence; groundedness skipped.",
            )

        query: str = evidence.get(_EV_QUERY, "")
        retrieved: list[RetrievalResult] = evidence.get(_EV_RETRIEVED_RESULTS, [])
        chunks = [r.chunk.content for r in retrieved]

        if self.judge is not None:
            score = await self.judge.judge(query, answer, chunks)
            confidence = 0.9
        else:
            score = _heuristic_groundedness(answer, chunks)
            confidence = 1.0

        score = max(0.0, min(1.0, score))
        passed = score >= self.groundedness_threshold

        metric = EvaluationMetric(
            name="groundedness",
            kind=EvaluationMetricKind.GROUNDEDNESS,
            value=score,
            score=score,
            passed=passed,
            threshold=self.groundedness_threshold,
            weight=1.0,
            metadata={"mode": "llm" if self.judge is not None else "heuristic"},
        )
        status = EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED

        return EvaluationResult(
            strategy_name=self.name,
            strategy_version=self.version,
            status=status,
            metrics=(metric,),
            score=score,
            confidence=confidence,
            evidence_refs=(_EV_QUERY, _EV_ANSWER, _EV_RETRIEVED_RESULTS),
            started_at=started_at,
            ended_at=datetime.now(UTC),
        )
