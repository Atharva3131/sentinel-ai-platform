"""SRE workflow exception hierarchy."""

from __future__ import annotations

from typing import Any

from backend.exceptions import SentinelError


class SREWorkflowError(SentinelError):
    """Base exception for all SRE workflow failures."""


class SREPhaseError(SREWorkflowError):
    """Raised when a specific workflow phase fails.

    ``phase`` identifies the orchestration stage (e.g. 'context_retrieval',
    'plan_build', 'analysis', 'confidence', 'approval', 'recovery', 'audit').
    ``retryable`` signals whether the executor may schedule another attempt.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        incident_id: str | None = None,
        retryable: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.incident_id = incident_id
        self.retryable = retryable
        self.metadata: dict[str, Any] = metadata or {}


class IncidentContextError(SREWorkflowError):
    """Raised when the incident payload is invalid or missing required fields."""


class ApprovalTimeoutError(SREWorkflowError):
    """Raised when an approval request is not answered within its deadline."""

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        incident_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.incident_id = incident_id


class AuditError(SREWorkflowError):
    """Raised when audit-log persistence fails."""
