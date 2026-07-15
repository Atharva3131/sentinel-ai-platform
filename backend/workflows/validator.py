"""Workflow definition and execution validation."""

from __future__ import annotations

from dataclasses import dataclass

from backend.workflows.context import WorkflowContext
from backend.workflows.definition import WorkflowDefinition
from backend.workflows.lifecycle import WorkflowLifecycle
from backend.workflows.state import WorkflowState


class WorkflowValidationError(ValueError):
    """Raised when a workflow definition or execution is invalid."""


@dataclass(slots=True)
class WorkflowValidator:
    """Validate workflow definitions, contexts, and state transitions."""

    def validate_definition(self, definition: WorkflowDefinition) -> None:
        """Validate a workflow definition before registration or execution."""
        if not definition.workflow_id.strip():
            raise WorkflowValidationError("workflow_id must not be empty")
        if not definition.name.strip():
            raise WorkflowValidationError("name must not be empty")
        if not definition.version.strip():
            raise WorkflowValidationError("version must not be empty")
        if definition.runtime_name is not None and not definition.runtime_name.strip():
            raise WorkflowValidationError("runtime_name must not be blank")
        if definition.execution_policy.max_attempts < 1:
            raise WorkflowValidationError("execution_policy.max_attempts must be >= 1")

    def validate_context(self, context: WorkflowContext) -> None:
        """Validate a workflow execution context."""
        if not context.workflow_id.strip():
            raise WorkflowValidationError("workflow_id must not be empty")
        if not context.execution_id.strip():
            raise WorkflowValidationError("execution_id must not be empty")
        if context.workflow_version is not None and not context.workflow_version.strip():
            raise WorkflowValidationError("workflow_version must not be blank")
        if context.attempt < 1:
            raise WorkflowValidationError("attempt must be >= 1")
        if context.max_attempts < 1:
            raise WorkflowValidationError("max_attempts must be >= 1")

    def validate_state(
        self,
        state: WorkflowState,
        lifecycle: WorkflowLifecycle | None = None,
    ) -> None:
        """Validate that the state is structurally and transitionally sound."""
        if not state.workflow_id.strip():
            raise WorkflowValidationError("state.workflow_id must not be empty")
        if not state.workflow_version.strip():
            raise WorkflowValidationError("state.workflow_version must not be empty")
        if not state.execution_id.strip():
            raise WorkflowValidationError("state.execution_id must not be empty")
        if state.attempt < 0:
            raise WorkflowValidationError("state.attempt must be >= 0")
        if lifecycle is not None and state.status not in lifecycle.transitions:
            raise WorkflowValidationError(f"Unknown workflow status '{state.status}'")

    def validate_transition(
        self,
        state: WorkflowState,
        next_status: str,
        lifecycle: WorkflowLifecycle | None = None,
    ) -> None:
        """Validate a requested state transition."""
        resolved_lifecycle = lifecycle or WorkflowLifecycle()
        if not resolved_lifecycle.can_transition(state.status, next_status):
            raise WorkflowValidationError(
                f"Transition '{state.status}' -> '{next_status}' is not allowed"
            )

