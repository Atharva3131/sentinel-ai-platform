"""ClosedLoopOrchestrator — the final autonomous incident lifecycle.

Completes the closed loop:

  Incident
    ↓ ingestion
  Investigation
    ↓ RCA
  Remediation (GitHub PR + deployment)
    ↓
  VerificationCoordinator
    ├── PASS → RESOLVED
    └── FAIL → Reinvestigation (up to max_cycles)
                  └── Revised remediation + deployment
                        └── PASS → RESOLVED
                        └── max_cycles exceeded → ESCALATED

This orchestrator does NOT:
  * Re-implement ingestion, investigation, or remediation logic.
  * Add new observability providers.
  * Know about specific incident types.
  * Know about specific metrics or services.

All loops are bounded by ``max_reinvestigation_cycles`` to prevent infinite
remediation storms.  When the limit is reached the incident is escalated.

Idempotency:
  * Each cycle gets a unique ``execution_id``.
  * Deployment requests carry idempotency keys derived from
    incident_id + environment + ref + cycle_number.
  * The incident repository is used to detect already-resolved incidents
    before starting a new cycle.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from backend.core.remediation_engine import RemediationEngine
from backend.core.validation_engine import ValidationEngine
from backend.models.deployment import DeploymentResult
from backend.models.incident import Incident, IncidentStatus
from backend.models.resolution import IncidentResolution, ResolutionStatus
from backend.models.validation import VerificationPlan
from backend.services.deployment_pipeline import DeploymentPipeline, build_deployment_request
from backend.services.incident_events import (
    INCIDENT_ESCALATED,
    INCIDENT_FAILED,
    INCIDENT_REINVESTIGATION_REQUIRED,
    INCIDENT_RESOLVED,
    REINVESTIGATION_LIMIT_REACHED,
    REINVESTIGATION_STARTED,
)
from backend.services.incident_repository import IncidentRepository
from backend.services.investigation_orchestrator import InvestigationOrchestrator
from backend.services.verification_coordinator import (
    VerificationCoordinator,
    VerificationOutcome,
    VerificationState,
)

log = structlog.get_logger(__name__)

_DEFAULT_MAX_CYCLES = 3


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClosedLoopResult:
    """Complete output of one ClosedLoopOrchestrator.run() call.

    ``status``         — "resolved", "escalated", "failed", "cancelled"
    ``cycles``         — how many investigation→remediation→verification cycles ran
    ``resolution``     — populated on RESOLVED
    ``last_outcome``   — the final VerificationOutcome
    ``deployment_result`` — the final DeploymentResult (may be None)
    ``duration_ms``    — total wall-clock time
    ``failure_reason`` — why the loop did not resolve
    """

    incident_id: str
    execution_id: str
    status: str
    cycles: int
    last_outcome: VerificationOutcome | None
    deployment_result: DeploymentResult | None
    resolution: IncidentResolution | None
    duration_ms: float
    failure_reason: str | None = None

    @property
    def resolved(self) -> bool:
        return self.status == "resolved"


# ---------------------------------------------------------------------------
# ClosedLoopOrchestrator
# ---------------------------------------------------------------------------


@dataclass
class ClosedLoopOrchestrator:
    """Runs investigation → remediation → deployment → verification in a loop.

    Required:
      ``investigation``      — InvestigationOrchestrator
      ``remediation_engine`` — RemediationEngine
      ``deployment_pipeline``— DeploymentPipeline
      ``verification``       — VerificationCoordinator
      ``repository``         — IncidentRepository

    Optional:
      ``validation_engine``  — ValidationEngine for pre-deployment checks
      ``remediation_planner``— callable(incident, rca) → RemediationPlan
      ``verification_planner_factory`` — callable(incident, cycle) → VerificationPlan
      ``event_emitter``      — IncidentEventEmitter
      ``max_reinvestigation_cycles`` — hard limit (default 3)
      ``deployment_owner``   — GitHub owner for deployment requests
      ``deployment_repo``    — GitHub repository
      ``deployment_workflow``— GitHub Actions workflow file
      ``deployment_environment``— target environment
    """

    investigation: InvestigationOrchestrator
    remediation_engine: RemediationEngine
    deployment_pipeline: DeploymentPipeline
    verification: VerificationCoordinator
    repository: IncidentRepository
    validation_engine: ValidationEngine | None = None
    remediation_planner: Any | None = None
    verification_planner_factory: Any | None = None  # (incident, cycle) → VerificationPlan
    event_emitter: Any | None = None
    max_reinvestigation_cycles: int = _DEFAULT_MAX_CYCLES
    deployment_owner: str = ""
    deployment_repo: str = ""
    deployment_workflow: str = "deploy.yml"
    deployment_environment: str = "staging"

    async def run(
        self,
        incident: Incident,
        *,
        execution_id: str | None = None,
        cancellation_check: Any = None,
        context: dict[str, Any] | None = None,
    ) -> ClosedLoopResult:
        """Drive the incident to resolution or escalation.

        Returns a ClosedLoopResult in all cases — errors are captured as
        ``status="failed"`` rather than raised.
        """
        t0 = time.monotonic()
        execution_id = execution_id or str(uuid.uuid4())
        bound_log = log.bind(
            incident_id=incident.incident_id,
            execution_id=execution_id,
        )

        try:
            bound_log.info(
                "closed_loop_started",
                max_cycles=self.max_reinvestigation_cycles,
            )

            last_outcome: VerificationOutcome | None = None
            last_deployment: DeploymentResult | None = None

            def _duration() -> float:
                return (time.monotonic() - t0) * 1000

            def _resolved(resolution: IncidentResolution) -> ClosedLoopResult:
                return ClosedLoopResult(
                    incident_id=incident.incident_id,
                    execution_id=execution_id,
                    status="resolved",
                    cycles=cycle,
                    last_outcome=last_outcome,
                    deployment_result=last_deployment,
                    resolution=resolution,
                    duration_ms=_duration(),
                )

            def _escalated(reason: str) -> ClosedLoopResult:
                return ClosedLoopResult(
                    incident_id=incident.incident_id,
                    execution_id=execution_id,
                    status="escalated",
                    cycles=cycle,
                    last_outcome=last_outcome,
                    deployment_result=last_deployment,
                    resolution=None,
                    duration_ms=_duration(),
                    failure_reason=reason,
                )

            def _failed(reason: str) -> ClosedLoopResult:
                return ClosedLoopResult(
                    incident_id=incident.incident_id,
                    execution_id=execution_id,
                    status="failed",
                    cycles=cycle,
                    last_outcome=last_outcome,
                    deployment_result=last_deployment,
                    resolution=None,
                    duration_ms=_duration(),
                    failure_reason=reason,
                )

            # ── Idempotency: check if already resolved ────────────────────────
            existing = await self.repository.get(incident.incident_id)
            if existing is not None and existing.status == IncidentStatus.RESOLVED:
                bound_log.info("closed_loop_already_resolved")
                return ClosedLoopResult(
                    incident_id=incident.incident_id,
                    execution_id=execution_id,
                    status="resolved",
                    cycles=0,
                    last_outcome=None,
                    deployment_result=None,
                    resolution=None,
                    duration_ms=_duration(),
                )

            for cycle in range(1, self.max_reinvestigation_cycles + 1):
                # ── Cancellation check ────────────────────────────────────────
                if cancellation_check is not None and cancellation_check():
                    bound_log.warning("closed_loop_cancelled", cycle=cycle)
                    return ClosedLoopResult(
                        incident_id=incident.incident_id,
                        execution_id=execution_id,
                        status="cancelled",
                        cycles=cycle,
                        last_outcome=last_outcome,
                        deployment_result=last_deployment,
                        resolution=None,
                        duration_ms=_duration(),
                    )

                cycle_exec_id = f"{execution_id}-c{cycle}"
                bound_log.info("closed_loop_cycle_started", cycle=cycle)

                # ── Reinvestigation event (cycle > 1) ─────────────────────────
                if cycle > 1:
                    await self._emit(
                        REINVESTIGATION_STARTED,
                        {
                            "incident_id": incident.incident_id,
                            "execution_id": execution_id,
                            "cycle": cycle,
                            "max_cycles": self.max_reinvestigation_cycles,
                        },
                    )

                # ── Investigation ─────────────────────────────────────────────
                investigation_result = await self.investigation.investigate(
                    incident,
                    execution_id=cycle_exec_id,
                    context=context,
                    cancellation_check=cancellation_check,
                )

                if investigation_result.cancelled:
                    return ClosedLoopResult(
                        incident_id=incident.incident_id,
                        execution_id=execution_id,
                        status="cancelled",
                        cycles=cycle,
                        last_outcome=last_outcome,
                        deployment_result=last_deployment,
                        resolution=None,
                        duration_ms=_duration(),
                    )

                rca = investigation_result.rca

                # ── Remediation (if planner and engine are wired) ─────────────
                if self.remediation_planner is not None:
                    try:
                        plan = await self.remediation_planner(incident, rca)
                        await self.remediation_engine.execute_plan(
                            plan, incident,
                            correlation_id=incident.correlation_id,
                            context=context,
                        )
                    except Exception as exc:
                        bound_log.error("remediation_failed", cycle=cycle, error=str(exc))
                        failure_reason = f"Remediation failed on cycle {cycle}: {exc}"
                        
                        # Persist error metadata and ESCALATED status before returning
                        error_metadata = incident.metadata.copy() if incident.metadata else {}
                        error_metadata["closed_loop_failure_stage"] = "remediation"
                        error_metadata["closed_loop_failure_reason"] = str(exc)
                        
                        updated_incident = incident.with_status(IncidentStatus.ESCALATED)
                        from dataclasses import replace as dc_replace
                        updated_incident = dc_replace(updated_incident, metadata=error_metadata)
                        
                        try:
                            await self.repository.save(updated_incident)
                        except Exception as status_exc:
                            bound_log.exception(
                                "remediation_failure_status_persistence_failed",
                                status_error=str(status_exc),
                            )
                        
                        return _failed(failure_reason)

                # ── Deployment ────────────────────────────────────────────────
                deploy_req = build_deployment_request(
                    owner=self.deployment_owner,
                    repository=self.deployment_repo,
                    workflow_id=self.deployment_workflow,
                    ref=context.get("ref", "main") if context else "main",
                    environment=self.deployment_environment,
                    incident_id=incident.incident_id,
                    correlation_id=incident.correlation_id,
                    deployment_id=(
                        f"deploy-{incident.incident_id[:8]}-c{cycle}"
                    ),
                )
                # Override idempotency_key per cycle to allow retry
                from dataclasses import replace as dc_replace
                deploy_req = dc_replace(
                    deploy_req,
                    idempotency_key=(
                        f"cl-{incident.incident_id}-{self.deployment_environment}-c{cycle}"
                    ),
                )

                last_deployment = await self.deployment_pipeline.deploy(
                    deploy_req, incident,
                    cancellation_check=cancellation_check,
                    context=context,
                )

                if not last_deployment.succeeded:
                    bound_log.warning(
                        "closed_loop_deployment_failed",
                        cycle=cycle,
                        error=last_deployment.error,
                    )
                    await self._emit(
                        INCIDENT_FAILED,
                        {
                            "incident_id": incident.incident_id,
                            "execution_id": execution_id,
                            "phase": "deployment",
                            "cycle": cycle,
                            "error": last_deployment.error,
                        },
                    )
                    failure_reason = f"Deployment failed on cycle {cycle}: {last_deployment.error}"
                    
                    # Persist error metadata and ESCALATED status before returning
                    error_metadata = incident.metadata.copy() if incident.metadata else {}
                    error_metadata["closed_loop_failure_stage"] = "deployment"
                    error_metadata["closed_loop_failure_reason"] = last_deployment.error or ""
                    
                    updated_incident = incident.with_status(IncidentStatus.ESCALATED)
                    from dataclasses import replace as dc_replace
                    updated_incident = dc_replace(updated_incident, metadata=error_metadata)
                    
                    try:
                        await self.repository.save(updated_incident)
                    except Exception as status_exc:
                        bound_log.exception(
                            "deployment_failure_status_persistence_failed",
                            status_error=str(status_exc),
                        )
                    
                    return _failed(failure_reason)

                # ── Verification ──────────────────────────────────────────────
                ver_plan = (
                    await self.verification_planner_factory(incident, cycle)
                    if self.verification_planner_factory
                    else _default_verification_plan(incident)
                )

                last_outcome = await self.verification.verify(
                    incident,
                    ver_plan,
                    last_deployment,
                    execution_id=cycle_exec_id,
                    cancellation_check=cancellation_check,
                    context=context,
                )

                if last_outcome.state == VerificationState.CANCELLED:
                    return ClosedLoopResult(
                        incident_id=incident.incident_id,
                        execution_id=execution_id,
                        status="cancelled",
                        cycles=cycle,
                        last_outcome=last_outcome,
                        deployment_result=last_deployment,
                        resolution=None,
                        duration_ms=_duration(),
                    )

                # ── PASS → resolve ────────────────────────────────────────────
                if last_outcome.passed:
                    resolution = await self._resolve(
                        incident, execution_id, cycle, last_outcome, _duration()
                    )
                    bound_log.info(
                        "closed_loop_resolved",
                        cycle=cycle,
                        duration_ms=round(_duration(), 2),
                    )
                    return _resolved(resolution)

                # ── FAIL → prepare next reinvestigation cycle ─────────────────
                bound_log.warning(
                    "closed_loop_verification_failed",
                    cycle=cycle,
                    reason=last_outcome.failure_reason,
                )
                await self._emit(
                    INCIDENT_REINVESTIGATION_REQUIRED,
                    {
                        "incident_id": incident.incident_id,
                        "execution_id": execution_id,
                        "cycle": cycle,
                        "reason": last_outcome.failure_reason,
                        "max_cycles": self.max_reinvestigation_cycles,
                    },
                )

                if cycle >= self.max_reinvestigation_cycles:
                    bound_log.error(
                        "closed_loop_max_cycles_reached",
                        cycles=cycle,
                    )
                    await self._emit(
                        REINVESTIGATION_LIMIT_REACHED,
                        {
                            "incident_id": incident.incident_id,
                            "execution_id": execution_id,
                            "cycles": cycle,
                        },
                    )
                    await self._emit(
                        INCIDENT_ESCALATED,
                        {
                            "incident_id": incident.incident_id,
                            "execution_id": execution_id,
                            "reason": "max_reinvestigation_cycles_reached",
                        },
                    )
                    await self._update_status(incident, IncidentStatus.ESCALATED)
                    return _escalated(
                        f"Max reinvestigation cycles ({self.max_reinvestigation_cycles}) reached"
                        f" without verification passing."
                    )

            # Should not reach here; belt-and-suspenders
            return _escalated("Loop exited without resolution.")

        except Exception as exc:
            bound_log.exception(
                "closed_loop_orchestration_unexpected_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            
            # Persist error details to incident metadata before escalating
            error_metadata = incident.metadata.copy() if incident.metadata else {}
            error_metadata["closed_loop_error"] = str(exc)
            error_metadata["closed_loop_error_type"] = type(exc).__name__
            
            # Update incident with error metadata and ESCALATED status
            updated_incident = incident.with_status(IncidentStatus.ESCALATED)
            from dataclasses import replace as dc_replace_incident
            updated_incident = dc_replace_incident(updated_incident, metadata=error_metadata)
            
            try:
                await self.repository.save(updated_incident)
            except Exception as status_exc:
                bound_log.exception(
                    "closed_loop_escalated_status_persistence_failed",
                    status_error=str(status_exc),
                )
            
            # Return failed result with exception details
            return ClosedLoopResult(
                incident_id=incident.incident_id,
                execution_id=execution_id,
                status="failed",
                cycles=0,
                last_outcome=None,
                deployment_result=None,
                resolution=None,
                duration_ms=(time.monotonic() - t0) * 1000,
                failure_reason=f"{type(exc).__name__}: {exc}",
            )

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _resolve(
        self,
        incident: Incident,
        execution_id: str,
        cycle: int,
        outcome: VerificationOutcome,
        duration_ms: float,
    ) -> IncidentResolution:
        resolution = IncidentResolution(
            resolution_id=str(uuid.uuid4()),
            incident_id=incident.incident_id,
            status=ResolutionStatus.RESOLVED,
            resolved_at=datetime.now(UTC),
            duration_ms=duration_ms,
            execution_id=execution_id,
            verification_result=(
                outcome.verification_result if outcome else None
            ),
            summary=(
                f"Incident {incident.incident_id} resolved after {cycle} cycle(s) "
                f"in {duration_ms:.0f}ms."
            ),
            metadata={
                "cycles": cycle,
                "deployment_id": outcome.deployment_id,
                "verification_state": outcome.state.value,
            },
        )
        await self._update_status(incident, IncidentStatus.RESOLVED)
        await self._emit(
            INCIDENT_RESOLVED,
            {
                "incident_id": incident.incident_id,
                "execution_id": execution_id,
                "resolution_id": resolution.resolution_id,
                "cycles": cycle,
                "duration_ms": duration_ms,
            },
        )
        try:
            updated = incident.with_status(IncidentStatus.RESOLVED)
            await self.repository.save(updated)
        except Exception as exc:
            log.warning(
                "resolution_persistence_failed",
                incident_id=incident.incident_id,
                error=str(exc),
            )
        return resolution

    async def _update_status(self, incident: Incident, status: IncidentStatus) -> None:
        try:
            updated = incident.with_status(status)
            await self.repository.save(updated)
        except Exception as exc:
            log.warning(
                "status_update_failed",
                incident_id=incident.incident_id,
                status=str(status),
                error=str(exc),
            )

    async def _emit(self, event_name: str, payload: Mapping[str, Any]) -> None:
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter.emit(event_name, payload, None)
        except Exception as exc:
            log.warning("closed_loop_event_failed", event_name=event_name, error=str(exc))


# ---------------------------------------------------------------------------
# Default verification plan factory
# ---------------------------------------------------------------------------


def _default_verification_plan(incident: Incident) -> VerificationPlan:
    """Produce a minimal VerificationPlan with generic before/after metrics.

    Used when no verification_planner_factory is injected.  The plan compares
    a generic ``error_rate`` metric directionally (lower is better).
    Callers may inject their own factory to use service-specific metrics.
    """
    return VerificationPlan(
        plan_id=str(uuid.uuid4()),
        incident_id=incident.incident_id,
        remediation_plan_id="",
        metrics_to_compare=("error_rate",),
        comparison_window_seconds=60.0,
        thresholds={"error_rate": 0.05},
        snapshot_before={
            svc: {"error_rate": 1.0}   # assume degraded before
            for svc in incident.affected_services
        },
    )
