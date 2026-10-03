"""Deployment-specific error hierarchy — domain-neutral.

These errors are produced by the deployment provider layer and consumed by
the orchestration pipeline.  They do not reference GitHub API concepts
beyond what is necessary for debugging.
"""

from __future__ import annotations


class DeploymentError(Exception):
    """Base for all deployment errors."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class DeploymentAuthError(DeploymentError):
    """Bad or missing credentials for the deployment provider."""


class DeploymentNotFoundError(DeploymentError):
    """Workflow, environment, or repository not found."""

    def __init__(
        self, message: str, *, provider: str | None = None, resource: str | None = None
    ) -> None:
        super().__init__(message, provider=provider)
        self.resource = resource


class DeploymentConflictError(DeploymentError):
    """Duplicate trigger or conflicting deployment state."""


class DeploymentUnavailableError(DeploymentError):
    """Provider is unreachable or returned a server error."""


class DeploymentTimeoutError(DeploymentError):
    """Deployment exceeded the configured timeout budget."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.timeout_seconds = timeout_seconds


class DeploymentCancelledError(DeploymentError):
    """Deployment was cancelled before completion."""


class DeploymentPolicyError(DeploymentError):
    """Deployment was rejected by the ActionPolicy engine."""
