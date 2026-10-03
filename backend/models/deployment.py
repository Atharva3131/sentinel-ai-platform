"""Deployment domain models — provider-neutral.

These models represent the deployment lifecycle independently of any vendor
(GitHub Actions, Kubernetes, Argo, etc.).  No provider-specific field names
or concepts appear here.

``DeploymentState`` is the canonical status vocabulary.  All provider adapters
map their native statuses into these values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class DeploymentState(StrEnum):
    """Provider-neutral deployment lifecycle state."""

    PENDING = "pending"        # created, not yet triggered
    TRIGGERED = "triggered"    # trigger request sent; waiting for provider ack
    RUNNING = "running"        # actively deploying
    SUCCEEDED = "succeeded"    # deployment completed successfully
    FAILED = "failed"          # deployment completed with errors
    CANCELLED = "cancelled"    # explicitly cancelled
    TIMED_OUT = "timed_out"    # exceeded the configured timeout budget
    UNKNOWN = "unknown"        # state cannot be determined (transient)


@dataclass(frozen=True, slots=True)
class DeploymentRequest:
    """Parameters for triggering one deployment.

    ``workflow_id``   — provider-specific workflow/pipeline identifier
                        (e.g. "deploy.yml" for GitHub Actions)
    ``environment``   — target environment name (e.g. "staging")
    ``ref``           — branch/tag/SHA to deploy
    ``parameters``    — explicitly allow-listed workflow inputs; no secrets
    ``incident_id``   — links the deployment back to the incident that caused it
    ``correlation_id``— end-to-end tracing identifier
    ``idempotency_key``— prevents duplicate triggers for the same incident/env
    """

    deployment_id: str
    owner: str
    repository: str
    workflow_id: str
    environment: str
    ref: str
    parameters: dict[str, str] = field(default_factory=dict)
    incident_id: str | None = None
    correlation_id: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeploymentStatus:
    """Snapshot of a deployment's current state.

    ``run_id``    — provider-assigned run/execution identifier (for polling)
    ``state``     — domain-neutral deployment state
    ``html_url``  — human-readable URL to the run/deployment in the provider UI
    ``conclusion``— provider-native conclusion string (for audit; not for logic)
    ``duration_ms``— elapsed time since trigger (None when not yet complete)
    """

    deployment_id: str
    run_id: str | None
    state: DeploymentState
    html_url: str | None = None
    conclusion: str | None = None
    environment: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            DeploymentState.SUCCEEDED,
            DeploymentState.FAILED,
            DeploymentState.CANCELLED,
            DeploymentState.TIMED_OUT,
        )

    @property
    def succeeded(self) -> bool:
        return self.state == DeploymentState.SUCCEEDED


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    """Final outcome of a deployment lifecycle.

    Produced after polling reaches a terminal state or the timeout fires.
    ``status`` is the final ``DeploymentStatus``; ``error`` is populated
    only on failure/timeout.
    """

    request: DeploymentRequest
    status: DeploymentStatus
    error: str | None = None
    total_duration_ms: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status.succeeded
