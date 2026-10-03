"""VerificationCoordinator — post-deployment observation and closed-loop verification.

Responsibilities:
  1. Wait for the configured observation window after deployment completes.
  2. Collect fresh post-deployment evidence via EvidenceOrchestrator.
  3. Run the existing VerificationEngine (before/after comparison).
  4. Produce a typed VerificationOutcome.
  5. Emit lifecycle events at every transition.
  6. Propagate incident_id, correlation_id, deployment_id through OTel spans.

Domain-neutral design:
  * No hard-coded metrics, thresholds, or service names.
  * VerificationPlan supplies all thresholds and metric names.
  * Evidence providers supply the actual observations.
  * The coordinator never directly modifies incident state.

States (VerificationState):
  PENDING     — coordinator created, not yet started
  OBSERVING   — waiting in the post-deployment observation window
  COLLECTING  — collecting post-deployment evidence
  RUNNING     — executing VerificationEngine
  PASSED      — all checks passed; incident is improving
  FAILED      — one or more checks degraded
  TIMED_OUT   — observation window exceeded its budget
  CANCELLED   — cooperative cancellation was requested
  ERROR       — provider or engine error prevented completion
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from backend.core.validation_engine import VerificationEngine
from backend.models.deployment import DeploymentResult
from backend.models.incident import Incident
from backend.models.validation import VerificationPlan, VerificationResult
from backend.services.evidence_orchestrator import EvidenceOrchestrator
from backend.services.incident_events import (
    VERIFICATION_COMPLETED,
    VERIFICATION_EVIDENCE_COLLECTED,
    VERIFICATION_FAILED,
    VERIFICATION_OBSERVATION_STARTED,
    VERIFICATION_PASSED,
    VERIFICATION_STARTED,
    VERIFICATION_TIMED_OUT,
)
from backend.services.incident_repository import IncidentRepository

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer("sentinel.verification")


# ---------------------------------------------------------------------------
# Domain states
# ---------------------------------------------------------------------------


class VerificationState(StrEnum):
    """Domain-neutral verification lifecycle state."""

    PENDING = "pending"
    OBSERVING = "observing"
    COLLECTING = "collecting"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Outcome model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Complete result of one post-deployment verification run.

    ``state``              — terminal VerificationState
    ``verification_result``— from VerificationEngine (may be None on error)
    ``evidence_snapshot``  — evidence collected after deployment
    ``deployment_id``      — links back to the triggering deployment
    ``observation_duration_ms`` — time spent in the observation window
    ``total_duration_ms``  — total coordinator wall-clock time
    ``reinvestigate``      — True when the pipeline should re-enter investigation
    ``failure_reason``     — why the outcome was not PASSED (None on PASSED)
    """

    state: VerificationState
    incident_id: str
    deployment_id: str | None
    verification_result: VerificationResult | None
    observation_duration_ms: float
    total_duration_ms: float
    reinvestigate: bool
    failure_reason: str | None = None
    execution_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.state == VerificationState.PASSED


# ---------------------------------------------------------------------------
# VerificationCoordinator
# ---------------------------------------------------------------------------


