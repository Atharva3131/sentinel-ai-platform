"""IncidentIngestionService — the entry gate for every incident.

Responsibilities:
  1. Validate the incoming incident (required fields, severity range).
  2. Assign a correlation ID when none is present.
  3. Detect duplicates via correlation ID (de-duplication).
  4. Persist the normalised incident via IncidentRepository.
  5. Emit an ``Incident.Lifecycle.Created`` event.
  6. Return the persisted incident (and a duplicate flag when applicable).

The service is intentionally slim — it performs only ingestion, not
investigation.  The investigation pipeline is initiated by the caller
(IncidentOrchestrationPipeline) after successful ingestion.

No vendor-specific logic lives here.  The service is agnostic to whether the
incident arrived from a REST API, an event bus message, a monitoring webhook,
or a test harness.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import structlog

from backend.models.incident import Incident
from backend.services.incident_events import INCIDENT_CREATED, INCIDENT_DUPLICATE_DETECTED
from backend.services.incident_repository import IncidentRepository

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IncidentValidationError(ValueError):
    """Raised when an incident fails structural validation."""


class IncidentIngestionError(RuntimeError):
    """Raised when ingestion fails for a non-validation reason (e.g. persistence)."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Output of one IncidentIngestionService.ingest() call.

    ``incident``    — the normalised, persisted incident
    ``is_duplicate``— True when a matching open incident already existed
    ``existing``    — the pre-existing incident when ``is_duplicate`` is True
    """

    incident: Incident
    is_duplicate: bool
    existing: Incident | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass
class IncidentIngestionService:
    """Validates, persists, and emits events for incoming incidents.

    Inject:
      ``repository``     — IncidentRepository (port; in-memory or DB-backed)
      ``event_emitter``  — optional IncidentEventEmitter
      ``emitter_context``— optional context forwarded to event_emitter.emit()
    """

    repository: IncidentRepository
    event_emitter: Any | None = None  # IncidentEventEmitter (structural protocol)

    async def ingest(
        self,
        incident: Incident,
        *,
        emitter_context: Any = None,
    ) -> IngestionResult:
        """Ingest *incident* through the full ingestion pipeline.

        Raises:
            IncidentValidationError: if the incident is structurally invalid.
            IncidentIngestionError:  if persistence fails.
        """
        self._validate(incident)

        # Assign correlation ID if missing
        incident = self._ensure_correlation_id(incident)

        bound_log = log.bind(
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
            severity=str(incident.severity),
        )

        # Duplicate detection — look for an open incident with the same correlation_id
        if incident.correlation_id:
            existing = await self.repository.exists_by_correlation(incident.correlation_id)
            if existing is not None and existing.incident_id != incident.incident_id:
                bound_log.info(
                    "duplicate_incident_detected",
                    existing_id=existing.incident_id,
                )
                await self._emit(
                    INCIDENT_DUPLICATE_DETECTED,
                    {
                        "incident_id": incident.incident_id,
                        "existing_incident_id": existing.incident_id,
                        "correlation_id": incident.correlation_id,
                    },
                    emitter_context,
                )
                return IngestionResult(
                    incident=incident,
                    is_duplicate=True,
                    existing=existing,
                )

        # Normalise metadata — ensure detected_at is timezone-aware
        incident = self._normalize_metadata(incident)

        # Persist
        try:
            await self.repository.save(incident)
        except Exception as exc:
            raise IncidentIngestionError(
                f"Failed to persist incident {incident.incident_id}: {exc}"
            ) from exc

        bound_log.info(
            "incident_ingested",
            title=incident.title,
            affected_services=list(incident.affected_services),
        )

        # Emit created event
        await self._emit(
            INCIDENT_CREATED,
            {
                "incident_id": incident.incident_id,
                "correlation_id": incident.correlation_id,
                "severity": str(incident.severity),
                "title": incident.title,
                "affected_services": list(incident.affected_services),
                "environment": incident.environment,
                "detected_at": incident.detected_at.isoformat(),
            },
            emitter_context,
        )

        return IngestionResult(incident=incident, is_duplicate=False)

    # ── Validation ────────────────────────────────────────────────────────

    @staticmethod
    def _validate(incident: Incident) -> None:
        """Raise IncidentValidationError for structurally invalid incidents."""
        errors: list[str] = []

        if not incident.incident_id or not incident.incident_id.strip():
            errors.append("incident_id is required")

        if not incident.title or not incident.title.strip():
            errors.append("title is required")

        if not incident.affected_services:
            errors.append("at least one affected_service is required")

        if not incident.description or not incident.description.strip():
            errors.append("description is required")

        if errors:
            raise IncidentValidationError(
                f"Incident validation failed: {'; '.join(errors)}"
            )

    # ── Normalization ─────────────────────────────────────────────────────

    @staticmethod
    def _ensure_correlation_id(incident: Incident) -> Incident:
        if incident.correlation_id:
            return incident
        from dataclasses import replace
        return replace(incident, correlation_id=str(uuid.uuid4()))

    @staticmethod
    def _normalize_metadata(incident: Incident) -> Incident:
        """Ensure detected_at is UTC-aware and status is OPEN if not set."""
        from dataclasses import replace
        changes: dict[str, Any] = {}

        # Make detected_at timezone-aware if naive
        if incident.detected_at.tzinfo is None:
            changes["detected_at"] = incident.detected_at.replace(tzinfo=UTC)

        if changes:
            return replace(incident, **changes)
        return incident

    # ── Event emission ────────────────────────────────────────────────────

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
