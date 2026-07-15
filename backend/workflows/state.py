"""Workflow execution state."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from backend.runtime.results import RuntimeResult
from backend.workflows.lifecycle import WorkflowLifecycle


@dataclass(frozen=True, slots=True)
class WorkflowState:
    """Mutable-by-replacement workflow state for one execution."""

    workflow_id: str
    workflow_version: str
    execution_id: str
    status: str = "pending"
    attempt: int = 0
    recovery_count: int = 0
    checkpoint_id: str | None = None
    started_at: datetime | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    last_result: RuntimeResult | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def initial(
        cls,
        *,
        workflow_id: str,
        workflow_version: str,
        execution_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> WorkflowState:
        """Create the initial execution state."""
        return cls(
            workflow_id=workflow_id,
            workflow_version=workflow_version,
            execution_id=execution_id,
            metadata=metadata or {},
        )

    def is_terminal(self, lifecycle: WorkflowLifecycle | None = None) -> bool:
        """Return `True` when the state is terminal."""
        return (lifecycle or WorkflowLifecycle()).is_terminal(self.status)

    def can_restart(self, lifecycle: WorkflowLifecycle | None = None) -> bool:
        """Return `True` when the state can be resumed or retried."""
        return (lifecycle or WorkflowLifecycle()).can_restart(self.status)

    def with_status(
        self,
        status: str,
        *,
        attempt: int | None = None,
        checkpoint_id: str | None = None,
        last_error: str | None = None,
        last_result: RuntimeResult | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        clear_ended_at: bool = False,
        metadata: dict[str, Any] | None = None,
        recovery_count: int | None = None,
    ) -> WorkflowState:
        """Return a copy with updated status and metadata."""
        merged_metadata = dict(self.metadata)
        if metadata:
            merged_metadata.update(metadata)
        return replace(
            self,
            status=status,
            attempt=self.attempt if attempt is None else attempt,
            checkpoint_id=self.checkpoint_id if checkpoint_id is None else checkpoint_id,
            last_error=last_error,
            last_result=last_result,
            started_at=self.started_at if started_at is None else started_at,
            ended_at=None if clear_ended_at else self.ended_at if ended_at is None else ended_at,
            metadata=merged_metadata,
            recovery_count=self.recovery_count if recovery_count is None else recovery_count,
            updated_at=datetime.now(UTC),
        )

    def with_result(self, result: RuntimeResult) -> WorkflowState:
        """Return a copy updated from a runtime result."""
        metadata = dict(self.metadata)
        metadata.update(result.metadata)
        checkpoint_id = metadata.get("checkpoint_id", self.checkpoint_id)
        last_error = None if result.error is None else str(result.error)
        return replace(
            self,
            status=result.status,
            attempt=self.attempt,
            checkpoint_id=checkpoint_id,
            last_result=result,
            last_error=last_error,
            ended_at=(
                datetime.now(UTC)
                if result.status in WorkflowLifecycle().terminal_statuses
                else None
            ),
            metadata=metadata,
            updated_at=datetime.now(UTC),
        )

    def with_checkpoint(self, checkpoint_id: str | None) -> WorkflowState:
        """Return a copy with a new checkpoint reference."""
        return replace(self, checkpoint_id=checkpoint_id, updated_at=datetime.now(UTC))

    def with_recovery(self) -> WorkflowState:
        """Return a copy marked as recovered once."""
        return replace(
            self,
            recovery_count=self.recovery_count + 1,
            updated_at=datetime.now(UTC),
        )
