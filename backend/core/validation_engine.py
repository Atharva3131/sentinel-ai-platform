"""ValidationEngine — runs post-remediation validation and verification.

Validation checks that the remediation did not break anything.
Verification compares the before/after state to confirm improvement.

Both are fully configurable: thresholds live in ValidationStrategyConfig
and VerificationPlan — not hard-coded in the engine.

The ``ValidationStrategyRunner`` protocol is the extension point for
custom check implementations.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from backend.models.incident import Incident
from backend.models.validation import (
    ComparisonResult,
    ComparisonVerdict,
    ValidationPlan,
    ValidationResult,
    ValidationStatus,
    ValidationStrategyConfig,
    ValidationStrategyKind,
    VerificationPlan,
    VerificationResult,
)

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


@runtime_checkable
class ValidationStrategyRunner(Protocol):
    """Port for running a single validation strategy against a service."""

    @property
    def kind(self) -> ValidationStrategyKind:
        """Return the strategy kind this runner handles."""
        ...

    async def run(
        self,
        config: ValidationStrategyConfig,
        *,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """Execute the validation check defined by *config*."""
        ...


@runtime_checkable
class MetricsSnapshotPort(Protocol):
    """Port for capturing a metric snapshot used in verification."""

    async def snapshot(
        self,
        service: str,
        metric_names: tuple[str, ...],
        *,
        window_seconds: float = 60.0,
    ) -> dict[str, float]:
        """Return current metric values for *service*."""
        ...


# ---------------------------------------------------------------------------
# Fake implementations for tests
# ---------------------------------------------------------------------------


class FakeHealthCheckRunner:
    """Fake health-check runner — returns configurable pass/fail."""

    kind = ValidationStrategyKind.HEALTH_CHECK

    def __init__(self, *, succeed: bool = True) -> None:
        self._succeed = succeed
        self.calls: list[ValidationStrategyConfig] = []

    async def run(
        self,
        config: ValidationStrategyConfig,
        *,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        self.calls.append(config)
        now = datetime.now(UTC)
        status = ValidationStatus.PASSED if self._succeed else ValidationStatus.FAILED
        return ValidationResult(
            result_id=str(uuid.uuid4()),
            plan_id="",
            strategy_kind=self.kind,
            target_service=config.target_service,
            status=status,
            score=1.0 if self._succeed else 0.0,
            message="Health check passed." if self._succeed else "Health check failed.",
            executed_at=now,
            duration_ms=10.0,
        )


class FakeMetricComparisonRunner:
    """Fake metric comparison runner."""

    kind = ValidationStrategyKind.METRIC_COMPARISON

    def __init__(self, *, succeed: bool = True) -> None:
        self._succeed = succeed
        self.calls: list[ValidationStrategyConfig] = []

    async def run(
        self,
        config: ValidationStrategyConfig,
        *,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        self.calls.append(config)
        now = datetime.now(UTC)
        status = ValidationStatus.PASSED if self._succeed else ValidationStatus.FAILED
        return ValidationResult(
            result_id=str(uuid.uuid4()),
            plan_id="",
            strategy_kind=self.kind,
            target_service=config.target_service,
            status=status,
            score=0.9 if self._succeed else 0.1,
            message="Metric within threshold." if self._succeed else "Metric exceeded threshold.",
            executed_at=now,
            duration_ms=15.0,
        )


class FakeMetricsSnapshot:
    """Fake metric snapshot — returns pre-configured values."""

    def __init__(self, values: dict[str, dict[str, float]] | None = None) -> None:
        # values[service][metric_name] = float
        self._values = values or {}

    async def snapshot(
        self,
        service: str,
        metric_names: tuple[str, ...],
        *,
        window_seconds: float = 60.0,
    ) -> dict[str, float]:
        svc_vals = self._values.get(service, {})
        return {m: svc_vals.get(m, 0.0) for m in metric_names}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


@dataclass
class ValidationEngine:
    """Runs a ValidationPlan and produces per-strategy ValidationResults.

    Strategies are dispatched to registered runners by kind.  Unknown strategy
    kinds are skipped with ``ValidationStatus.SKIPPED``.  Failures in one
    strategy do not abort others.
    """

    runners: dict[ValidationStrategyKind, ValidationStrategyRunner]
    max_concurrency: int = 4

    def register_runner(
        self, kind: ValidationStrategyKind, runner: ValidationStrategyRunner
    ) -> None:
        self.runners[kind] = runner

    async def validate(
        self,
        plan: ValidationPlan,
        incident: Incident,
        *,
        context: dict[str, Any] | None = None,
    ) -> tuple[ValidationResult, ...]:
        """Run all strategies in *plan* and return results.

        Results are returned in the same order as plan.strategies.
        """
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def _run_one(cfg: ValidationStrategyConfig) -> ValidationResult:
            async with semaphore:
                runner = self.runners.get(cfg.strategy_kind)
                if runner is None:
                    return ValidationResult(
                        result_id=str(uuid.uuid4()),
                        plan_id=plan.plan_id,
                        strategy_kind=cfg.strategy_kind,
                        target_service=cfg.target_service,
                        status=ValidationStatus.SKIPPED,
                        score=0.0,
                        message=f"No runner registered for {cfg.strategy_kind}",
                        executed_at=datetime.now(UTC),
                        duration_ms=0.0,
                    )
                try:
                    result = await asyncio.wait_for(
                        runner.run(cfg, context=context),
                        timeout=cfg.timeout_seconds,
                    )
                    # Stamp the plan_id onto the result
                    from dataclasses import replace
                    return replace(result, plan_id=plan.plan_id)
                except TimeoutError:
                    return ValidationResult(
                        result_id=str(uuid.uuid4()),
                        plan_id=plan.plan_id,
                        strategy_kind=cfg.strategy_kind,
                        target_service=cfg.target_service,
                        status=ValidationStatus.ERROR,
                        score=0.0,
                        message=f"Validation timed out after {cfg.timeout_seconds}s",
                        executed_at=datetime.now(UTC),
                        duration_ms=cfg.timeout_seconds * 1000,
                    )
                except Exception as exc:
                    return ValidationResult(
                        result_id=str(uuid.uuid4()),
                        plan_id=plan.plan_id,
                        strategy_kind=cfg.strategy_kind,
                        target_service=cfg.target_service,
                        status=ValidationStatus.ERROR,
                        score=0.0,
                        message=f"Validation error: {exc}",
                        executed_at=datetime.now(UTC),
                        duration_ms=0.0,
                    )

        results = await asyncio.gather(*[_run_one(cfg) for cfg in plan.strategies])
        return tuple(results)

    def passed(self, results: tuple[ValidationResult, ...]) -> bool:
        """Return True when all non-skipped results passed."""
        active = [r for r in results if r.status != ValidationStatus.SKIPPED]
        if not active:
            return False
        return all(r.status == ValidationStatus.PASSED for r in active)


@dataclass
class VerificationEngine:
    """Compares before/after metric state to confirm remediation was effective.

    Thresholds are taken from ``VerificationPlan.thresholds`` — nothing is
    hard-coded in this class.  The ``improvement_direction`` convention is:
    lower is better for error rates and latency; higher is better for
    availability and throughput.  The plan specifies this via threshold signs.
    """

    metrics_snapshot: MetricsSnapshotPort

    async def verify(
        self,
        plan: VerificationPlan,
        incident: Incident,
        *,
        context: dict[str, Any] | None = None,
    ) -> VerificationResult:
        """Capture after-state, compare to before-state, return VerificationResult."""
        # Collect after snapshots for all affected services
        affected_services = list(incident.affected_services) or ["unknown"]
        snapshot_after: dict[str, Any] = {}
        for svc in affected_services:
            svc_snapshot = await self.metrics_snapshot.snapshot(
                svc,
                plan.metrics_to_compare,
                window_seconds=plan.comparison_window_seconds,
            )
            snapshot_after[svc] = svc_snapshot

        comparisons: list[ComparisonResult] = []
        for metric in plan.metrics_to_compare:
            threshold = plan.thresholds.get(metric)
            for svc in affected_services:
                before_val = float(plan.snapshot_before.get(svc, {}).get(metric, 0.0))
                after_val = float(snapshot_after.get(svc, {}).get(metric, 0.0))
                pct = _percent_change(before_val, after_val)
                verdict = _compare(before_val, after_val, threshold)
                comparisons.append(ComparisonResult(
                    metric_name=metric,
                    service=svc,
                    before_value=before_val,
                    after_value=after_val,
                    verdict=verdict,
                    percent_change=pct,
                    threshold_used=threshold,
                ))

        if not comparisons:
            overall_verdict = ComparisonVerdict.UNKNOWN
            passed = False
            confidence = 0.0
        else:
            improved = sum(1 for c in comparisons if c.verdict == ComparisonVerdict.IMPROVED)
            degraded = sum(1 for c in comparisons if c.verdict == ComparisonVerdict.DEGRADED)
            total = len(comparisons)
            confidence = improved / total if total > 0 else 0.0
            if degraded > 0:
                overall_verdict = ComparisonVerdict.DEGRADED
            elif improved > 0:
                overall_verdict = ComparisonVerdict.IMPROVED
            else:
                overall_verdict = ComparisonVerdict.UNCHANGED
            passed = degraded == 0 and improved >= (total // 2 + 1 if total > 0 else 1)

        summary_parts = [f"{c.metric_name}@{c.service}: {c.verdict}" for c in comparisons[:5]]
        summary = "; ".join(summary_parts) if summary_parts else "No comparisons available."

        return VerificationResult(
            result_id=str(uuid.uuid4()),
            plan_id=plan.plan_id,
            incident_id=incident.incident_id,
            comparisons=tuple(comparisons),
            overall_verdict=overall_verdict,
            passed=passed,
            confidence=round(confidence, 4),
            summary=summary,
            produced_at=datetime.now(UTC),
            snapshot_after=snapshot_after,
        )


def _percent_change(before: float, after: float) -> float | None:
    if before == 0.0:
        return None
    return round((after - before) / abs(before) * 100.0, 2)


def _compare(
    before: float,
    after: float,
    threshold: float | None,
) -> ComparisonVerdict:
    """Determine verdict for a single metric comparison.

    When a threshold is provided: after < threshold → IMPROVED (for error-rate
    style metrics where lower is better).  When no threshold, compare
    directionally.
    """
    if threshold is not None:
        if after < threshold and before >= threshold:
            return ComparisonVerdict.IMPROVED
        if after >= threshold and before < threshold:
            return ComparisonVerdict.DEGRADED
        return ComparisonVerdict.UNCHANGED
    # No threshold: interpret directionally (lower = better by convention)
    if after < before:
        return ComparisonVerdict.IMPROVED
    if after > before:
        return ComparisonVerdict.DEGRADED
    return ComparisonVerdict.UNCHANGED
