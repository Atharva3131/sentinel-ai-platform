"""Tool value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from backend.tools.exceptions import ToolException


class ToolPermission(StrEnum):
    """Normalized tool permission values."""

    NONE = "none"
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


@dataclass(frozen=True, slots=True)
class ToolMetadata:
    """Immutable metadata describing a tool implementation.

    `input_schema` and `output_schema` are intended to be JSON Schema compatible payloads so the
    tool surface can be exported to systems like MCP in the future.
    """

    name: str
    version: str | None = None
    description: str | None = None
    permissions: tuple[ToolPermission, ...] = ()
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_descriptor(self) -> dict[str, Any]:
        """Return a tool descriptor suitable for external registries."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "permissions": [str(permission) for permission in self.permissions],
            "inputSchema": self.input_schema or {},
            "outputSchema": self.output_schema or {},
            "labels": list(self.labels),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolInput:
    """Input envelope supplied to tools."""

    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Normalized result returned by tools."""

    tool: ToolMetadata
    workflow_id: str | None
    execution_id: str | None
    status: str
    output: Any = None
    error: ToolException | None = None
    retryable: bool = False
    retry_after_seconds: float | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    emitted_events: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def duration_seconds(self, *, now: datetime | None = None) -> float | None:
        """Return execution duration when timestamps are available."""
        if self.started_at is None:
            return None
        current = now or datetime.now(UTC)
        finished_at = self.ended_at or current
        return max((finished_at - self.started_at).total_seconds(), 0.0)
