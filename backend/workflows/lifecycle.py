"""Workflow lifecycle rules and transition helpers."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class WorkflowLifecycle:
    """Declarative workflow lifecycle model.

    The lifecycle defines the allowed status transitions, terminal states, and the initial status
    assigned to a newly created workflow execution.
    """

    initial_status: str = "pending"
    active_statuses: tuple[str, ...] = (
        "pending",
        "ready",
        "running",
        "paused",
        "retrying",
        "recovering",
    )
    terminal_statuses: tuple[str, ...] = (
        "completed",
        "failed",
        "cancelled",
        "timed_out",
    )
    pending_status: str = "pending"
    ready_status: str = "ready"
    running_status: str = "running"
    paused_status: str = "paused"
    retrying_status: str = "retrying"
    recovering_status: str = "recovering"
    completed_status: str = "completed"
    failed_status: str = "failed"
    cancelled_status: str = "cancelled"
    timed_out_status: str = "timed_out"
    transitions: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "pending": ("ready", "running", "cancelled", "failed"),
            "ready": ("running", "cancelled", "failed"),
            "running": (
                "paused",
                "retrying",
                "recovering",
                "completed",
                "failed",
                "cancelled",
                "timed_out",
            ),
            "paused": ("running", "cancelled", "failed"),
            "retrying": ("running", "cancelled", "failed", "timed_out"),
            "recovering": ("running", "failed", "cancelled"),
            "completed": (),
            "failed": (),
            "cancelled": (),
            "timed_out": (),
        }
    )

    def is_terminal(self, status: str) -> bool:
        """Return `True` when the status is terminal."""
        return status in self.terminal_statuses

    def can_transition(self, current_status: str, next_status: str) -> bool:
        """Return `True` when the lifecycle permits the requested transition."""
        if current_status == next_status:
            return True
        allowed = self.transitions.get(current_status, ())
        return next_status in allowed

    def can_restart(self, status: str) -> bool:
        """Return `True` when an execution can be restarted from the status."""
        return status in {"pending", "ready", "paused", "retrying", "recovering", "failed"}
