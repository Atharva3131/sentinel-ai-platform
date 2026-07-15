"""EvaluationEngine — registry-driven, pipeline-executed evaluation coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.evaluation.context import EvaluationContext
from backend.evaluation.models import EvaluationReport
from backend.evaluation.pipeline import EvaluationPipeline
from backend.evaluation.registry import EvaluationRegistry
from backend.evaluation.strategy import EvaluationStrategy


@dataclass(slots=True)
class EvaluationEngine:
    """Coordinates strategy resolution, pipeline construction, and report emission.

    The engine is the primary entry point for callers. It decouples callers from
    the registry's resolution logic and pipeline orchestration details.

    Usage::

        report = await engine.evaluate(
            ["latency", "token_usage", "tool_success"],
            context,
            fail_fast=True,
            timeout_seconds=30.0,
        )

    The engine resolves each strategy name from the registry, constructs a
    transient EvaluationPipeline, executes it, and returns the EvaluationReport.
    No state is retained between calls.
    """

    registry: EvaluationRegistry
    default_fail_fast: bool = False
    default_timeout_seconds: float | None = None

    async def evaluate(
        self,
        strategy_names: list[str],
        context: EvaluationContext,
        *,
        versions: dict[str, str] | None = None,
        fail_fast: bool | None = None,
        timeout_seconds: float | None = None,
        dependencies: dict[str, Any] | None = None,
    ) -> EvaluationReport:
        """Resolve strategies, build a pipeline, and run it against the context.

        Args:
            strategy_names: Ordered list of strategy names to execute.
            context: Evidence envelope for this evaluation run.
            versions: Optional per-strategy version overrides keyed by name.
            fail_fast: Stop after the first FAILED result. Overrides engine default.
            timeout_seconds: Per-strategy timeout. Overrides engine default.
            dependencies: Forwarded to each strategy provider at construction time.

        Returns:
            EvaluationReport with verdict, score, confidence, and per-strategy results.
        """
        strategies = self._resolve_strategies(
            strategy_names,
            versions=versions or {},
            dependencies=dependencies,
        )
        pipeline = EvaluationPipeline(
            strategies=strategies,
            fail_fast=fail_fast if fail_fast is not None else self.default_fail_fast,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else self.default_timeout_seconds
            ),
        )
        return await pipeline.run(context)

    def _resolve_strategies(
        self,
        names: list[str],
        *,
        versions: dict[str, str],
        dependencies: dict[str, Any] | None,
    ) -> tuple[EvaluationStrategy, ...]:
        strategies: list[EvaluationStrategy] = []
        for name in names:
            version = versions.get(name)
            strategy = self.registry.create(name, version, dependencies=dependencies)
            strategies.append(strategy)
        return tuple(strategies)
