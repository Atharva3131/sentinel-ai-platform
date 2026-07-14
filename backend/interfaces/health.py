"""Health-check contracts shared by application and infrastructure layers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class HealthStatus(StrEnum):
    """Normalized component health status."""

    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    """Provider-neutral result of one dependency check."""

    name: str
    status: HealthStatus
    latency_ms: float
    detail: str | None = None


class HealthCheck(Protocol):
    """Contract implemented by readiness dependency adapters."""

    @property
    def name(self) -> str:
        """Return the stable dependency name."""
        ...

    async def check(self) -> None:
        """Raise when the dependency is not ready."""
        ...
