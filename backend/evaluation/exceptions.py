"""Evaluation system exception hierarchy."""

from __future__ import annotations

from typing import Any

from backend.exceptions import SentinelError


class EvaluationError(SentinelError):
    """Base exception for evaluation system failures."""


class EvaluationStrategyError(EvaluationError):
    """Raised when an EvaluationStrategy raises an unexpected error during execution."""

    def __init__(
        self,
        message: str,
        *,
        strategy_name: str | None = None,
        strategy_version: str | None = None,
        evaluation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.strategy_name = strategy_name
        self.strategy_version = strategy_version
        self.evaluation_id = evaluation_id
        self.metadata: dict[str, Any] = metadata or {}


class EvaluationTimeoutError(EvaluationError):
    """Raised when an EvaluationStrategy exceeds its configured timeout."""

    def __init__(
        self,
        message: str,
        *,
        strategy_name: str | None = None,
        timeout_seconds: float | None = None,
        evaluation_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.strategy_name = strategy_name
        self.timeout_seconds = timeout_seconds
        self.evaluation_id = evaluation_id


class EvaluationRegistryError(EvaluationError):
    """Raised when a requested strategy is not found in the registry."""


class EvaluationContextError(EvaluationError):
    """Raised when the evaluation context contains invalid or insufficient evidence."""
