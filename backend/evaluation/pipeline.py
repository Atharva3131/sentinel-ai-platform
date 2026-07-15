"""EvaluationPipeline — ordered, weighted execution of evaluation strategies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from backend.evaluation.context import EvaluationContext
from backend.evaluation.exceptions import EvaluationStrategyError, EvaluationTimeoutError
from backend.evaluation.models import (
    EvaluationReport,
    EvaluationResult,
    EvaluationStatus,
    EvaluationVerdict,
)
from backend.evaluation.strategy import EvaluationStrategy


@dataclass(slots=True)
class EvaluationPipeline:
    """Runs an ordered sequence of EvaluationStrategy instances against a shared context.

    Execution is sequential. Strategies are independent: one strategy's result
    does not influence another's inputs.

    Aggregation rules:
        verdict:
            - Any FAILED result → FAILED.
            - All active results PASSED → PASSED.
            - Otherwise → INDETERMINATE.
            Active = status not in {SKIPPED, ERROR}.
        overall_score:
            Weighted average of active result scores using strategy.weight.
        overall_confidence:
            Minimum confidence across active results (weakest-link model).

    Timeout:
        When ``timeout_seconds`` is set, each strategy is individually wrapped in
        ``asyncio.wait_for``. Exceeding the timeout produces an ERROR result and
        records an EvaluationTimeoutError; the pipeline continues unless
        ``fail_fast`` is also set.

    Cancellation:
        Checked via EvaluationContext.is_cancelled() before each strategy.
        Remaining strategies are recorded as SKIPPED when cancelled.
    """

    strategies: tuple[EvaluationStrategy, ...]
    fail_fast: bool = False
    timeout_seconds: float | None = None

    async def run(self, context: EvaluationContext) -> EvaluationReport:
        """Execute all strategies and return an aggregated EvaluationReport."""
        started_at = datetime.now(UTC)
        results: list[tuple[float, EvaluationResult]] = []

        for strategy in self.strategies:
            if context.is_cancelled():
                remaining = self.strategies[len(results):]
                for s in remaining:
                    results.append((s.weight, self._skipped_result(s)))
                break

            result = await self._run_strategy(strategy, context)
            results.append((strategy.weight, result))

            if self.fail_fast and result.status == EvaluationStatus.FAILED:
                remaining = self.strategies[len(results):]
                for s in remaining:
                    results.append((s.weight, self._skipped_result(s)))
                break

        ended_at = datetime.now(UTC)
        return self._build_report(context, results, started_at, ended_at)

    async def _run_strategy(
        self,
        strategy: EvaluationStrategy,
        context: EvaluationContext,
    ) -> EvaluationResult:
        timeout = self.timeout_seconds
        if context.deadline is not None:
            remaining = context.remaining_seconds()
            if remaining is not None:
                timeout = min(timeout, remaining) if timeout is not None else remaining

        try:
            if timeout is not None:
                return await asyncio.wait_for(
                    strategy.evaluate(context), timeout=timeout
                )
            return await strategy.evaluate(context)
        except TimeoutError:
            exc = EvaluationTimeoutError(
                f"Strategy '{strategy.name}' timed out after {timeout}s",
                strategy_name=strategy.name,
                timeout_seconds=timeout,
                evaluation_id=context.evaluation_id,
            )
            return self._error_result(strategy, exc)
        except Exception as exc:
            wrapped = EvaluationStrategyError(
                f"Strategy '{strategy.name}' raised: {exc}",
                strategy_name=strategy.name,
                strategy_version=strategy.version,
                evaluation_id=context.evaluation_id,
                metadata={"exception_type": type(exc).__name__},
            )
            return self._error_result(strategy, wrapped)

    def _skipped_result(self, strategy: EvaluationStrategy) -> EvaluationResult:
        now = datetime.now(UTC)
        return EvaluationResult(
            strategy_name=strategy.name,
            strategy_version=strategy.version,
            status=EvaluationStatus.SKIPPED,
            metrics=(),
            score=0.0,
            confidence=1.0,
            evidence_refs=(),
            started_at=now,
            ended_at=now,
            reasoning="Skipped due to fail_fast or cancellation",
        )

    def _error_result(
        self,
        strategy: EvaluationStrategy,
        error: EvaluationStrategyError | EvaluationTimeoutError,
    ) -> EvaluationResult:
        now = datetime.now(UTC)
        return EvaluationResult(
            strategy_name=strategy.name,
            strategy_version=strategy.version,
            status=EvaluationStatus.ERROR,
            metrics=(),
            score=0.0,
            confidence=1.0,
            evidence_refs=(),
            started_at=now,
            ended_at=now,
            reasoning=str(error),
            error=error,
        )

    def _build_report(
        self,
        context: EvaluationContext,
        weighted_results: list[tuple[float, EvaluationResult]],
        started_at: datetime,
        ended_at: datetime,
    ) -> EvaluationReport:
        only_results = tuple(r for _, r in weighted_results)
        active = [
            (w, r)
            for w, r in weighted_results
            if r.status not in (EvaluationStatus.SKIPPED, EvaluationStatus.ERROR)
        ]

        overall_score = self._weighted_score(active)
        overall_confidence = (
            min(r.confidence for _, r in active) if active else 1.0
        )
        verdict = self._compute_verdict(active)

        return EvaluationReport(
            evaluation_id=context.evaluation_id,
            subject=context.subject,
            subject_id=context.subject_id,
            subject_version=context.subject_version,
            results=only_results,
            verdict=verdict,
            overall_score=overall_score,
            overall_confidence=overall_confidence,
            started_at=started_at,
            ended_at=ended_at,
            workflow_id=context.workflow_id,
            execution_id=context.execution_id,
            correlation_id=context.correlation_id,
            metadata=dict(context.metadata),
        )

    @staticmethod
    def _weighted_score(
        active: list[tuple[float, EvaluationResult]],
    ) -> float:
        if not active:
            return 0.0
        total_weight = sum(w for w, _ in active)
        if total_weight == 0.0:
            return 0.0
        return sum(w * r.score for w, r in active) / total_weight

    @staticmethod
    def _compute_verdict(
        active: list[tuple[float, EvaluationResult]],
    ) -> EvaluationVerdict:
        if not active:
            return EvaluationVerdict.INDETERMINATE
        statuses = {r.status for _, r in active}
        if EvaluationStatus.FAILED in statuses:
            return EvaluationVerdict.FAILED
        if statuses == {EvaluationStatus.PASSED}:
            return EvaluationVerdict.PASSED
        return EvaluationVerdict.INDETERMINATE
