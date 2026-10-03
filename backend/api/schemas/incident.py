"""Pydantic transport schemas for the Incident API.

These schemas live at the API boundary — they are NOT domain models.
Domain Incident objects are constructed/read via the ingestion service,
never constructed directly from these schemas in business logic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------


class IncidentSignalRequest(BaseModel):
    """One operational signal that contributed to incident detection."""

    signal_id: str = Field(..., min_length=1, max_length=64)
    signal_type: str = Field(..., min_length=1, max_length=64)
    source: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=512)
    description: str = Field(..., min_length=1)
    severity: str = Field(..., min_length=1, max_length=32)
    received_at: datetime
    service: str | None = None
    environment: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreateIncidentRequest(BaseModel):
    """Request body for POST /api/v1/incidents."""

    incident_id: str | None = Field(
        default=None,
        description="Caller-supplied incident ID; auto-generated when omitted.",
        max_length=64,
    )
    title: str = Field(..., min_length=1, max_length=512)
    description: str = Field(..., min_length=1)
    severity: str = Field(..., min_length=1, max_length=32)
    affected_services: list[str] = Field(..., min_length=1)
    environment: str | None = Field(default=None, max_length=128)
    tenant_id: str | None = Field(default=None, max_length=64)
    correlation_id: str | None = Field(
        default=None,
        description="Idempotency key; duplicate requests with the same correlation_id "
                    "return the existing incident.",
        max_length=64,
    )
    symptoms: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    signals: list[IncidentSignalRequest] = Field(default_factory=list)
    detected_at: datetime | None = Field(
        default=None,
        description="UTC timestamp when the incident was first detected; "
                    "defaults to the current server time.",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("affected_services")
    @classmethod
    def affected_services_not_empty_strings(cls, v: list[str]) -> list[str]:
        for svc in v:
            if not svc.strip():
                raise ValueError("affected_services must not contain empty strings")
        return v

    @field_validator("severity")
    @classmethod
    def severity_valid(cls, v: str) -> str:
        allowed = {"critical", "high", "medium", "low"}
        if v.lower() not in allowed:
            raise ValueError(f"severity must be one of {sorted(allowed)}")
        return v.lower()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class IncidentSignalResponse(BaseModel):
    """Signal serialised in an API response."""

    signal_id: str
    signal_type: str
    source: str
    title: str
    severity: str
    received_at: datetime
    service: str | None = None
    environment: str | None = None


class IncidentResponse(BaseModel):
    """Single incident resource."""

    incident_id: str
    correlation_id: str | None
    title: str
    description: str
    severity: str
    status: str
    environment: str | None
    tenant_id: str | None
    affected_services: list[str]
    symptoms: list[str]
    tags: list[str]
    signals: list[IncidentSignalResponse]
    detected_at: datetime
    metadata: dict[str, Any]


class CreateIncidentResponse(BaseModel):
    """Response returned from POST /api/v1/incidents."""

    incident: IncidentResponse
    is_duplicate: bool = Field(
        description="True when the request matched an already-open incident "
                    "via correlation_id de-duplication."
    )
    existing_incident_id: str | None = Field(
        default=None,
        description="ID of the pre-existing incident when is_duplicate=True.",
    )


class IncidentListResponse(BaseModel):
    """Paginated list of incidents."""

    items: list[IncidentResponse]
    total: int
    limit: int
    offset: int


class ErrorResponse(BaseModel):
    """Structured error payload."""

    error: str
    detail: str | None = None
    incident_id: str | None = None