@dataclass
class VerificationCoordinator:
    """Orchestrates post-deployment observation and verification.

    Inject:
      ``verification_engine``    — VerificationEngine for before/after comparison
      ``evidence_orchestrator``  — EvidenceOrchestrator for post-deployment evidence
      ``event_emitter``          — optional IncidentEventEmitter
      ``repository``             — optional IncidentRepository (for idempotency)
      ``observation_window_seconds`` — how long to wait before collecting evidence
      ``verification_timeout_seconds`` — maximum budget for the full cycle
    """

    verification_engine: VerificationEngine
    evidence_orchestrator: EvidenceOrchestrator
    event_emitter: Any | None = None
    repository: IncidentRepository | None = None
    observation_window_seconds: float = 60.0
    verification_timeout_seconds: float = 300.0

    async def verify(
        self,
        incident: Incident,
        plan: VerificationPlan,
        deployment_result: DeploymentResult | None = None,
        *,
        execution_id: str | None = None,
        cancellation_check: Any = None,
        context: dict[str, Any] | None = None,
    ) -> VerificationOutcome:
        """Run the full post-deployment verification cycle.

        Steps:
          1. Emit VerificationStarted.
          2. Wait the observation window (cooperative cancellation checked).
          3. Collect fresh evidence.
          4. Run VerificationEngine.
          5. Emit terminal event (Passed / Failed / TimedOut / Cancelled).
          6. Return VerificationOutcome.
        """
        execution_id = execution_id or str(uuid.uuid4())
        deployment_id = deployment_result.request.deployment_id if deployment_result else None
        t0 = time.monotonic()
        obs_t0 = t0

        bound_log = log.bind(
            incident_id=incident.incident_id,
            deployment_id=deployment_id,
            execution_id=execution_id,
            correlation_id=incident.correlation_id,
        )

        with _tracer.start_as_current_span(
            "verification.cycle", kind=trace.SpanKind.INTERNAL
        ) as span:
            _set_span_attrs(
                span,
                incident_id=incident.incident_id,
                deployment_id=deployment_id,
                execution_id=execution_id,
                correlation_id=incident.correlation_id,
            )

            await self._emit(
                VERIFICATION_STARTED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "deployment_id": deployment_id,
                    "observation_window_seconds": self.observation_window_seconds,
                },
            )

            # ── Observation window ────────────────────────────────────────
            await self._emit(
                VERIFICATION_OBSERVATION_STARTED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "window_seconds": self.observation_window_seconds,
                },
            )
            bound_log.info(
                "verification_observation_started",
                window_seconds=self.observation_window_seconds,
            )

            obs_deadline = time.monotonic() + self.observation_window_seconds
            cancelled = False
            timed_out = False

            while time.monotonic() < obs_deadline:
                if cancellation_check is not None and cancellation_check():
                    cancelled = True
                    break
                # Check global timeout
                if (time.monotonic() - t0) > self.verification_timeout_seconds:
                    timed_out = True
                    break
                await asyncio.sleep(min(5.0, obs_deadline - time.monotonic()))

            obs_duration_ms = (time.monotonic() - obs_t0) * 1000

            if cancelled:
                span.set_status(StatusCode.OK)
                return self._outcome(
                    state=VerificationState.CANCELLED,
                    incident_id=incident.incident_id,
                    deployment_id=deployment_id,
                    execution_id=execution_id,
                    verification_result=None,
                    obs_ms=obs_duration_ms,
                    total_ms=(time.monotonic() - t0) * 1000,
                    reinvestigate=False,
                    reason="Verification cancelled by caller.",
                )

            if timed_out:
                await self._emit(
                    VERIFICATION_TIMED_OUT,
                    {
                        "incident_id": incident.incident_id,
                        "execution_id": execution_id,
                        "timeout_seconds": self.verification_timeout_seconds,
                    },
                )
                span.set_status(StatusCode.OK)
                return self._outcome(
                    state=VerificationState.TIMED_OUT,
                    incident_id=incident.incident_id,
                    deployment_id=deployment_id,
                    execution_id=execution_id,
                    verification_result=None,
                    obs_ms=obs_duration_ms,
                    total_ms=(time.monotonic() - t0) * 1000,
                    reinvestigate=True,
                    reason="Verification timed out before observation window completed.",
                )

            # ── Evidence collection ───────────────────────────────────────
            bound_log.info("verification_collecting_evidence")
            try:
                ev_result = await self.evidence_orchestrator.collect(
                    incident, context=context
                )
            except Exception as exc:
                bound_log.warning("verification_evidence_collection_failed", error=str(exc))
                ev_result = None

            await self._emit(
                VERIFICATION_EVIDENCE_COLLECTED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "evidence_count": ev_result.collection.count if ev_result else 0,
                    "partial": ev_result.partial if ev_result else True,
                },
            )

            # ── Run VerificationEngine ────────────────────────────────────
            bound_log.info("verification_running_engine")
            try:
                verification_result = await self.verification_engine.verify(
                    plan, incident, context=context
                )
            except Exception as exc:
                bound_log.error("verification_engine_failed", error=str(exc))
                span.set_status(StatusCode.ERROR)
                return self._outcome(
                    state=VerificationState.ERROR,
                    incident_id=incident.incident_id,
                    deployment_id=deployment_id,
                    execution_id=execution_id,
                    verification_result=None,
                    obs_ms=obs_duration_ms,
                    total_ms=(time.monotonic() - t0) * 1000,
                    reinvestigate=True,
                    reason=f"Verification engine error: {exc}",
                )

            total_ms = (time.monotonic() - t0) * 1000

            # ── Determine outcome ─────────────────────────────────────────
            passed = verification_result.passed
            state = VerificationState.PASSED if passed else VerificationState.FAILED
            reinvestigate = not passed

            terminal_event = VERIFICATION_PASSED if passed else VERIFICATION_FAILED
            await self._emit(
                terminal_event,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "deployment_id": deployment_id,
                    "verdict": verification_result.overall_verdict.value,
                    "confidence": verification_result.confidence,
                    "duration_ms": total_ms,
                },
            )
            await self._emit(
                VERIFICATION_COMPLETED,
                {
                    "incident_id": incident.incident_id,
                    "execution_id": execution_id,
                    "passed": passed,
                    "duration_ms": total_ms,
                },
            )

            span.set_attribute("verification.state", state.value)
            span.set_attribute("verification.passed", passed)
            span.set_attribute("verification.confidence", verification_result.confidence)
            span.set_attribute("verification.duration_ms", round(total_ms, 2))
            span.set_status(StatusCode.OK)

            bound_log.info(
                "verification_complete",
                state=state.value,
                confidence=verification_result.confidence,
                duration_ms=round(total_ms, 2),
            )

            return self._outcome(
                state=state,
                incident_id=incident.incident_id,
                deployment_id=deployment_id,
                execution_id=execution_id,
                verification_result=verification_result,
                obs_ms=obs_duration_ms,
                total_ms=total_ms,
                reinvestigate=reinvestigate,
                reason=None if passed else verification_result.summary,
            )

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _outcome(
        *,
        state: VerificationState,
        incident_id: str,
        deployment_id: str | None,
        execution_id: str | None,
        verification_result: VerificationResult | None,
        obs_ms: float,
        total_ms: float,
        reinvestigate: bool,
        reason: str | None,
    ) -> VerificationOutcome:
        return VerificationOutcome(
            state=state,
            incident_id=incident_id,
            deployment_id=deployment_id,
            verification_result=verification_result,
            observation_duration_ms=obs_ms,
            total_duration_ms=total_ms,
            reinvestigate=reinvestigate,
            failure_reason=reason,
            execution_id=execution_id,
        )

    async def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        if self.event_emitter is None:
            return
        try:
            await self.event_emitter.emit(event_name, payload, None)
        except Exception as exc:
            log.warning(
                "verification_event_emission_failed",
                event=event_name, error=str(exc)
            )


def _set_span_attrs(
    span: Any,
    *,
    incident_id: str,
    deployment_id: str | None,
    execution_id: str | None,
    correlation_id: str | None,
) -> None:
    from opentelemetry.trace import NonRecordingSpan
    if isinstance(span, NonRecordingSpan):
        return
    span.set_attribute("incident.id", incident_id)
    if deployment_id:
        span.set_attribute("deployment.id", deployment_id)
    if execution_id:
        span.set_attribute("execution.id", execution_id)
    if correlation_id:
        span.set_attribute("correlation.id", correlation_id)
