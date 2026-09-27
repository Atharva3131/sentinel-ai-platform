"""Validation and verification domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ValidationStrategyKind(StrEnum):
    """Type of validation check to run after remediation."""

    HEALTH_CHECK = "health_check"
    METRIC_COMPARISON = "metric_comparison"
    ERROR_RATE_COMPARISON = "error_rate_comparison"
    LATENCY_COMPARISON = "latency_comparison"
    RESOURCE_UTILIZATION = "resource_utilization"
    INTEGRATION_TEST = "integration_test"
    UNIT_TEST = "unit_test"
    SMOKE_TEST = "smoke_test"
    CUSTOM = "custom"


class ValidationStatus(StrEnum):
    """Result of a single validation check."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"
    INDETERMINATE = "indeterminate"


class ComparisonVerdict(StrEnum):
    """Verdict of comparing before/after state."""

    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ValidationStrategyConfig:
    """Configuration for one validation strategy.

    Thresholds and policies are configurable — not hard-coded.  A deployer
    decides what constitutes success for their environment.
    """

    strategy_kind: ValidationStrategyKind
    target_service: str
    timeout_seconds: float = 30.0
    # Configurable thresholds — no fixed values
    thresholds: dict[str, float] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationPlan:
    """Set of validation strategies to run after remediation.

    ``snapshot_before`` holds a baseline of relevant metrics/state captured
    before remediation began.  It is used in comparisons.
    """

    plan_id: str
    incident_id: str
    remediation_plan_id: str
    strategies: tuple[ValidationStrategyConfig, ...]
    snapshot_before: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of running one validation strategy."""

    result_id: str
    plan_id: str
    strategy_kind: ValidationStrategyKind
    target_service: str
    status: ValidationStatus
    score: float                # 0.0-1.0
    message: str
    executed_at: datetime
    duration_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"ValidationResult score must be 0.0-1.0, got {self.score}")


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    """Before/after comparison for a single metric or signal.

    ``before_value`` and ``after_value`` are raw measurements.
    ``verdict`` is the platform's interpretation of the change.
    ``percent_change`` is (after - before) / before * 100.
    """

    metric_name: str
    service: str
    before_value: float
    after_value: float
    verdict: ComparisonVerdict
    percent_change: float | None
    threshold_used: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    """Plan for comparing pre/post-remediation state.

    ``metrics_to_compare`` lists the metric names to measure before and after.
    ``comparison_window_seconds`` is how long after remediation to wait before
    collecting the after-state.
    Thresholds are kept in ``thresholds`` — not hard-coded in the model.
    """

    plan_id: str
    incident_id: str
    remediation_plan_id: str
    metrics_to_compare: tuple[str, ...]
    comparison_window_seconds: float = 60.0
    thresholds: dict[str, float] = field(default_factory=dict)
    snapshot_before: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of the full pre/post verification comparison.

    ``comparisons`` maps metric_name → ComparisonResult.
    ``overall_verdict`` is the aggregate judgment.
    ``passed`` is True when all required metrics improved or are within policy.
    """

    result_id: str
    plan_id: str
    incident_id: str
    comparisons: tuple[ComparisonResult, ...]
    overall_verdict: ComparisonVerdict
    passed: bool
    confidence: float
    summary: str
    produced_at: datetime
    snapshot_after: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"VerificationResult confidence must be 0.0-1.0, got {self.confidence}"
            )
