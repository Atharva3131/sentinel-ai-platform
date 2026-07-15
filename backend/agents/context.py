"""Agent execution context and extension hooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from backend.runtime import RuntimeFactory, RuntimeMiddlewarePipeline
from backend.runtime.contracts import RuntimeCancellationToken


@runtime_checkable
class AgentEventEmitter(Protocol):
    """Contract for agent event emission hooks."""

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: AgentContext,
    ) -> None:
        """Emit an agent-scoped event with the active context."""
        ...


@runtime_checkable
class AgentMetricsRecorder(Protocol):
    """Contract for recording agent-level metrics."""

    async def record_agent_execution(
        self,
        *,
        agent_name: str,
        agent_version: str | None,
        workflow_id: str | None,
        execution_id: str | None,
        status: str,
        duration_ms: float,
        attempt: int,
        correlation_id: str | None,
    ) -> None:
        """Record one agent execution observation."""
        ...


@runtime_checkable
class AgentEvaluationHook(Protocol):
    """Contract for evaluation hooks around agent execution."""

    async def before(self, context: AgentContext) -> None:
        """Hook called before agent execution begins."""
        ...

    async def after(self, context: AgentContext, *, status: str) -> None:
        """Hook called after agent execution completes."""
        ...


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Execution-scoped context propagated through agent execution."""

    workflow_id: str | None = None
    execution_id: str | None = None
    correlation_id: str | None = None
    tenant_id: str | None = None
    actor_id: str | None = None
    deadline: datetime | None = None
    timeout_seconds: float | None = None
    attempt: int = 1
    max_attempts: int = 1
    retry_delay_seconds: float | None = None
    cancellation_token: RuntimeCancellationToken | None = None
    runtime_factory: RuntimeFactory | None = None
    runtime_middleware_pipeline: RuntimeMiddlewarePipeline | None = None
    event_emitter: AgentEventEmitter | None = None
    metrics_recorder: AgentMetricsRecorder | None = None
    evaluation_hook: AgentEvaluationHook | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_attempt(self, attempt: int) -> AgentContext:
        """Return a copy using a different attempt number."""
        return replace(self, attempt=attempt)

    def is_cancelled(self) -> bool:
        """Return `True` when cancellation has been requested."""
        if self.cancellation_token is None:
            return False
        return self.cancellation_token.is_set()

    def has_timed_out(self, *, now: datetime | None = None) -> bool:
        """Return `True` when the deadline has elapsed."""
        if self.deadline is None:
            return False
        current = now or datetime.now(UTC)
        return current >= self.deadline

    def remaining_timeout_seconds(self, *, now: datetime | None = None) -> float | None:
        """Return remaining deadline budget, if configured."""
        if self.deadline is None:
            return self.timeout_seconds
        current = now or datetime.now(UTC)
        remaining = (self.deadline - current).total_seconds()
        return max(remaining, 0.0)
