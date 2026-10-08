"""IncidentOrchestrationPipeline — top-level coordinator for the full lifecycle.

Pipeline stages (all optional beyond ingestion):

    IncidentIngestionService      → validate / persist / dedup / emit Created
            ↓
    InvestigationOrchestrator     → evidence → hypotheses → RCA (iterative)
            ↓  (if rca.has_root_cause OR run_remediation_on_inconclusive)
    RemediationEngine             → policy-gate → execute plan → rollback
            ↓
    ValidationEngine              → post-remediation health checks
            ↓
    VerificationEngine            → before/after metric comparison
            ↓
    IncidentRepository.save()     → persist terminal status
            ↓
    IncidentResolved / Failed event

Every stage emits structured lifecycle events via the injected event emitter.
All context IDs (incident_id, execution_id, correlation_id) are threaded through
every log statement and OTel span.

The pipeline is intentionally composable — callers can skip stages by not
injecting the corresponding engine.  For example, a test can inject only the
ingestion service and investigation orchestrator and skip remediation entirely.

No hard-coded failure modes, services, or cloud resources.
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
from backend.core.validation_engine import ValidationEngine, VerificationEngine
from backend.models.hypothesis import RootCauseAnalysis
from backend.models.incident import Incident, IncidentStatus
from backend.models.remediation import RemediationPlan, RemediationResult
from backend.models.resolution import IncidentResolution, ResolutionStatus
from backend.models.validation import ValidationPlan, VerificationPlan
from backend.services.incident_events import (
    INCIDENT_CANCELLED,
    INCIDENT_ESCALATED,
    INCIDENT_FAILED,
    INCIDENT_REINVESTIGATION_REQUIRED,
    INCIDENT_RESOLVED,
    POLICY_EVALUATED,
    REMEDIATION_COMPLETED,
    REMEDIATION_PLANNED,
    REMEDIATION_STARTED,
    VALIDATION_COMPLETED,
    VALIDATION_STARTED,
    VERIFICATION_COMPLETED,
    VERIFICATION_STARTED,
)
from backend.services.incident_ingestion import IncidentIngestionService, IngestionResult
from backend.services.incident_observability import IncidentTracer
from backend.services.incident_repository import IncidentRepository
from backend.services.investigation_orchestrator import (
    InvestigationOrchestrator,
    InvestigationResult,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Ports — thin typing shims so mypy is happy with structural usage
# ---------------------------------------------------------------------------


class _EventEmitter:
    """Type alias comment — the actual type is IncidentEventEmitter (structural)."""


# ---------------------------------------------------------------------------
# Pipeline result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Complete output of one IncidentOrchestrationPipeline.run() call.

    Every field except ``incident_id``, ``status``, and ``execution_id`` is
    optional because earlier stages may not have been reached.
    """

    incident_id: str
    execution_id: str | None
    status: str                                   # "resolved","failed","cancelled", etc.
    ingestion: IngestionResult | None
    investigation: InvestigationResult | None
    resolution: IncidentResolution | None
    remediation_result: RemediationResult | None
    duration_ms: float
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# IncidentOrchestrationPipeline
# ---------------------------------------------------------------------------


