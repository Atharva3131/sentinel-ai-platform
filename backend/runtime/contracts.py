"""Runtime execution contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from backend.runtime.context import RuntimeContext, WorkflowMetadata
    from backend.runtime.results import RuntimeResult


@runtime_checkable
class RuntimeCancellationToken(Protocol):
    """Contract for cooperative cancellation signals."""

    def is_set(self) -> bool:
        """Return `True` when cancellation has been requested."""
        ...

    async def wait(self) -> None:
        """Wait until cancellation is requested."""
        ...


@runtime_checkable
class RuntimeEventEmitter(Protocol):
    """Contract for runtime event emission hooks."""

    async def emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: RuntimeContext,
    ) -> None:
        """Emit a runtime event with the active execution context."""
        ...


@runtime_checkable
class WorkflowRuntime(Protocol):
    """Framework-neutral workflow runtime interface."""

    @property
    def name(self) -> str:
        """Return the stable runtime name used for registry lookup."""
        ...

    @property
    def version(self) -> str | None:
        """Return the runtime implementation version, if declared."""
        ...

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Return the workflow capabilities supported by this runtime."""
        ...

    def supports(self, workflow: WorkflowMetadata) -> bool:
        """Return `True` when the runtime can execute the supplied workflow."""
        ...

    async def execute(self, context: RuntimeContext) -> RuntimeResult:
        """Execute the workflow described by the supplied runtime context."""
        ...

    async def cancel(self, context: RuntimeContext) -> None:
        """Request cancellation for the supplied workflow execution."""
        ...
