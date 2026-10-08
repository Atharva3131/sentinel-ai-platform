from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import structlog
from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, HTTPException, Query, status

from backend.api.schemas.incident import (
    CreateIncidentRequest,
    CreateIncidentResponse,
    ErrorResponse,
    IncidentListResponse,
    IncidentResponse,
    IncidentSignalResponse,
)
from backend.db.repositories.incident import (
    IncidentPersistenceError,
    PostgreSQLIncidentRepository,
)
from backend.models.incident import (
    Incident,
    IncidentSeverity,
    IncidentSignal,
    IncidentStatus,
    SignalSource,
    SignalType,
)
from backend.services.closed_loop_orchestrator import ClosedLoopOrchestrator
from backend.services.incident_ingestion import (
    IncidentIngestionService,
    IncidentValidationError,
)

log = structlog.get_logger(__name__)


router = APIRouter(
    prefix="/incidents",
    tags=["incidents"],
    route_class=DishkaRoute,
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Validation error"},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
    },
)


# ---------------------------------------------------------------------------
# POST /incidents
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=CreateIncidentResponse,
    summary="Ingest a new incident",
    responses={
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "Duplicate incident (same correlation_id already open)",
        },
    },
)
async def create_incident(
    body: CreateIncidentRequest,
    ingestion_service: FromDishka[IncidentIngestionService],
    closed_loop_orchestrator: FromDishka[ClosedLoopOrchestrator],
) -> CreateIncidentResponse:
    """Accept an incident from any source (API, event bridge, monitoring adapter).

    When the request carries a ``correlation_id`` that matches an already-open
    incident, the existing incident is returned with ``is_duplicate=True`` and
    HTTP 200 instead of 201. The caller may inspect ``existing_incident_id``
    to retrieve the original.

    For new incidents, after persistence the ClosedLoopOrchestrator is invoked
    inline to drive the investigation → remediation → deployment → verification
    lifecycle. Orchestration failures are logged but do not affect the 201
    response — the incident is already safely persisted.
    """
    incident = _request_to_domain(body)

    bound_log = log.bind(
        incident_id=incident.incident_id,
        correlation_id=incident.correlation_id,
    )

    try:
        result = await ingestion_service.ingest(incident)

    except IncidentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    except IncidentPersistenceError as exc:
        bound_log.error(
            "incident_persistence_failed",
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist incident",
        ) from exc

    except Exception as exc:
        bound_log.error(
            "incident_ingestion_unexpected_error",
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error during incident ingestion",
        ) from exc

    bound_log.info(
        "incident_api_created",
        is_duplicate=result.is_duplicate,
        existing_id=(
            result.existing.incident_id if result.existing else None
        ),
    )

    # Only trigger the closed-loop for genuinely new incidents.
    # Duplicates already have an active orchestration cycle in progress.
    if not result.is_duplicate:
        try:
            await closed_loop_orchestrator.run(result.incident)

        except Exception as exc:
            # The incident is persisted — log the failure and return 201.
            #
            # IMPORTANT:
            # Use exception() rather than error() so the full traceback is
            # emitted. This lets us identify the actual closed-loop failure
            # in production.
            bound_log.exception(
                "closed_loop_orchestration_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                incident_id=result.incident.incident_id,
            )

    return CreateIncidentResponse(
        incident=_domain_to_response(result.incident),
        is_duplicate=result.is_duplicate,
        existing_incident_id=(
            result.existing.incident_id if result.existing else None
        ),
    )


# ---------------------------------------------------------------------------
# GET /incidents/{incident_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{incident_id}",
    response_model=IncidentResponse,
    summary="Retrieve a single incident",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "Incident not found",
        },
    },
)
async def get_incident(
    incident_id: str,
    repository: FromDishka[PostgreSQLIncidentRepository],
) -> IncidentResponse:
    """Return the incident identified by *incident_id*."""
    try:
        incident = await repository.get(incident_id)

    except IncidentPersistenceError as exc:
        log.error(
            "incident_get_failed",
            incident_id=incident_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve incident",
        ) from exc

    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Incident '{incident_id}' not found",
        )

    return _domain_to_response(incident)


# ---------------------------------------------------------------------------
# GET /incidents
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=IncidentListResponse,
    summary="List incidents",
)
async def list_incidents(
    repository: FromDishka[PostgreSQLIncidentRepository],
    status_filter: Annotated[
        str | None,
        Query(alias="status", description="Filter by lifecycle status"),
    ] = None,
    severity_filter: Annotated[
        str | None,
        Query(alias="severity", description="Filter by severity"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> IncidentListResponse:
    """Return a paginated list of incidents with optional status/severity filters."""
    try:
        incidents = await repository.list_all(
            limit=limit,
            offset=offset,
            status=status_filter,
            severity=severity_filter,
        )

    except IncidentPersistenceError as exc:
        log.error(
            "incident_list_failed",
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to list incidents",
        ) from exc

    return IncidentListResponse(
        items=[_domain_to_response(i) for i in incidents],
        total=len(incidents),
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Translation helpers
# ---------------------------------------------------------------------------


def _request_to_domain(body: CreateIncidentRequest) -> Incident:
    """Convert a validated API request to a domain Incident."""
    incident_id = body.incident_id or str(uuid.uuid4())

    detected_at = body.detected_at or datetime.now(UTC)

    # Ensure timezone-aware
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=UTC)

    signals = tuple(
        IncidentSignal(
            signal_id=s.signal_id,
            signal_type=_safe_signal_type(s.signal_type),
            source=_safe_signal_source(s.source),
            title=s.title,
            description=s.description,
            severity=IncidentSeverity(s.severity),
            received_at=(
                s.received_at
                if s.received_at.tzinfo is not None
                else s.received_at.replace(tzinfo=UTC)
            ),
            service=s.service,
            environment=s.environment,
            raw_payload=s.raw_payload,
            labels=s.labels,
        )
        for s in body.signals
    )

    return Incident(
        incident_id=incident_id,
        title=body.title,
        severity=IncidentSeverity(body.severity),
        status=IncidentStatus.OPEN,
        affected_services=tuple(body.affected_services),
        description=body.description,
        detected_at=detected_at,
        signals=signals,
        symptoms=tuple(body.symptoms),
        tenant_id=body.tenant_id,
        environment=body.environment,
        correlation_id=body.correlation_id,
        tags=tuple(body.tags),
        metadata=body.metadata,
    )


def _domain_to_response(incident: Incident) -> IncidentResponse:
    """Convert a domain Incident to an API response schema."""
    return IncidentResponse(
        incident_id=incident.incident_id,
        correlation_id=incident.correlation_id,
        title=incident.title,
        description=incident.description,
        severity=incident.severity.value,
        status=incident.status.value,
        environment=incident.environment,
        tenant_id=incident.tenant_id,
        affected_services=list(incident.affected_services),
        symptoms=list(incident.symptoms),
        tags=list(incident.tags),
        signals=[
            IncidentSignalResponse(
                signal_id=s.signal_id,
                signal_type=s.signal_type.value,
                source=s.source.value,
                title=s.title,
                severity=s.severity.value,
                received_at=s.received_at,
                service=s.service,
                environment=s.environment,
            )
            for s in incident.signals
        ],
        detected_at=incident.detected_at,
        metadata=incident.metadata,
    )


def _safe_signal_type(value: str) -> SignalType:
    try:
        return SignalType(value)
    except ValueError:
        return SignalType.OTHER


def _safe_signal_source(value: str) -> SignalSource:
    try:
        return SignalSource(value)
    except ValueError:
        return SignalSource.OTHER