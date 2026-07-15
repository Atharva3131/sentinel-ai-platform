"""Tool middleware contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from backend.tools.context import ToolContext
from backend.tools.models import ToolInput, ToolResult
from backend.tools.tool import Tool


@dataclass(frozen=True, slots=True)
class ToolExecutionRequest:
    """Request object passed through the tool middleware pipeline."""

    tool: Tool
    context: ToolContext
    tool_input: ToolInput
    state: dict[str, Any] = field(default_factory=dict)

    def with_context(self, context: ToolContext) -> ToolExecutionRequest:
        """Return a copy with updated context."""
        return replace(self, context=context)

    def with_input(self, tool_input: ToolInput) -> ToolExecutionRequest:
        """Return a copy with updated input."""
        return replace(self, tool_input=tool_input)

    def with_state(self, **values: Any) -> ToolExecutionRequest:
        """Return a copy with merged middleware state."""
        state = dict(self.state)
        state.update(values)
        return replace(self, state=state)

    def with_metadata(self, **values: Any) -> ToolExecutionRequest:
        """Return a copy with merged context metadata."""
        metadata = dict(self.context.metadata)
        metadata.update(values)
        return self.with_context(replace(self.context, metadata=metadata))


ToolMiddlewareNext = Callable[[ToolExecutionRequest], Awaitable[ToolResult]]


@runtime_checkable
class ToolExecutionMiddleware(Protocol):
    """Async middleware contract for tool execution."""

    async def __call__(
        self,
        request: ToolExecutionRequest,
        call_next: ToolMiddlewareNext,
    ) -> ToolResult:
        """Invoke the middleware and optionally continue the chain."""
        ...
