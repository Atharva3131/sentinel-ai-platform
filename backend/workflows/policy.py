"""Execution policy for workflow retries, deadlines, and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Deterministic policy applied to one workflow execution."""

    max_attempts: int = 3
    timeout_seconds: float | None = None
    base_retry_delay_seconds: float = 0.0
    retry_backoff_multiplier: float = 2.0
    checkpoint_required: bool = True
    checkpoint_interval: int = 1
    allow_cancellation: bool = True
    allow_recovery: bool = True
    retryable_statuses: tuple[str, ...] = ("failed", "timed_out")
    required_metadata_keys: tuple[str, ...] = ()

    def next_retry_delay_seconds(self, attempt: int) -> float:
        """Return the retry delay for the provided attempt number."""
        if attempt < 1:
            raise ValueError("attempt must be greater than or equal to 1")
        if self.base_retry_delay_seconds <= 0:
            return 0.0
        exponent = max(attempt - 1, 0)
        return self.base_retry_delay_seconds * (self.retry_backoff_multiplier**exponent)

    def can_retry(self, status: str, attempt: int) -> bool:
        """Return `True` when the status is eligible for another attempt."""
        return status in self.retryable_statuses and attempt < self.max_attempts

    def requires_checkpoint(self, attempt: int) -> bool:
        """Return `True` when the policy expects a checkpoint on this attempt."""
        if not self.checkpoint_required:
            return False
        if self.checkpoint_interval <= 0:
            return True
        return attempt % self.checkpoint_interval == 0

    def effective_timeout_seconds(self, timeout_seconds: float | None) -> float | None:
        """Return the configured timeout constrained by an execution-specific timeout."""
        if timeout_seconds is None:
            return self.timeout_seconds
        if self.timeout_seconds is None:
            return timeout_seconds
        return min(self.timeout_seconds, timeout_seconds)

