"""Evaluation framework public API."""

from __future__ import annotations

from backend.evaluation.context import EvaluationCancellationToken, EvaluationContext
from backend.evaluation.engine import EvaluationEngine
from backend.evaluation.exceptions import (
    EvaluationContextError,
    EvaluationError,
    EvaluationRegistryError,
    EvaluationStrategyError,
    EvaluationTimeoutError,
)
from backend.evaluation.models import (
    EvaluationMetric,
    EvaluationMetricKind,
    EvaluationReport,
    EvaluationResult,
    EvaluationStatus,
    EvaluationSubject,
    EvaluationVerdict,
)
from backend.evaluation.pipeline import EvaluationPipeline
from backend.evaluation.registry import EvaluationRegistry, EvaluationStrategyProvider
from backend.evaluation.strategy import EvaluationStrategy

__all__ = [
    "EvaluationCancellationToken",
    "EvaluationContext",
    "EvaluationContextError",
    "EvaluationEngine",
    "EvaluationError",
    "EvaluationMetric",
    "EvaluationMetricKind",
    "EvaluationPipeline",
    "EvaluationRegistry",
    "EvaluationRegistryError",
    "EvaluationReport",
    "EvaluationResult",
    "EvaluationStatus",
    "EvaluationStrategy",
    "EvaluationStrategyError",
    "EvaluationStrategyProvider",
    "EvaluationSubject",
    "EvaluationTimeoutError",
    "EvaluationVerdict",
]