@dataclass
class IncidentOrchestrationPipeline:
    """Top-level incident response pipeline.

    Required:
      ``ingestion_service``       — IncidentIngestionService
      ``investigation``           — InvestigationOrchestrator
      ``repository``              — IncidentRepository (for status updates)

    Optional (skipped when None):
      ``remediation_engine``      — RemediationEngine
      ``validation_engine``       — ValidationEngine
      ``verification_engine``     — VerificationEngine
      ``remediation_planner``     — callable(incident, rca) → RemediationPlan
      ``validation_planner``      — callable(incident, remediation_plan_id) → ValidationPlan
      ``verification_planner``    — callable(incident, plan_id, snapshot) →
                                   VerificationPlan
      ``event_emitter``           — IncidentEventEmitter
      ``tracer``                  — IncidentTracer (no-op when None)
      ``run_remediation_on_inconclusive`` — attempt remediation even without RCA confidence

    The pipeline is safe to call concurrently; every run gets its own
    execution_id.
    """

    ingestion_service: IncidentIngestionService
    investigation: InvestigationOrchestrator
    repository: IncidentRepository
    remediation_engine: RemediationEngine | None = None
    validation_engine: ValidationEngine | None = None
    verification_engine: VerificationEngine | None = None
    # Planners: callables that produce plans from context
    remediation_planner: Any | None = None   # (Incident, RCA | None) → RemediationPlan
    validation_planner: Any | None = None    # (Incident, str) → ValidationPlan
    verification_planner: Any | None = None  # (Incident, str, dict) → VerificationPlan
    event_emitter: Any | None = None         # IncidentEventEmitter
    tracer: IncidentTracer | None = None
    run_remediation_on_inconclusive: bool = False

    async def run(
        self,
        incident: Incident,
        *,
        execution_id: str | None = None,
        context: dict[str, Any] | None = None,
        cancellation_check: Any = None,    # () → bool
        emitter_context: Any = None,
    ) -> PipelineResult:
        """Execute the full pipeline for *incident*.

        Returns a PipelineResult in all cases — pipeline failures are captured
        as ``status="failed"`` with a ``failure_reason``, not raised as
        exceptions.  Only programmer errors (type errors, etc.) propagate.
        """
        execution_id = execution_id or str(uuid.uuid4())
        t0 = time.monotonic()

        _tracer = self.tracer or IncidentTracer()
        bound_log = log.bind(
            incident_id=incident.incident_id,
            execution_id=execution_id,
            correlation_id=incident.correlation_id,
        )
        bound_log.info("pipeline_started")

        ingestion_result: IngestionResult | None = None
        investigation_result: InvestigationResult | None = None
        remediation_result: RemediationResult | None = None
        resolution: IncidentResolution | None = None

        def _duration() -> float:
            return (time.monotonic() - t0) * 1000

        def _result(status: str, reason: str | None = None) -> PipelineResult:
            return PipelineResult(
                incident_id=incident.incident_id,
                execution_id=execution_id,
                status=status,
                ingestion=ingestion_result,
                investigation=investigation_result,
                resolution=resolution,
                remediation_result=remediation_result,
                duration_ms=_duration(),
                failure_reason=reason,
            )

        # ── Stage 1: Ingestion ────────────────────────────────────────────
        with _tracer.ingestion_span(
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
        ):
            try:
                ingestion_result = await self.ingestion_service.ingest(
                    incident, emitter_context=emitter_context
                )
            except Exception as exc:
                bound_log.error("ingestion_failed", error=str(exc))
                await self._emit(
                    INCIDENT_FAILED,
                    {"incident_id": incident.incident_id, "phase": "ingestion", "error": str(exc)},
                    emitter_context,
                )
                return _result("failed", str(exc))

        if ingestion_result.is_duplicate:
            bound_log.info(
                "pipeline_skipped_duplicate",
                existing_id=ingestion_result.existing.incident_id
                if ingestion_result.existing
                else None,
            )
            return _result("duplicate")

        # Update local reference to the normalised incident
        incident = ingestion_result.incident

        # ── Cancellation gate ─────────────────────────────────────────────
        if cancellation_check is not None and cancellation_check():
            await self._update_status(incident, IncidentStatus.CANCELLED)
            await self._emit(
                INCIDENT_CANCELLED,
                {"incident_id": incident.incident_id, "phase": "post_ingestion"},
                emitter_context,
            )
            return _result("cancelled")

        # ── Stage 2: Investigation ────────────────────────────────────────
        await self._update_status(incident, IncidentStatus.INVESTIGATING)

        with _tracer.investigation_span(
            incident_id=incident.incident_id,
            execution_id=execution_id,
            correlation_id=incident.correlation_id,
        ):
            try:
                investigation_result = await self.investigation.investigate(
                    incident,
                    execution_id=execution_id,
                    context=context,
                    cancellation_check=cancellation_check,
                    emitter_context=emitter_context,
                )
            except Exception as exc:
                bound_log.error("investigation_failed", error=str(exc))
                await self._update_status(incident, IncidentStatus.ESCALATED)
                await self._emit(
                    INCIDENT_FAILED,
                    {
                        "incident_id": incident.incident_id,
                        "phase": "investigation",
                        "error": str(exc),
                    },
                    emitter_context,
                )
                return _result("failed", str(exc))

        if investigation_result.cancelled:
            await self._update_status(incident, IncidentStatus.CANCELLED)
            await self._emit(
                INCIDENT_CANCELLED,
                {"incident_id": incident.incident_id, "phase": "investigation"},
                emitter_context,
            )
            return _result("cancelled")

        if investigation_result.timed_out:
            await self._update_status(incident, IncidentStatus.ESCALATED)
            await self._emit(
                INCIDENT_ESCALATED,
                {"incident_id": incident.incident_id, "phase": "investigation",
                 "reason": "timeout"},
                emitter_context,
            )
            return _result("timed_out", "Investigation timed out")

        _tracer.record_rca_outcome(
            incident_id=incident.incident_id,
            execution_id=execution_id,
            confidence=investigation_result.rca.confidence,
            has_root_cause=investigation_result.rca.has_root_cause,
            iterations=investigation_result.iterations,
            duration_ms=investigation_result.duration_ms,
        )

        # Decide whether to proceed to remediation
        rca = investigation_result.rca
        should_remediate = (
            rca.has_root_cause
            or (self.run_remediation_on_inconclusive and investigation_result.inconclusive)
        )

        if (
            not should_remediate
            or self.remediation_engine is None
            or self.remediation_planner is None
        ):
            # No remediation — escalate if inconclusive, otherwise resolve
            if investigation_result.inconclusive:
                await self._update_status(incident, IncidentStatus.ESCALATED)
                await self._emit(
                    INCIDENT_REINVESTIGATION_REQUIRED,
                    {
                        "incident_id": incident.incident_id,
                        "confidence": rca.confidence,
                        "reason": rca.unresolved_uncertainty,
                    },
                    emitter_context,
                )
                resolution = self._build_resolution(
                    incident,
                    execution_id,
                    ResolutionStatus.ESCALATED,
                    rca=rca,
                    duration_ms=_duration(),
                )
                return _result("escalated")
            else:
                # RCA found but no remediation engine — mark resolved (investigation only)
                resolution = self._build_resolution(
                    incident,
                    execution_id,
                    ResolutionStatus.RESOLVED,
                    rca=rca,
                    duration_ms=_duration(),
                )
                await self._update_status(incident, IncidentStatus.RESOLVED)
                await self._emit(
                    INCIDENT_RESOLVED,
                    {
                        "incident_id": incident.incident_id,
                        "resolution_id": resolution.resolution_id,
                        "confidence": rca.confidence,
                    },
                    emitter_context,
                )
                _tracer.record_pipeline_completed(
                    incident_id=incident.incident_id,
                    execution_id=execution_id,
                    status="resolved",
                    duration_ms=_duration(),
                )
                return _result("resolved")

        # ── Stage 3: Remediation ──────────────────────────────────────────
        await self._update_status(incident, IncidentStatus.REMEDIATING)

        with _tracer.remediation_span(
            incident_id=incident.incident_id,
            execution_id=execution_id,
        ):
            try:
                plan: RemediationPlan = await self.remediation_planner(incident, rca)
            except Exception as exc:
                bound_log.error("remediation_planning_failed", error=str(exc))
                await self._update_status(incident, IncidentStatus.ESCALATED)
                await self._emit(
                    INCIDENT_FAILED,
                    {
                        "incident_id": incident.incident_id,
                        "phase": "remediation_planning",
                        "error": str(exc),
                    },
                    emitter_context,
                )
                return _result("failed", str(exc))

            await self._emit(
                REMEDIATION_PLANNED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "plan_id": plan.plan_id,
                    "action_count": plan.action_count,
                },
                emitter_context,
            )
            await self._emit(
                POLICY_EVALUATED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "plan_id": plan.plan_id,
                },
                emitter_context,
            )
            await self._emit(
                REMEDIATION_STARTED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "plan_id": plan.plan_id,
                },
                emitter_context,
            )

            try:
                remediation_result = await self.remediation_engine.execute_plan(
                    plan,
                    incident,
                    correlation_id=incident.correlation_id,
                    context=context,
                )
            except Exception as exc:
                bound_log.error("remediation_execution_failed", error=str(exc))
                await self._emit(
                    INCIDENT_FAILED,
                    {
                        "incident_id": incident.incident_id,
                        "phase": "remediation",
                        "error": str(exc),
                    },
                    emitter_context,
                )
                return _result("failed", str(exc))

            await self._emit(
                REMEDIATION_COMPLETED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "plan_id": plan.plan_id,
                    "succeeded": remediation_result.succeeded,
                    "actions_attempted": remediation_result.actions_attempted,
                    "actions_succeeded": remediation_result.actions_succeeded,
                    "actions_failed": remediation_result.actions_failed,
                },
                emitter_context,
            )

        # ── Stage 4: Validation ───────────────────────────────────────────
        validation_passed = True
        if self.validation_engine is not None and self.validation_planner is not None:
            await self._update_status(incident, IncidentStatus.VALIDATING)
            with _tracer.validation_span(
                incident_id=incident.incident_id,
                execution_id=execution_id,
            ):
                await self._emit(
                    VALIDATION_STARTED,
                    {"incident_id": incident.incident_id, "execution_id": execution_id},
                    emitter_context,
                )
                try:
                    val_plan: ValidationPlan = await self.validation_planner(
                        incident, plan.plan_id
                    )
                    val_results = await self.validation_engine.validate(
                        val_plan, incident, context=context
                    )
                    validation_passed = self.validation_engine.passed(val_results)
                except Exception as exc:
                    bound_log.warning("validation_error", error=str(exc))
                    validation_passed = False

                await self._emit(
                    VALIDATION_COMPLETED,
                    {
                        "incident_id": incident.incident_id,
                        "execution_id": execution_id,
                        "passed": validation_passed,
                    },
                    emitter_context,
                )

            if not validation_passed:
                await self._emit(
                    INCIDENT_REINVESTIGATION_REQUIRED,
                    {
                        "incident_id": incident.incident_id,
                        "reason": "post-remediation validation failed",
                    },
                    emitter_context,
                )
                resolution = self._build_resolution(
                    incident,
                    execution_id,
                    ResolutionStatus.FAILED,
                    rca=rca,
                    remediation_result=remediation_result,
                    duration_ms=_duration(),
                    summary="Post-remediation validation failed.",
                )
                await self._update_status(incident, IncidentStatus.ESCALATED)
                return _result("reinvestigation_required", "Validation failed")

        # ── Stage 5: Verification ─────────────────────────────────────────
        verification_passed = True
        verification_result = None
        if self.verification_engine is not None and self.verification_planner is not None:
            with _tracer.verification_span(
                incident_id=incident.incident_id,
                execution_id=execution_id,
            ):
                await self._emit(
                    VERIFICATION_STARTED,
                    {"incident_id": incident.incident_id, "execution_id": execution_id},
                    emitter_context,
                )
                try:
                    ver_plan: VerificationPlan = await self.verification_planner(
                        incident, plan.plan_id, {}
                    )
                    verification_result = await self.verification_engine.verify(
                        ver_plan, incident, context=context
                    )
                    verification_passed = verification_result.passed
                except Exception as exc:
                    bound_log.warning("verification_error", error=str(exc))
                    verification_passed = False

                await self._emit(
                    VERIFICATION_COMPLETED,
                    {
                        "incident_id": incident.incident_id,
                        "execution_id": execution_id,
                        "passed": verification_passed,
                    },
                    emitter_context,
                )

        # ── Resolution ────────────────────────────────────────────────────
        if (
            remediation_result
            and remediation_result.succeeded
            and validation_passed
            and verification_passed
        ):
            final_status = ResolutionStatus.RESOLVED
            pipeline_status = "resolved"
        elif remediation_result and not remediation_result.succeeded:
            final_status = ResolutionStatus.FAILED
            pipeline_status = "failed"
        else:
            # Remediation succeeded but validation/verification had issues
            final_status = ResolutionStatus.MITIGATED
            pipeline_status = "mitigated"

        resolution = self._build_resolution(
            incident,
            execution_id,
            final_status,
            rca=rca,
            remediation_result=remediation_result,
            verification_result=verification_result,
            duration_ms=_duration(),
        )
        await self._update_status(
            incident,
            IncidentStatus.RESOLVED if pipeline_status == "resolved" else IncidentStatus.ESCALATED,
        )
        event = (
            INCIDENT_RESOLVED
            if pipeline_status in ("resolved", "mitigated")
            else INCIDENT_FAILED
        )
        await self._emit(
            event,
            {
                "incident_id": incident.incident_id,
                "execution_id": execution_id,
                "resolution_id": resolution.resolution_id,
                "status": pipeline_status,
            },
            emitter_context,
        )
        _tracer.record_pipeline_completed(
            incident_id=incident.incident_id,
            execution_id=execution_id,
            status=pipeline_status,
            duration_ms=_duration(),
        )
        bound_log.info("pipeline_completed", status=pipeline_status)
        return _result(pipeline_status)

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _update_status(self, incident: Incident, status: IncidentStatus) -> None:
        """Persist an updated incident status; swallow failures so pipeline continues."""
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

    async def _emit(
        self,
        event_name: str,
        payload: Mapping[str, Any],
        context: Any,
    ) -> None:
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter.emit(event_name, payload, context)
        except Exception as exc:
            log.warning("event_emission_failed", event_name=event_name, error=str(exc))

    @staticmethod
    def _build_resolution(
        incident: Incident,
        execution_id: str,
        status: ResolutionStatus,
        *,
        rca: RootCauseAnalysis | None = None,
        remediation_result: RemediationResult | None = None,
        verification_result: Any = None,
        duration_ms: float = 0.0,
        summary: str = "",
    ) -> IncidentResolution:
        return IncidentResolution(
            resolution_id=str(uuid.uuid4()),
            incident_id=incident.incident_id,
            status=status,
            resolved_at=datetime.now(UTC),
            duration_ms=duration_ms,
            execution_id=execution_id,
            rca=rca,
            remediation_result=remediation_result,
            verification_result=verification_result,
            summary=summary or (
                f"Incident {incident.incident_id} {status.value} "
                f"after {duration_ms:.0f}ms."
            ),
        )
