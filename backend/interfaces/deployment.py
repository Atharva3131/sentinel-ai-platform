"""DeploymentProvider protocol — provider-neutral deployment interface.

All deployment adapters (GitHub Actions, future Kubernetes/Argo, etc.) must
satisfy this structural protocol.  The domain and orchestration layers
depend only on this interface; they never import a concrete adapter.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.models.deployment import (
    DeploymentRequest,
    DeploymentStatus,
)


@runtime_checkable
class DeploymentProvider(Protocol):
    """Contract for triggering and monitoring deployments."""

    @property
    def name(self) -> str:
        """Return the stable provider name for registry lookup and logging."""
        ...

    async def trigger(
        self,
        request: DeploymentRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> DeploymentStatus:
        """Trigger a deployment and return the initial status.

        The returned status will typically be TRIGGERED or RUNNING.
        Callers should poll ``get_status()`` until ``status.is_terminal``.

        Raises:
            DeploymentAuthError: bad or missing credentials.
            DeploymentNotFoundError: workflow/environment not found.
            DeploymentConflictError: duplicate trigger (idempotency).
            DeploymentUnavailableError: provider unreachable.
            DeploymentTimeoutError: trigger itself timed out.
        """
        ...

    async def get_status(
        self,
        request: DeploymentRequest,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> DeploymentStatus:
        """Return the current status of a previously triggered deployment.

        ``run_id`` is the provider-assigned run identifier returned by
        ``trigger()``.
        """
        ...

    async def cancel(
        self,
        request: DeploymentRequest,
        run_id: str,
    ) -> DeploymentStatus:
        """Cancel a running deployment.

        Returns the final status after cancellation is confirmed.
        May return CANCELLED or FAILED if the provider does not support
        mid-flight cancellation.
        """
        ...

    async def health_check(self) -> bool:
        """Return True when the provider is reachable and configured."""
        ...
