"""Runtime metadata and execution context models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.runtime.contracts import RuntimeCancellationToken, RuntimeEventEmitter


@dataclass(frozen=True, slots=True)
class WorkflowMetadata:
    """Immutable metadata describing a workflow definition."""

    workflow_id: str
    name: str
    version: str | None = None
    description: str | None = None
    capabilities: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    """Immutable metadata for one workflow execution attempt."""

    execution_id: str
    workflow_id: str
    runtime_name: str
    attempt: int = 1
    status: str = "pending"
    checkpoint_id: str | None = None
    parent_checkpoint_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def duration_seconds(self, *, now: datetime | None = None) -> float | None:
        """Return the execution duration when both timestamps are available."""
        if self.started_at is None:
            return None
        current = now or datetime.now(UTC)
        finished_at = self.ended_at or current
        return max((finished_at - self.started_at).total_seconds(), 0.0)


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Execution-scoped context supplied to runtime implementations."""

    workflow: WorkflowMetadata
    execution: WorkflowExecution
    correlation_id: str | None = None
    tenant_id: str | None = None
    actor_id: str | None = None
    deadline: datetime | None = None
    timeout_seconds: float | None = None
    attempt: int = 1
    max_attempts: int = 1
    retry_delay_seconds: float | None = None
    cancellation_token: RuntimeCancellationToken | None = None
    event_emitter: RuntimeEventEmitter | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_cancelled(self) -> bool:
        """Return `True` when cooperative cancellation has been requested."""
        if self.cancellation_token is None:
            return False
        return self.cancellation_token.is_set()

    def has_timed_out(self, *, now: datetime | None = None) -> bool:
        """Return `True` when the runtime deadline has elapsed."""
        if self.deadline is None:
            return False
        current = now or datetime.now(UTC)
        return current >= self.deadline

    def remaining_timeout_seconds(self, *, now: datetime | None = None) -> float | None:
        """Return the remaining deadline budget in seconds, if one is configured."""
        if self.deadline is None:
            return self.timeout_seconds
        current = now or datetime.now(UTC)
        remaining = (self.deadline - current).total_seconds()
        return max(remaining, 0.0)

    def event_metadata(self) -> Mapping[str, Any]:
        """Return a read-only view of context metadata for event emission."""
        return self.metadata
