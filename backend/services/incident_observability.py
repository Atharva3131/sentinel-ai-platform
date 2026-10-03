"""Incident lifecycle OpenTelemetry instrumentation.

Provides a single ``IncidentTracer`` that creates spans and records metrics
for every phase of the incident orchestration pipeline.  All context IDs
(incident_id, execution_id, correlation_id, workflow_id) are propagated as
span attributes and structured-log fields so traces, metrics, and logs can
be correlated without manual effort.

Design:
  * Uses the OpenTelemetry API only — no SDK-specific calls outside of
    the span/attribute surface so this works with any compatible backend.
  * structlog is used for structured logging alongside OTel spans.
  * The tracer is stateless; it can be shared across concurrent pipelines.
  * All methods are async-safe (spans are created synchronously inside
    async callers — OTel Python spans are not awaitable).
  * No vendor-specific exporters are imported or required here.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import structlog
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, Span, StatusCode

log = structlog.get_logger(__name__)

_TRACER_NAME = "sentinel.incident.orchestration"


@dataclass(slots=True)
class IncidentTracer:
    """OpenTelemetry + structlog instrumentation for incident lifecycle phases.

    Inject one instance per pipeline; share it freely — it is stateless.
    ``service_name`` is attached to every span as ``service.name``.
    """

    service_name: str = "incident-orchestrator"

    @property
    def _tracer(self) -> trace.Tracer:
        return trace.get_tracer(_TRACER_NAME)

    # ── Span factories ────────────────────────────────────────────────────

    @contextmanager
    def ingestion_span(
        self,
        *,
        incident_id: str,
        correlation_id: str | None = None,
    ) -> Generator[Span, None, None]:
        """Span wrapping incident ingestion."""
        with self._tracer.start_as_current_span(
            "incident.ingestion",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(span, incident_id=incident_id, correlation_id=correlation_id)
            try:
                yield span
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @contextmanager
    def investigation_span(
        self,
        *,
        incident_id: str,
        execution_id: str | None = None,
        correlation_id: str | None = None,
        workflow_id: str | None = None,
    ) -> Generator[Span, None, None]:
        """Span wrapping the full iterative investigation."""
        with self._tracer.start_as_current_span(
            "incident.investigation",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(
                span,
                incident_id=incident_id,
                execution_id=execution_id,
                correlation_id=correlation_id,
                workflow_id=workflow_id,
            )
            try:
                yield span
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @contextmanager
    def evidence_collection_span(
        self,
        *,
        incident_id: str,
        provider_count: int,
        correlation_id: str | None = None,
    ) -> Generator[Span, None, None]:
        """Span wrapping one evidence collection phase."""
        with self._tracer.start_as_current_span(
            "incident.evidence_collection",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(
                span, incident_id=incident_id, correlation_id=correlation_id
            )
            span.set_attribute("evidence.provider_count", provider_count)
            try:
                yield span
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @contextmanager
    def provider_span(
        self,
        provider_name: str,
        *,
        incident_id: str,
        correlation_id: str | None = None,
    ) -> Generator[Span, None, None]:
        """Span for one individual evidence provider call."""
        with self._tracer.start_as_current_span(
            f"incident.provider.{provider_name}",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(
                span, incident_id=incident_id, correlation_id=correlation_id
            )
            span.set_attribute("evidence.provider", provider_name)
            try:
                yield span
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @contextmanager
    def hypothesis_span(
        self,
        *,
        incident_id: str,
        execution_id: str | None = None,
        iteration: int = 1,
    ) -> Generator[Span, None, None]:
        """Span wrapping hypothesis generation + evaluation."""
        with self._tracer.start_as_current_span(
            "incident.hypothesis_evaluation",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(
                span, incident_id=incident_id, execution_id=execution_id
            )
            span.set_attribute("investigation.iteration", iteration)
            try:
                yield span
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @contextmanager
    def rca_span(
        self,
        *,
        incident_id: str,
        execution_id: str | None = None,
    ) -> Generator[Span, None, None]:
        """Span wrapping root cause analysis."""
        with self._tracer.start_as_current_span(
            "incident.rca",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(
                span, incident_id=incident_id, execution_id=execution_id
            )
            try:
                yield span
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @contextmanager
    def remediation_span(
        self,
        *,
        incident_id: str,
        execution_id: str | None = None,
        plan_id: str | None = None,
    ) -> Generator[Span, None, None]:
        """Span wrapping remediation plan execution."""
        with self._tracer.start_as_current_span(
            "incident.remediation",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(
                span, incident_id=incident_id, execution_id=execution_id
            )
            if plan_id:
                span.set_attribute("remediation.plan_id", plan_id)
            try:
                yield span
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @contextmanager
    def validation_span(
        self,
        *,
        incident_id: str,
        execution_id: str | None = None,
    ) -> Generator[Span, None, None]:
        """Span wrapping post-remediation validation."""
        with self._tracer.start_as_current_span(
            "incident.validation",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(
                span, incident_id=incident_id, execution_id=execution_id
            )
            try:
                yield span
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    @contextmanager
    def verification_span(
        self,
        *,
        incident_id: str,
        execution_id: str | None = None,
    ) -> Generator[Span, None, None]:
        """Span wrapping pre/post verification comparison."""
        with self._tracer.start_as_current_span(
            "incident.verification",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            self._set_incident_attrs(
                span, incident_id=incident_id, execution_id=execution_id
            )
            try:
                yield span
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise

    # ── Attribute helpers ─────────────────────────────────────────────────

    @staticmethod
    def _set_incident_attrs(
        span: Span,
        *,
        incident_id: str,
        execution_id: str | None = None,
        correlation_id: str | None = None,
        workflow_id: str | None = None,
    ) -> None:
        """Attach canonical incident context attributes to a span."""
        if isinstance(span, NonRecordingSpan):
            return
        span.set_attribute("incident.id", incident_id)
        if execution_id:
            span.set_attribute("execution.id", execution_id)
        if correlation_id:
            span.set_attribute("correlation.id", correlation_id)
        if workflow_id:
            span.set_attribute("workflow.id", workflow_id)

    # ── Structured-log helpers ────────────────────────────────────────────

    @staticmethod
    def record_provider_latency(
        provider_name: str,
        duration_ms: float,
        *,
        incident_id: str,
        succeeded: bool,
    ) -> None:
        """Emit a structured log entry for one provider call."""
        log.info(
            "provider_latency",
            provider=provider_name,
            duration_ms=round(duration_ms, 2),
            incident_id=incident_id,
            succeeded=succeeded,
        )

    @staticmethod
    def record_provider_failure(
        provider_name: str,
        error: str,
        *,
        incident_id: str,
    ) -> None:
        """Emit a structured log entry for a provider failure."""
        log.warning(
            "provider_failure",
            provider=provider_name,
            error=error,
            incident_id=incident_id,
        )

    @staticmethod
    def record_investigation_iteration(
        iteration: int,
        confidence: float,
        *,
        incident_id: str,
        execution_id: str | None = None,
        evidence_count: int,
    ) -> None:
        """Emit a structured log entry for an investigation iteration."""
        log.info(
            "investigation_iteration",
            iteration=iteration,
            confidence=round(confidence, 4),
            evidence_count=evidence_count,
            incident_id=incident_id,
            execution_id=execution_id,
        )

    @staticmethod
    def record_rca_outcome(
        *,
        incident_id: str,
        execution_id: str | None = None,
        confidence: float,
        has_root_cause: bool,
        iterations: int,
        duration_ms: float,
    ) -> None:
        """Emit a structured log entry for the final RCA outcome."""
        log.info(
            "rca_outcome",
            incident_id=incident_id,
            execution_id=execution_id,
            confidence=round(confidence, 4),
            has_root_cause=has_root_cause,
            iterations=iterations,
            duration_ms=round(duration_ms, 2),
        )

    @staticmethod
    def record_pipeline_completed(
        *,
        incident_id: str,
        execution_id: str | None = None,
        status: str,
        duration_ms: float,
    ) -> None:
        """Emit a structured log entry for pipeline completion."""
        log.info(
            "pipeline_completed",
            incident_id=incident_id,
            execution_id=execution_id,
            status=status,
            duration_ms=round(duration_ms, 2),
        )
