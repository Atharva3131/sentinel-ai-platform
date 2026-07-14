"""Health endpoint response schemas."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


class ComponentHealthResponse(BaseModel):
    """One readiness dependency result."""

    name: str
    status: Literal["up", "down"]
    latency_ms: float = Field(ge=0)
    detail: str | None = None


class HealthResponse(BaseModel):
    """Common health response."""

    status: Literal["up", "ready", "not_ready"]
    service: str
    version: str
    environment: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checks: list[ComponentHealthResponse] = Field(default_factory=list)
