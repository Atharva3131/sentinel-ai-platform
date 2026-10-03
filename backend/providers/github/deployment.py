"""GitHubActionsDeploymentProvider — implements DeploymentProvider for GitHub Actions.

Trigger flow:
  1. Send a ``workflow_dispatch`` event to the target workflow.
  2. Poll ``/actions/workflows/{id}/runs`` to find the run created after trigger.
  3. Poll the run's status until it reaches a terminal state or timeout fires.
  4. Map the GitHub-native conclusion to a domain DeploymentState.

State mapping:
  GitHub run status     → DeploymentState
  ─────────────────────────────────────────
  queued / pending      → TRIGGERED
  in_progress / waiting → RUNNING
  completed + success   → SUCCEEDED
  completed + failure   → FAILED
  completed + cancelled → CANCELLED
  completed + timed_out → TIMED_OUT
  completed + (other)   → FAILED

Security:
  * Token is never logged or added to OTel span attributes.
  * Workflow ``inputs`` are passed through unmodified but callers must ensure
    only explicitly allow-listed keys are present before calling trigger().

OTel instrumentation:
  Each public method has its own span.  Attributes include:
  incident.id, correlation.id, github.repo, github.workflow,
  github.run_id, deployment.state, deployment.environment.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from backend.configuration.settings import DeploymentSettings, GitHubSettings
from backend.models.deployment import (
    DeploymentRequest,
    DeploymentResult,
    DeploymentState,
    DeploymentStatus,
)
from backend.providers.github.client import GitHubClient
from backend.providers.github.deployment_errors import (
    DeploymentAuthError,
    DeploymentError,
    DeploymentNotFoundError,
    DeploymentTimeoutError,
    DeploymentUnavailableError,
)
from backend.providers.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubTimeoutError,
)

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer("sentinel.deployment.github_actions")

_PROVIDER_NAME = "github_actions"

# GitHub statuses that represent a completed run
_TERMINAL_RUN_STATUSES = frozenset({"completed", "action_required", "neutral", "stale"})

# Seconds to wait between trigger and first status poll
# (GitHub needs a moment to create the run record)
_TRIGGER_SETTLE_SECONDS = 2.0

# Maximum runs to scan when matching a triggered run
_RUN_SCAN_LIMIT = 10


def _map_github_run_state(
    status: str, conclusion: str | None
) -> DeploymentState:
    """Map GitHub run status + conclusion to a domain DeploymentState."""
    if status not in _TERMINAL_RUN_STATUSES:
        if status in ("queued", "pending", "waiting"):
            return DeploymentState.TRIGGERED
        return DeploymentState.RUNNING

    if conclusion is None:
        return DeploymentState.RUNNING

    mapping: dict[str, DeploymentState] = {
        "success":        DeploymentState.SUCCEEDED,
        "failure":        DeploymentState.FAILED,
        "cancelled":      DeploymentState.CANCELLED,
        "timed_out":      DeploymentState.TIMED_OUT,
        "action_required": DeploymentState.FAILED,
        "neutral":        DeploymentState.SUCCEEDED,
        "skipped":        DeploymentState.CANCELLED,
        "stale":          DeploymentState.FAILED,
    }
    return mapping.get(conclusion, DeploymentState.FAILED)


@dataclass
class GitHubActionsDeploymentProvider:
    """Triggers and monitors GitHub Actions workflow-dispatch deployments.

    Instantiate via ``GitHubActionsDeploymentProvider.from_settings(...)``.
    """

    client: GitHubClient
    default_workflow: str = "deploy.yml"
    default_environment: str = "staging"
    poll_interval_seconds: float = 15.0
    deployment_timeout_seconds: float = 600.0

    @classmethod
    def from_settings(
        cls,
        github_settings: GitHubSettings,
        deployment_settings: DeploymentSettings,
    ) -> GitHubActionsDeploymentProvider:
        """Build a provider from typed settings; no secrets in repr."""
        client = GitHubClient.from_settings(github_settings)
        return cls(
            client=client,
            default_workflow=deployment_settings.workflow,
            default_environment=deployment_settings.environment,
            poll_interval_seconds=deployment_settings.poll_interval_seconds,
            deployment_timeout_seconds=deployment_settings.timeout_seconds,
        )

    @property
    def name(self) -> str:
        return _PROVIDER_NAME

    # ── Public protocol methods ────────────────────────────────────────────

    async def trigger(
        self,
        request: DeploymentRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> DeploymentStatus:
        """Dispatch a workflow and return an initial TRIGGERED status.

        Raises domain DeploymentError subclasses; never GitHubError directly.
        """
        t0 = time.monotonic()
        with _tracer.start_as_current_span(
            "deployment.trigger", kind=trace.SpanKind.CLIENT
        ) as span:
            _set_span_attrs(span, request=request)

            bound_log = log.bind(
                provider=_PROVIDER_NAME,
                deployment_id=request.deployment_id,
                workflow=request.workflow_id,
                ref=request.ref,
                environment=request.environment,
                incident_id=request.incident_id,
                correlation_id=request.correlation_id,
            )
            bound_log.info("deployment_trigger_started")

            try:
                await self.client.trigger_workflow(
                    request.owner,
                    request.repository,
                    request.workflow_id,
                    request.ref,
                    inputs=request.parameters or None,
                    correlation_id=request.correlation_id,
                    incident_id=request.incident_id,
                )
            except GitHubAuthError as exc:
                span.set_status(StatusCode.ERROR)
                raise DeploymentAuthError(str(exc), provider=_PROVIDER_NAME) from exc
            except GitHubNotFoundError as exc:
                span.set_status(StatusCode.ERROR)
                raise DeploymentNotFoundError(
                    str(exc), provider=_PROVIDER_NAME,
                    resource=f"{request.owner}/{request.repository}/{request.workflow_id}",
                ) from exc
            except GitHubTimeoutError as exc:
                span.set_status(StatusCode.ERROR)
                raise DeploymentTimeoutError(
                    str(exc), provider=_PROVIDER_NAME,
                    timeout_seconds=exc.timeout_seconds,
                ) from exc
            except GitHubError as exc:
                span.set_status(StatusCode.ERROR)
                raise DeploymentUnavailableError(str(exc), provider=_PROVIDER_NAME) from exc

            latency_ms = (time.monotonic() - t0) * 1000
            span.set_attribute("deployment.latency_ms", round(latency_ms, 2))
            span.set_status(StatusCode.OK)

            bound_log.info("deployment_trigger_accepted", latency_ms=round(latency_ms, 2))

        return DeploymentStatus(
            deployment_id=request.deployment_id,
            run_id=None,   # not yet known; resolved on first get_status call
            state=DeploymentState.TRIGGERED,
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
        """Fetch the current status of a workflow run.

        When ``run_id`` is empty the method attempts to discover the most
        recent run for the workflow+ref combination.
        """
        with _tracer.start_as_current_span(
            "deployment.get_status", kind=trace.SpanKind.CLIENT
        ) as span:
            _set_span_attrs(span, request=request)
            if run_id:
                span.set_attribute("github.run_id", run_id)

            try:
                if run_id:
                    raw = await self.client.get_workflow_run(
                        request.owner, request.repository, int(run_id),
                        correlation_id=request.correlation_id,
                        incident_id=request.incident_id,
                    )
                else:
                    # Discover most recent run for the workflow+ref
                    raw = await self._find_latest_run(request)
            except GitHubAuthError as exc:
                span.set_status(StatusCode.ERROR)
                raise DeploymentAuthError(str(exc), provider=_PROVIDER_NAME) from exc
            except GitHubError as exc:
                span.set_status(StatusCode.ERROR)
                raise DeploymentUnavailableError(str(exc), provider=_PROVIDER_NAME) from exc

            status = _run_to_status(request.deployment_id, raw)
            span.set_attribute("deployment.state", status.state.value)
            span.set_status(StatusCode.OK)

        return status

    async def cancel(
        self,
        request: DeploymentRequest,
        run_id: str,
    ) -> DeploymentStatus:
        """Request cancellation of a running workflow run."""
        with _tracer.start_as_current_span(
            "deployment.cancel", kind=trace.SpanKind.CLIENT
        ) as span:
            _set_span_attrs(span, request=request)
            span.set_attribute("github.run_id", run_id)

            try:
                await self.client.cancel_workflow_run(
                    request.owner, request.repository, int(run_id),
                    correlation_id=request.correlation_id,
                    incident_id=request.incident_id,
                )
            except GitHubAuthError as exc:
                span.set_status(StatusCode.ERROR)
                raise DeploymentAuthError(str(exc), provider=_PROVIDER_NAME) from exc
            except GitHubError as exc:
                span.set_status(StatusCode.ERROR)
                raise DeploymentUnavailableError(str(exc), provider=_PROVIDER_NAME) from exc

            span.set_status(StatusCode.OK)

        return DeploymentStatus(
            deployment_id=request.deployment_id,
            run_id=run_id,
            state=DeploymentState.CANCELLED,
            environment=request.environment,
        )

    async def health_check(self) -> bool:
        return await self.client.health_check()

    # ── Polling helper ────────────────────────────────────────────────────

    async def wait_for_completion(
        self,
        request: DeploymentRequest,
        run_id: str,
        *,
        timeout_seconds: float | None = None,
        cancellation_check: Any = None,
    ) -> DeploymentResult:
        """Poll until the run reaches a terminal state or timeout fires.

        ``cancellation_check`` is an optional ``() -> bool`` callable.
        """
        effective_timeout = timeout_seconds or self.deployment_timeout_seconds
        poll_interval = self.poll_interval_seconds
        deadline = time.monotonic() + effective_timeout

        with _tracer.start_as_current_span(
            "deployment.wait", kind=trace.SpanKind.INTERNAL
        ) as span:
            _set_span_attrs(span, request=request)
            span.set_attribute("deployment.timeout_seconds", effective_timeout)
            t0 = time.monotonic()

            current_run_id = run_id
            last_status = DeploymentStatus(
                deployment_id=request.deployment_id,
                run_id=current_run_id or None,
                state=DeploymentState.TRIGGERED,
                environment=request.environment,
            )

            # Brief initial settle — GitHub needs a moment to create the run
            if not current_run_id:
                await asyncio.sleep(_TRIGGER_SETTLE_SECONDS)

            while True:
                # Cancellation check
                if cancellation_check is not None and cancellation_check():
                    if current_run_id:
                        try:
                            await self.cancel(request, current_run_id)
                        except DeploymentError:
                            pass
                    span.set_status(StatusCode.OK)
                    return DeploymentResult(
                        request=request,
                        status=DeploymentStatus(
                            deployment_id=request.deployment_id,
                            run_id=current_run_id or None,
                            state=DeploymentState.CANCELLED,
                            environment=request.environment,
                        ),
                        error="Cancelled by caller",
                        total_duration_ms=(time.monotonic() - t0) * 1000,
                    )

                # Timeout check
                if time.monotonic() >= deadline:
                    span.set_attribute("deployment.timed_out", True)
                    span.set_status(StatusCode.OK)
                    return DeploymentResult(
                        request=request,
                        status=DeploymentStatus(
                            deployment_id=request.deployment_id,
                            run_id=current_run_id or None,
                            state=DeploymentState.TIMED_OUT,
                            environment=request.environment,
                        ),
                        error=f"Deployment timed out after {effective_timeout}s",
                        total_duration_ms=(time.monotonic() - t0) * 1000,
                    )

                try:
                    last_status = await self.get_status(
                        request, current_run_id or "",
                    )
                except DeploymentError as exc:
                    log.warning(
                        "deployment_poll_error",
                        error=str(exc),
                        deployment_id=request.deployment_id,
                    )
                    await asyncio.sleep(poll_interval)
                    continue

                # Capture run_id once discovered
                if last_status.run_id and not current_run_id:
                    current_run_id = last_status.run_id

                log.info(
                    "deployment_poll",
                    state=last_status.state.value,
                    run_id=current_run_id,
                    deployment_id=request.deployment_id,
                )

                if last_status.is_terminal:
                    total_ms = (time.monotonic() - t0) * 1000
                    span.set_attribute("deployment.state", last_status.state.value)
                    span.set_attribute("deployment.duration_ms", round(total_ms, 2))
                    span.set_status(StatusCode.OK)
                    return DeploymentResult(
                        request=request,
                        status=last_status,
                        error=None if last_status.succeeded else last_status.conclusion,
                        total_duration_ms=total_ms,
                    )

                await asyncio.sleep(poll_interval)

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _find_latest_run(self, request: DeploymentRequest) -> dict[str, Any]:
        """Discover the most recently created run for the workflow+ref pair."""
        runs = await self.client.list_workflow_runs(
            request.owner,
            request.repository,
            request.workflow_id,
            branch=request.ref if not _looks_like_sha(request.ref) else None,
            per_page=_RUN_SCAN_LIMIT,
            correlation_id=request.correlation_id,
            incident_id=request.incident_id,
        )
        if not runs:
            raise DeploymentNotFoundError(
                f"No runs found for workflow '{request.workflow_id}' on ref '{request.ref}'",
                provider=_PROVIDER_NAME,
            )
        return runs[0]


def _run_to_status(deployment_id: str, run: dict[str, Any]) -> DeploymentStatus:
    """Convert a raw GitHub workflow run dict to a DeploymentStatus."""
    status_str: str = run.get("status", "unknown")
    conclusion: str | None = run.get("conclusion")
    run_id: str = str(run.get("id", ""))

    state = _map_github_run_state(status_str, conclusion)

    started_raw = run.get("created_at") or run.get("run_started_at")
    completed_raw = run.get("updated_at") if status_str == "completed" else None

    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: float | None = None

    if started_raw:
        try:
            started_at = datetime.fromisoformat(started_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass

    if completed_raw and started_at:
        try:
            completed_at = datetime.fromisoformat(completed_raw.replace("Z", "+00:00"))
            duration_ms = (completed_at - started_at).total_seconds() * 1000
        except (ValueError, AttributeError):
            pass

    return DeploymentStatus(
        deployment_id=deployment_id,
        run_id=run_id,
        state=state,
        html_url=run.get("html_url"),
        conclusion=conclusion,
        environment=run.get("head_branch"),
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        metadata={
            "github_run_number": run.get("run_number"),
            "github_status": status_str,
        },
    )


def _looks_like_sha(ref: str) -> bool:
    return len(ref) == 40 and all(c in "0123456789abcdefABCDEF" for c in ref)


def _set_span_attrs(span: Any, *, request: DeploymentRequest) -> None:
    from opentelemetry.trace import NonRecordingSpan
    if isinstance(span, NonRecordingSpan):
        return
    span.set_attribute("deployment.id", request.deployment_id)
    span.set_attribute("deployment.environment", request.environment)
    span.set_attribute("github.repo", f"{request.owner}/{request.repository}")
    span.set_attribute("github.workflow", request.workflow_id)
    span.set_attribute("github.ref", request.ref)
    if request.incident_id:
        span.set_attribute("incident.id", request.incident_id)
    if request.correlation_id:
        span.set_attribute("correlation.id", request.correlation_id)
