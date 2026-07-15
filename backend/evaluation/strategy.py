"""EvaluationStrategy Protocol — contract for all evaluation strategy implementations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.evaluation.context import EvaluationContext
from backend.evaluation.models import EvaluationResult


@runtime_checkable
class EvaluationStrategy(Protocol):
    """Stateless, async contract for an evaluation strategy.

    A strategy receives an EvaluationContext containing structured evidence and
    returns an EvaluationResult. Strategies must be safe to call concurrently
    and must not mutate the context.

    ``weight`` is used by EvaluationPipeline to compute a weighted overall score.
    A value of 1.0 is neutral; higher values amplify the strategy's contribution.

    Built-in strategy categories (no implementations yet — infrastructure only):
        - Latency: measures p50/p95/p99 latency against thresholds.
        - Cost: measures per-call and cumulative cost in USD.
        - Tool success: measures tool call success rate.
        - Workflow success: measures workflow completion and error rates.
        - Agent success: measures agent execution success rate.
        - Memory usage: measures peak and average memory consumption.
        - Token usage: measures input/output token consumption.
        - Groundedness: LLM-as-a-judge; measures evidence grounding score.
        - Hallucination: LLM-as-a-judge; measures hallucination likelihood.
        - Custom: caller-defined metric collection.
    """

    @property
    def name(self) -> str:
        """Return the stable strategy name used for registry lookup."""
        ...

    @property
    def version(self) -> str | None:
        """Return the strategy version, when declared."""
        ...

    @property
    def weight(self) -> float:
        """Return the strategy's contribution weight for score aggregation (default 1.0)."""
        ...

    async def evaluate(self, context: EvaluationContext) -> EvaluationResult:
        """Evaluate the context and return an immutable EvaluationResult."""
        ...
