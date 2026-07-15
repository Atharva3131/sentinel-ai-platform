"""Agent value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from backend.agents.exceptions import AgentException
from backend.agents.lifecycle import AgentStatus


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    """Declared capabilities exposed by an agent implementation."""

    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentMetadata:
    """Immutable metadata describing one agent implementation."""

    name: str
    version: str | None = None
    description: str | None = None
    capabilities: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentInput:
    """Input envelope supplied to agents."""

    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentOutput:
    """Output envelope returned by agents."""

    payload: Any = None
    artifacts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    """Normalized agent execution result."""

    agent: AgentMetadata
    workflow_id: str | None
    execution_id: str | None
    status: AgentStatus
    output: AgentOutput | None = None
    error: AgentException | None = None
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
