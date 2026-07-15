"""Runtime factory and execution-context builders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.runtime.context import RuntimeContext, WorkflowExecution, WorkflowMetadata
from backend.runtime.contracts import (
    RuntimeCancellationToken,
    RuntimeEventEmitter,
    WorkflowRuntime,
)
from backend.runtime.registry import RuntimeRegistry


@dataclass(slots=True)
class RuntimeFactory:
    """Resolve runtime implementations and build runtime-scoped value objects."""

    registry: RuntimeRegistry

    def resolve(self, name: str | None = None) -> WorkflowRuntime:
        """Resolve a runtime by name through the underlying registry."""
        return self.registry.resolve(name)

    def create_workflow_metadata(
        self,
        *,
        workflow_id: str,
        name: str,
        version: str | None = None,
        description: str | None = None,
        capabilities: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowMetadata:
        """Create a workflow metadata value object."""
        return WorkflowMetadata(
            workflow_id=workflow_id,
            name=name,
            version=version,
            description=description,
            capabilities=capabilities,
            labels=labels,
            metadata=metadata or {},
        )

    def create_execution(
        self,
        *,
        execution_id: str,
        workflow_id: str,
        runtime_name: str,
        attempt: int = 1,
        status: str = "pending",
        checkpoint_id: str | None = None,
        parent_checkpoint_id: str | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowExecution:
        """Create a workflow execution value object."""
        return WorkflowExecution(
            execution_id=execution_id,
            workflow_id=workflow_id,
            runtime_name=runtime_name,
            attempt=attempt,
            status=status,
            checkpoint_id=checkpoint_id,
            parent_checkpoint_id=parent_checkpoint_id,
            started_at=started_at,
            ended_at=ended_at,
            metadata=metadata or {},
        )

    def create_context(
        self,
        *,
        workflow: WorkflowMetadata,
        execution: WorkflowExecution,
        correlation_id: str | None = None,
        tenant_id: str | None = None,
        actor_id: str | None = None,
        deadline: datetime | None = None,
        timeout_seconds: float | None = None,
        attempt: int = 1,
        max_attempts: int = 1,
        retry_delay_seconds: float | None = None,
        cancellation_token: RuntimeCancellationToken | None = None,
        event_emitter: RuntimeEventEmitter | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimeContext:
        """Create a runtime execution context."""
        return RuntimeContext(
            workflow=workflow,
            execution=execution,
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            actor_id=actor_id,
            deadline=deadline,
            timeout_seconds=timeout_seconds,
            attempt=attempt,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
            cancellation_token=cancellation_token,
            event_emitter=event_emitter,
            metadata=metadata or {},
        )
