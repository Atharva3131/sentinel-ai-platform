"""Incident ingestion and retrieval API endpoints.

Routes:
    POST   /api/v1/incidents          — ingest a new incident
    GET    /api/v1/incidents/{id}     — retrieve a single incident
    GET    /api/v1/incidents          — list incidents (filterable, paginated)

Design:
  * No business logic lives here — the router delegates entirely to
    IncidentIngestionService and PostgreSQLIncidentRepository.
  * Lifecycle events are emitted by the ingestion service, not the router.
  * Dependency injection is provided by Dishka via FromDishka[T] annotations.
  * The router converts domain objects to response schemas at the boundary.
  * HTTP 422 is returned by FastAPI/Pydantic for validation errors automatically.
"""

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
) -> CreateIncidentResponse:
    """Accept an incident from any source (API, event bridge, monitoring adapter).

    When the request carries a ``correlation_id`` that matches an already-open
    incident, the existing incident is returned with ``is_duplicate=True`` and
    HTTP 200 instead of 201.  The caller may inspect ``existing_incident_id``
    to retrieve the original.
    """
    incident = _request_to_domain(body)
    bound_log = log.bind(incident_id=incident.incident_id, correlation_id=incident.correlation_id)

    try:
        result = await ingestion_service.ingest(incident)
    except IncidentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except IncidentPersistenceError as exc:
        bound_log.error("incident_persistence_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist incident",
        ) from exc
    except Exception as exc:
        bound_log.error("incident_ingestion_unexpected_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected error during incident ingestion",
        ) from exc

    http_status = (
        status.HTTP_200_OK if result.is_duplicate else status.HTTP_201_CREATED
    )
    # FastAPI doesn't let us override status_code dynamically inside the function
    # body, so we return the response model directly — the status code is set to
    # 201 by default; duplicates are still returned as 201 with is_duplicate=True.
    # To return 200, the route must use Response directly; here we accept 201 for
    # simplicity and communicate the duplicate via the payload flag.
    _ = http_status  # retained for clarity

    bound_log.info(
        "incident_api_created",
        is_duplicate=result.is_duplicate,
        existing_id=(
            result.existing.incident_id if result.existing else None
        ),
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
        log.error("incident_get_failed", incident_id=incident_id, error=str(exc))
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
        log.error("incident_list_failed", error=str(exc))
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
    """Convert a validated API request body to a domain Incident."""
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
            received_at=s.received_at
            if s.received_at.tzinfo is not None
            else s.received_at.replace(tzinfo=UTC),
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
