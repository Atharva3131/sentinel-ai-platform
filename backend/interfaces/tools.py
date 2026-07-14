"""Tool execution contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.interfaces.common import ProviderContext


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Declared tool capabilities exposed to the runtime."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    requires_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """A single tool call request."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    """Normalized tool execution result."""

    tool_name: str
    success: bool
    output: Any = None
    error: str | None = None
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ToolExecutor(Protocol):
    """Contract implemented by governed tool execution adapters."""

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        """Return the tool catalog exposed by the executor."""
        ...

    async def execute(self, invocation: ToolInvocation) -> ToolExecutionResult:
        """Execute one approved tool invocation."""
        ...

