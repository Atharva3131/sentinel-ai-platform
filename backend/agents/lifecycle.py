"""Agent lifecycle state machine primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class AgentStatus(StrEnum):
    """Normalized agent execution status."""

    PENDING = "pending"
    RUNNING = "running"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class AgentLifecycle:
    """Declarative lifecycle for agent execution transitions."""

    initial_status: AgentStatus = AgentStatus.PENDING
    terminal_statuses: frozenset[AgentStatus] = field(
        default_factory=lambda: frozenset(
            {
                AgentStatus.COMPLETED,
                AgentStatus.FAILED,
                AgentStatus.CANCELLED,
                AgentStatus.TIMED_OUT,
            }
        )
    )
    transitions: dict[AgentStatus, frozenset[AgentStatus]] = field(
        default_factory=lambda: {
            AgentStatus.PENDING: frozenset({AgentStatus.RUNNING, AgentStatus.CANCELLED}),
            AgentStatus.RUNNING: frozenset(
                {
                    AgentStatus.COMPLETED,
                    AgentStatus.FAILED,
                    AgentStatus.RETRYING,
                    AgentStatus.CANCELLED,
                    AgentStatus.TIMED_OUT,
                }
            ),
            AgentStatus.RETRYING: frozenset({AgentStatus.RUNNING, AgentStatus.CANCELLED}),
        }
    )

    def is_terminal(self, status: AgentStatus) -> bool:
        """Return `True` when the supplied status is terminal."""
        return status in self.terminal_statuses

    def can_transition(self, current: AgentStatus, target: AgentStatus) -> bool:
        """Return `True` when a transition is permitted."""
        if current == target:
            return True
        allowed = self.transitions.get(current)
        if allowed is None:
            return False
        return target in allowed
