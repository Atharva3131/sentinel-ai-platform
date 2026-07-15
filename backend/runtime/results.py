"""Runtime result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.runtime.context import WorkflowExecution, WorkflowMetadata
from backend.runtime.exceptions import RuntimeException


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """Normalized result returned by a runtime implementation."""

    workflow: WorkflowMetadata
    execution: WorkflowExecution
    status: str
    output: Any = None
    error: RuntimeException | None = None
    retryable: bool = False
    retry_after_seconds: float | None = None
    emitted_events: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

