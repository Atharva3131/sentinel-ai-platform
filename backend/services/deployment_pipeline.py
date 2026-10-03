"""DeploymentPipeline — connects PR validation to deployment execution.

Lifecycle:
  1. Receive a ``DeploymentRequest`` (built from the remediation PR output).
  2. Evaluate against ``ActionPolicy`` / ``RemediationPolicyEngine``.
  3. On approval: call ``DeploymentProvider.trigger()``.
  4. Poll via ``DeploymentProvider.wait_for_completion()`` until terminal.
  5. Emit lifecycle events at each transition.
  6. Return a ``DeploymentResult``.

Policy contract:
  * Deployment actions carry ``ActionRiskLevel.HIGH`` by default (requires
    explicit approval unless the policy engine is configured otherwise).
  * ``ActionPolicy.protected_environments`` prevents deployments to
    protected targets (e.g. "production") without override.
  * Idempotency keys prevent duplicate deployment triggers.
  * Every policy decision is recorded in ``PolicyDecisionRecord``.

The pipeline does NOT:
  * Merge PRs autonomously.
  * Modify configuration files.
  * Interact with GitHub directly (that is the provider's responsibility).
  * Bypass the existing ``ActionPolicy``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from backend.interfaces.deployment import DeploymentProvider
from backend.models.deployment import (
    DeploymentRequest,
    DeploymentResult,
    DeploymentState,
    DeploymentStatus,
)
from backend.models.incident import Incident
from backend.models.remediation import (
    ActionRiskLevel,
    RemediationAction,
    RemediationActionType,
)
from backend.policies.action_policy import RemediationPolicyEngine

log = structlog.get_logger(__name__)

# Events emitted by the pipeline
DEPLOYMENT_POLICY_EVALUATED = "Incident.Deployment.PolicyEvaluated"
DEPLOYMENT_TRIGGERED = "Incident.Deployment.Triggered"
DEPLOYMENT_RUNNING = "Incident.Deployment.Running"
DEPLOYMENT_SUCCEEDED = "Incident.Deployment.Succeeded"
DEPLOYMENT_FAILED = "Incident.Deployment.Failed"
DEPLOYMENT_CANCELLED = "Incident.Deployment.Cancelled"
DEPLOYMENT_TIMED_OUT = "Incident.Deployment.TimedOut"


@dataclass
class DeploymentPipeline:
    """Orchestrates policy evaluation and deployment execution.

    Inject:
      ``provider``       — DeploymentProvider (GitHub Actions or fake)
      ``policy_engine``  — RemediationPolicyEngine
      ``event_emitter``  — optional IncidentEventEmitter
    """

    provider: DeploymentProvider
    policy_engine: RemediationPolicyEngine | None = None
    event_emitter: Any | None = None

    async def deploy(
        self,
        request: DeploymentRequest,
        incident: Incident,
        *,
        cancellation_check: Any = None,
        context: dict[str, Any] | None = None,
    ) -> DeploymentResult:
        """Evaluate policy, trigger, and wait for deployment completion.

        Returns:
            DeploymentResult — always returned; never raises for policy
            rejections or provider errors.  Check ``result.succeeded``.
        """
        bound_log = log.bind(
            deployment_id=request.deployment_id,
            incident_id=incident.incident_id,
            environment=request.environment,
            workflow=request.workflow_id,
        )

        # ── Policy evaluation ─────────────────────────────────────────────
        action = _build_remediation_action(request, incident)

        if self.policy_engine is not None:
            decision = await self.policy_engine.evaluate(action, incident)
            await self._emit(
                DEPLOYMENT_POLICY_EVALUATED,
                {
                    "incident_id": incident.incident_id,
                    "deployment_id": request.deployment_id,
                    "allowed": decision["allowed"],
                    "reason": decision["reason"],
                    "environment": request.environment,
                },
            )
            if not decision["allowed"]:
                bound_log.warning(
                    "deployment_policy_rejected", reason=decision["reason"]
                )
                return DeploymentResult(
                    request=request,
                    status=DeploymentStatus(
                        deployment_id=request.deployment_id,
                        run_id=None,
                        state=DeploymentState.FAILED,
                        environment=request.environment,
                    ),
                    error=f"Policy rejected: {decision['reason']}",
                    total_duration_ms=0.0,
                )

        # ── Trigger ───────────────────────────────────────────────────────
        bound_log.info("deployment_triggering")
        try:
            initial_status = await self.provider.trigger(request)
        except Exception as exc:
            bound_log.error("deployment_trigger_failed", error=str(exc))
            return DeploymentResult(
                request=request,
                status=DeploymentStatus(
                    deployment_id=request.deployment_id,
                    run_id=None,
                    state=DeploymentState.FAILED,
                    environment=request.environment,
                ),
                error=str(exc),
                total_duration_ms=0.0,
            )

        await self._emit(
            DEPLOYMENT_TRIGGERED,
            {
                "incident_id": incident.incident_id,
                "deployment_id": request.deployment_id,
                "run_id": initial_status.run_id,
                "environment": request.environment,
                "ref": request.ref,
            },
        )
        bound_log.info(
            "deployment_triggered",
            run_id=initial_status.run_id,
            state=initial_status.state.value,
        )

        # ── Wait for completion ────────────────────────────────────────────
        # Only providers that support wait_for_completion use it; otherwise
        # we do a single status check.
        if hasattr(self.provider, "wait_for_completion"):
            wait_fn = self.provider.wait_for_completion
            raw_result = await wait_fn(
                request,
                initial_status.run_id or "",
                cancellation_check=cancellation_check,
            )
            result: DeploymentResult = raw_result
        else:
            # No polling support — do a single status check to get the final state
            run_id = initial_status.run_id or ""
            if run_id:
                try:
                    final_status = await self.provider.get_status(request, run_id)
                except Exception:
                    final_status = initial_status
            else:
                final_status = initial_status
            result = DeploymentResult(
                request=request,
                status=final_status,
            )

        # ── Emit terminal event ───────────────────────────────────────────
        terminal_event = _state_to_event(result.status.state)
        await self._emit(
            terminal_event,
            {
                "incident_id": incident.incident_id,
                "deployment_id": request.deployment_id,
                "run_id": result.status.run_id,
                "state": result.status.state.value,
                "environment": request.environment,
                "duration_ms": result.total_duration_ms,
                "error": result.error,
            },
        )
        bound_log.info(
            "deployment_complete",
            state=result.status.state.value,
            duration_ms=round(result.total_duration_ms, 2),
            succeeded=result.succeeded,
        )
        return result

    async def _emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
    ) -> None:
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter.emit(event_name, payload, None)
        except Exception as exc:
            log.warning("deployment_event_emission_failed", event=event_name, error=str(exc))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_remediation_action(
    request: DeploymentRequest,
    incident: Incident,
) -> RemediationAction:
    """Wrap a DeploymentRequest as a RemediationAction for policy evaluation."""
    return RemediationAction(
        action_id=str(uuid.uuid4()),
        action_type=RemediationActionType.CUSTOM,
        target_service=f"{request.owner}/{request.repository}",
        target_environment=request.environment,
        risk_level=ActionRiskLevel.HIGH,
        title=f"Deploy {request.workflow_id} to {request.environment}",
        description=(
            f"Trigger GitHub Actions workflow '{request.workflow_id}' "
            f"on ref '{request.ref}' to environment '{request.environment}'."
        ),
        parameters={
            "owner": request.owner,
            "repo": request.repository,
            "workflow": request.workflow_id,
            "ref": request.ref,
            "environment": request.environment,
        },
        metadata={
            "github_operation": "deployment_trigger",
            "deployment_id": request.deployment_id,
            "incident_id": incident.incident_id,
            "correlation_id": incident.correlation_id or "",
        },
        idempotency_key=request.idempotency_key,
        timeout_seconds=300.0,
        requires_validation=True,
    )


def _state_to_event(state: DeploymentState) -> str:
    return {
        DeploymentState.SUCCEEDED: DEPLOYMENT_SUCCEEDED,
        DeploymentState.FAILED:    DEPLOYMENT_FAILED,
        DeploymentState.CANCELLED: DEPLOYMENT_CANCELLED,
        DeploymentState.TIMED_OUT: DEPLOYMENT_TIMED_OUT,
    }.get(state, DEPLOYMENT_FAILED)


def build_deployment_request(
    *,
    owner: str,
    repository: str,
    workflow_id: str,
    ref: str,
    environment: str,
    incident_id: str | None = None,
    correlation_id: str | None = None,
    parameters: dict[str, str] | None = None,
    deployment_id: str | None = None,
) -> DeploymentRequest:
    """Convenience factory for building a DeploymentRequest."""
    return DeploymentRequest(
        deployment_id=deployment_id or str(uuid.uuid4()),
        owner=owner,
        repository=repository,
        workflow_id=workflow_id,
        environment=environment,
        ref=ref,
        parameters=parameters or {},
        incident_id=incident_id,
        correlation_id=correlation_id,
        idempotency_key=f"deploy-{incident_id or 'unknown'}-{environment}-{ref[:8]}",
    )
