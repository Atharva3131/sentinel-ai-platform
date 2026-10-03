"""Fake DeploymentProvider for tests and local development.

Returns configurable responses without making any network calls.
"""

from __future__ import annotations

from datetime import UTC, datetime

from backend.models.deployment import (
    DeploymentRequest,
    DeploymentState,
    DeploymentStatus,
)
from backend.providers.github.deployment_errors import DeploymentAuthError


class FakeDeploymentProvider:
    """Configurable in-memory deployment provider — zero network calls."""

    def __init__(
        self,
        *,
        trigger_state: DeploymentState = DeploymentState.TRIGGERED,
        final_state: DeploymentState = DeploymentState.SUCCEEDED,
        run_id: str = "fake-run-001",
        fail_auth: bool = False,
    ) -> None:
        self._trigger_state = trigger_state
        self._final_state = final_state
        self._run_id = run_id
        self._fail_auth = fail_auth
        self.triggered: list[DeploymentRequest] = []
        self.polled: list[str] = []
        self.cancelled: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    async def trigger(
        self,
        request: DeploymentRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> DeploymentStatus:
        if self._fail_auth:
            raise DeploymentAuthError("Fake auth failure", provider=self.name)
        self.triggered.append(request)
        return DeploymentStatus(
            deployment_id=request.deployment_id,
            run_id=self._run_id,
            state=self._trigger_state,
            environment=request.environment,
            started_at=datetime.now(UTC),
        )

    async def get_status(
        self,
        request: DeploymentRequest,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
    ) -> DeploymentStatus:
        self.polled.append(run_id)
        return DeploymentStatus(
            deployment_id=request.deployment_id,
            run_id=run_id or self._run_id,
            state=self._final_state,
            environment=request.environment,
            conclusion="success" if self._final_state == DeploymentState.SUCCEEDED else "failure",
        )

    async def cancel(
        self,
        request: DeploymentRequest,
        run_id: str,
    ) -> DeploymentStatus:
        self.cancelled.append(run_id)
        return DeploymentStatus(
            deployment_id=request.deployment_id,
            run_id=run_id,
            state=DeploymentState.CANCELLED,
            environment=request.environment,
        )

    async def health_check(self) -> bool:
        return not self._fail_auth
