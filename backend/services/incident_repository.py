"""Incident repository port + in-memory implementation.

The port (``IncidentRepository``) is the only thing the ingestion service and
orchestration pipeline depend on.  The in-memory implementation is used in
tests and as a drop-in until a database-backed implementation is wired.

A real PostgreSQL-backed implementation satisfies the same protocol by
implementing the four async methods below.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.models.incident import Incident


@runtime_checkable
class IncidentRepository(Protocol):
    """Port for durable incident persistence."""

    async def save(self, incident: Incident) -> None:
        """Persist or update an incident record."""
        ...

    async def get(self, incident_id: str) -> Incident | None:
        """Return the incident with *incident_id*, or None if not found."""
        ...

    async def exists_by_correlation(self, correlation_id: str) -> Incident | None:
        """Return an open incident with the same correlation_id, or None."""
        ...

    async def list_open(self) -> list[Incident]:
        """Return all incidents that are not in a terminal state."""
        ...


class InMemoryIncidentRepository:
    """In-memory incident store for tests and local development."""

    def __init__(self) -> None:
        self._store: dict[str, Incident] = {}

    async def save(self, incident: Incident) -> None:
        self._store[incident.incident_id] = incident

    async def get(self, incident_id: str) -> Incident | None:
        return self._store.get(incident_id)

    async def exists_by_correlation(self, correlation_id: str) -> Incident | None:
        from backend.models.incident import IncidentStatus
        terminal = {IncidentStatus.RESOLVED, IncidentStatus.CANCELLED}
        for inc in self._store.values():
            if inc.correlation_id == correlation_id and inc.status not in terminal:
                return inc
        return None

    async def list_open(self) -> list[Incident]:
        from backend.models.incident import IncidentStatus
        terminal = {IncidentStatus.RESOLVED, IncidentStatus.CANCELLED}
        return [i for i in self._store.values() if i.status not in terminal]

    def all(self) -> list[Incident]:
        """Non-async helper for test assertions."""
        return list(self._store.values())
