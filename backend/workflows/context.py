"""Workflow execution context and event emission contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from backend.runtime import RuntimeContext, WorkflowExecution, WorkflowMetadata
from backend.runtime.contracts import RuntimeCancellationToken
from backend.workflows.definition import WorkflowDefinition
from backend.workflows.state import WorkflowState


@runtime_checkable
class WorkflowEventEmitter(Protocol):
    """Contract for workflow lifecycle event emission."""

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: WorkflowContext,
    ) -> None:
        """Emit a workflow event with the active execution context."""
        ...


@dataclass(slots=True)
class _RuntimeEventBridge:
    """Adapt runtime events back into the workflow event emitter."""

    emitter: WorkflowEventEmitter | None
    workflow_context: WorkflowContext

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: RuntimeContext,
    ) -> None:
        """Forward runtime events to the workflow-level emitter."""
        if self.emitter is None:
            return
        await self.emitter.emit(event_name, payload, self.workflow_context)


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """Execution-scoped context for workflow orchestration."""

    workflow_id: str
    workflow_version: str | None
    execution_id: str
    runtime_name: str | None = None
    correlation_id: str | None = None
    tenant_id: str | None = None
    actor_id: str | None = None
    deadline: datetime | None = None
    timeout_seconds: float | None = None
    attempt: int = 1
    max_attempts: int = 1
    retry_delay_seconds: float | None = None
    checkpoint_id: str | None = None
    cancellation_token: RuntimeCancellationToken | None = None
    event_emitter: WorkflowEventEmitter | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def with_attempt(self, attempt: int) -> WorkflowContext:
        """Return a copy using a different execution attempt."""
        return replace(self, attempt=attempt)

    def with_checkpoint(self, checkpoint_id: str | None) -> WorkflowContext:
        """Return a copy with an updated checkpoint reference."""
        return replace(self, checkpoint_id=checkpoint_id)

    def to_runtime_context(
        self,
        definition: WorkflowDefinition,
        state: WorkflowState,
        *,
        runtime_name: str,
        timeout_seconds: float | None = None,
    ) -> RuntimeContext:
        """Convert workflow context and state into runtime execution context."""
        workflow_metadata = WorkflowMetadata(
            workflow_id=definition.workflow_id,
            name=definition.name,
            version=definition.version,
            description=definition.description,
            capabilities=definition.capabilities,
            labels=definition.labels,
            metadata=dict(definition.metadata),
        )
        execution = WorkflowExecution(
            execution_id=self.execution_id,
            workflow_id=self.workflow_id,
            runtime_name=runtime_name,
            attempt=self.attempt,
            status=state.status,
            checkpoint_id=state.checkpoint_id or self.checkpoint_id,
            started_at=state.started_at,
            ended_at=state.ended_at,
            metadata=dict(state.metadata),
        )
        effective_metadata = dict(self.metadata)
        effective_metadata.setdefault("workflow_version", self.workflow_version)
        return RuntimeContext(
            workflow=workflow_metadata,
            execution=execution,
            correlation_id=self.correlation_id,
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            deadline=self.deadline,
            timeout_seconds=(
                timeout_seconds if timeout_seconds is not None else self.timeout_seconds
            ),
            attempt=self.attempt,
            max_attempts=self.max_attempts,
            retry_delay_seconds=self.retry_delay_seconds,
            cancellation_token=self.cancellation_token,
            event_emitter=_RuntimeEventBridge(self.event_emitter, self),
            metadata=effective_metadata,
        )
