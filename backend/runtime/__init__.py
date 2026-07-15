"""Framework-neutral runtime contracts and helpers."""

from backend.runtime.context import RuntimeContext, WorkflowExecution, WorkflowMetadata
from backend.runtime.contracts import (
    RuntimeCancellationToken,
    RuntimeEventEmitter,
    WorkflowRuntime,
)
from backend.runtime.exceptions import RuntimeException
from backend.runtime.factory import RuntimeFactory
from backend.runtime.registry import RuntimeRegistry
from backend.runtime.results import RuntimeResult

__all__ = [
    "RuntimeCancellationToken",
    "RuntimeContext",
    "RuntimeEventEmitter",
    "RuntimeException",
    "RuntimeFactory",
    "RuntimeRegistry",
    "RuntimeResult",
    "WorkflowExecution",
    "WorkflowMetadata",
    "WorkflowRuntime",
]
