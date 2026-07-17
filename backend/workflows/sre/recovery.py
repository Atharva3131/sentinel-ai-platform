"""RecoveryCoordinator — gates and orchestrates remediation actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.workflows.sre.exceptions import SREPhaseError
from backend.workflows.sre.models import (
    AnalysisResult,
    ApprovalDecision,
    ApprovalOutcome,
    ExecutionPlan,
    IncidentContext,
    IncidentSeverity,
    RecoveryAction,
    RecoveryResult,
)
from backend.workflows.sre.ports import RecoveryExecutor

_ALLOWED_ACTION_TYPES: frozenset[str] = frozenset(
    {"restart_service", "rollback_deployment", "scale_up", "clear_cache", "drain_traffic"}
)

_SEVERITY_ACTION_MAP: dict[IncidentSeverity, list[str]] = {
    IncidentSeverity.CRITICAL: ["restart_service", "rollback_deployment"],
    IncidentSeverity.HIGH: ["restart_service", "clear_cache"],
    IncidentSeverity.MEDIUM: ["clear_cache"],
    IncidentSeverity.LOW: ["clear_cache"],
}


@dataclass(slots=True)
class RecoveryCoordinator:
    """Determines and executes recovery actions for an incident.

    Approval gate:
        When ``approval`` is not None and the decision is not ``APPROVED``,
        recovery is skipped and a zero-action result is returned.

    Action derivation:
        Actions are derived deterministically from the incident severity and
        the affected service scope. The ``RecoveryExecutor`` is responsible for
        idempotency; this coordinator does not retry individual actions.

    Raises:
        SREPhaseError: If the executor raises an unexpected error.
    """

    executor: RecoveryExecutor

    async def run(
        self,
        incident: IncidentContext,
        plan: ExecutionPlan,
        analysis: AnalysisResult,
        approval: ApprovalOutcome | None,
        *,
        correlation_id: str | None = None,
    ) -> RecoveryResult:
        """Execute recovery for *incident*.

        Returns:
            A ``RecoveryResult`` summarising what was attempted and succeeded.

        Raises:
            SREPhaseError: If the executor raises an unexpected error.
        """
        if approval is not None and approval.decision != ApprovalDecision.APPROVED:
            return RecoveryResult(
                incident_id=incident.incident_id,
                actions_attempted=0,
                actions_succeeded=0,
                actions_failed=0,
                recovered=False,
                duration_ms=0.0,
                metadata={"skipped_reason": str(approval.decision)},
            )

        actions = self._derive_actions(incident, analysis)
        if not actions:
            return RecoveryResult(
                incident_id=incident.incident_id,
                actions_attempted=0,
                actions_succeeded=0,
                actions_failed=0,
                recovered=True,
                duration_ms=0.0,
                metadata={"reason": "no_recovery_actions_needed"},
            )

        try:
            return await self.executor.execute_actions(
                incident,
                actions,
                correlation_id=correlation_id,
            )
        except Exception as exc:
            raise SREPhaseError(
                f"Recovery executor failed for incident '{incident.incident_id}': {exc}",
                phase="recovery",
                incident_id=incident.incident_id,
                retryable=False,
            ) from exc

    def _derive_actions(
        self,
        incident: IncidentContext,
        analysis: AnalysisResult,
    ) -> list[RecoveryAction]:
        action_types = _SEVERITY_ACTION_MAP.get(incident.severity, ["clear_cache"])
        actions: list[RecoveryAction] = []
        for idx, service in enumerate(analysis.affected_scope):
            for action_type in action_types:
                if action_type not in _ALLOWED_ACTION_TYPES:
                    continue
                actions.append(
                    RecoveryAction(
                        action_id=f"action-{idx}-{action_type}",
                        name=f"{action_type.replace('_', ' ').title()} {service}",
                        target_service=service,
                        action_type=action_type,
                        parameters=self._action_params(action_type, service, incident),
                    )
                )
        return actions

    def _action_params(
        self,
        action_type: str,
        service: str,
        incident: IncidentContext,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {"service": service, "incident_id": incident.incident_id}
        if action_type == "restart_service":
            return {**base, "graceful": True, "timeout_seconds": 30}
        if action_type == "rollback_deployment":
            return {**base, "steps": 1}
        if action_type == "scale_up":
            return {**base, "replicas_delta": 2}
        return base
