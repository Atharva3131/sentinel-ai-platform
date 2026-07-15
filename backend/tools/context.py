"""Tool execution context and extension hooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from backend.runtime import RuntimeFactory, RuntimeMiddlewarePipeline
from backend.runtime.contracts import RuntimeCancellationToken
from backend.tools.models import ToolPermission


@runtime_checkable
class ToolEventEmitter(Protocol):
    """Contract for tool event emission hooks."""

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: ToolContext,
    ) -> None:
        """Emit a tool-scoped event with the active context."""
        ...


@runtime_checkable
class ToolMetricsRecorder(Protocol):
    """Contract for tool-level metrics recording."""

    async def record_tool_execution(
        self,
        *,
        tool_name: str,
        tool_version: str | None,
        workflow_id: str | None,
        execution_id: str | None,
        status: str,
        duration_ms: float,
        attempt: int,
        correlation_id: str | None,
    ) -> None:
        """Record one tool execution observation."""
        ...


@runtime_checkable
class ToolEvaluationHook(Protocol):
    """Contract for evaluation hooks around tool execution."""

    async def before(self, context: ToolContext) -> None:
        """Hook called before tool execution begins."""
        ...

    async def after(self, context: ToolContext, *, status: str) -> None:
        """Hook called after tool execution completes."""
        ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Execution-scoped context propagated through tool execution."""

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
    permissions: frozenset[ToolPermission] = field(default_factory=frozenset)
    runtime_factory: RuntimeFactory | None = None
    runtime_middleware_pipeline: RuntimeMiddlewarePipeline | None = None
    event_emitter: ToolEventEmitter | None = None
    metrics_recorder: ToolMetricsRecorder | None = None
    evaluation_hook: ToolEvaluationHook | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_attempt(self, attempt: int) -> ToolContext:
        """Return a copy using a different attempt number."""
        return replace(self, attempt=attempt)

    def with_permissions(self, *permissions: ToolPermission) -> ToolContext:
        """Return a copy with added permissions."""
        merged = set(self.permissions)
        merged.update(permissions)
        return replace(self, permissions=frozenset(merged))

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
