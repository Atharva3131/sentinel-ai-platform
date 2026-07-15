"""Workflow definition models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.workflows.lifecycle import WorkflowLifecycle
from backend.workflows.policy import ExecutionPolicy


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Immutable declaration of a workflow version."""

    workflow_id: str
    name: str
    version: str
    runtime_name: str | None = None
    description: str | None = None
    capabilities: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    lifecycle: WorkflowLifecycle = field(default_factory=WorkflowLifecycle)

