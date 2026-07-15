"""Workflow definition builder."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from backend.workflows.definition import WorkflowDefinition
from backend.workflows.lifecycle import WorkflowLifecycle
from backend.workflows.policy import ExecutionPolicy
from backend.workflows.validator import WorkflowValidator


@dataclass(slots=True)
class WorkflowBuilder:
    """Fluent builder for workflow definitions."""

    workflow_id: str
    name: str
    version: str = "1.0.0"
    runtime_name: str | None = None
    description: str | None = None
    capabilities: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    lifecycle: WorkflowLifecycle = field(default_factory=WorkflowLifecycle)
    validator: WorkflowValidator = field(default_factory=WorkflowValidator, repr=False)

    def with_version(self, version: str) -> WorkflowBuilder:
        """Return a builder with a new version."""
        return replace(self, version=version)

    def with_runtime(self, runtime_name: str | None) -> WorkflowBuilder:
        """Return a builder with a new runtime binding."""
        return replace(self, runtime_name=runtime_name)

    def with_description(self, description: str | None) -> WorkflowBuilder:
        """Return a builder with a new description."""
        return replace(self, description=description)

    def with_capabilities(self, *capabilities: str) -> WorkflowBuilder:
        """Return a builder with replacement capabilities."""
        return replace(self, capabilities=tuple(capabilities))

    def with_labels(self, *labels: str) -> WorkflowBuilder:
        """Return a builder with replacement labels."""
        return replace(self, labels=tuple(labels))

    def with_metadata(self, **metadata: Any) -> WorkflowBuilder:
        """Return a builder with merged metadata."""
        merged = dict(self.metadata)
        merged.update(metadata)
        return replace(self, metadata=merged)

    def with_execution_policy(self, execution_policy: ExecutionPolicy) -> WorkflowBuilder:
        """Return a builder with a replacement execution policy."""
        return replace(self, execution_policy=execution_policy)

    def with_lifecycle(self, lifecycle: WorkflowLifecycle) -> WorkflowBuilder:
        """Return a builder with a replacement lifecycle."""
        return replace(self, lifecycle=lifecycle)

    def build(self) -> WorkflowDefinition:
        """Validate and construct an immutable workflow definition."""
        definition = WorkflowDefinition(
            workflow_id=self.workflow_id,
            name=self.name,
            version=self.version,
            runtime_name=self.runtime_name,
            description=self.description,
            capabilities=self.capabilities,
            labels=self.labels,
            metadata=dict(self.metadata),
            execution_policy=self.execution_policy,
            lifecycle=self.lifecycle,
        )
        self.validator.validate_definition(definition)
        return definition

