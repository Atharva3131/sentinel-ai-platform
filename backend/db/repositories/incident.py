"""PostgreSQL-backed IncidentRepository.

This module provides:
  - ``IncidentMapper``    — translates Domain Incident ↔ IncidentORM
  - ``PostgreSQLIncidentRepository`` — implements the IncidentRepository protocol

The domain ``Incident`` is kept completely free of SQLAlchemy.
All translation logic lives in ``IncidentMapper``.

Structured error handling:
  - ``IncidentPersistenceError``  — base for all repository errors
  - ``IncidentNotFoundError``     — no row with given incident_id
  - ``IncidentDuplicateError``    — unique/constraint violation on insert
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.incident import IncidentORM
from backend.db.repositories.base import SQLAlchemyRepositoryBase
from backend.models.incident import (
    Incident,
    IncidentSeverity,
    IncidentSignal,
    IncidentStatus,
    SignalSource,
    SignalType,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IncidentPersistenceError(RuntimeError):
    """Base class for all incident repository errors."""


class IncidentNotFoundError(IncidentPersistenceError):
    """Raised when no incident row exists for the requested identifier."""

    def __init__(self, incident_id: str) -> None:
        super().__init__(f"Incident '{incident_id}' not found")
        self.incident_id = incident_id


class IncidentDuplicateError(IncidentPersistenceError):
    """Raised when an insert violates a uniqueness constraint."""

    def __init__(self, incident_id: str) -> None:
        super().__init__(f"Incident '{incident_id}' already exists")
        self.incident_id = incident_id


# ---------------------------------------------------------------------------
# IncidentMapper — domain ↔ ORM translation
# ---------------------------------------------------------------------------


class IncidentMapper:
    """Translates between the domain ``Incident`` dataclass and ``IncidentORM``.

    This class is the only place in the codebase that knows about both layers.
    It must never introduce business logic — only format translation.
    """

    @staticmethod
    def to_orm(incident: Incident, *, now: datetime | None = None) -> IncidentORM:
        """Convert a domain Incident to an IncidentORM instance."""
        ts = now or datetime.now(UTC)
        return IncidentORM(
            incident_id=incident.incident_id,
            correlation_id=incident.correlation_id,
            title=incident.title,
            description=incident.description,
            severity=incident.severity.value,
            status=incident.status.value,
            environment=incident.environment,
            tenant_id=incident.tenant_id,
            affected_services_json=json.dumps(list(incident.affected_services)),
            symptoms_json=json.dumps(list(incident.symptoms)),
            tags_json=json.dumps(list(incident.tags)),
            signals_json=json.dumps(
                [IncidentMapper._signal_to_dict(s) for s in incident.signals]
            ),
            metadata_json=json.dumps(incident.metadata),
            detected_at=incident.detected_at,
            created_at=ts,
            updated_at=ts,
        )

    @staticmethod
    def update_orm(orm: IncidentORM, incident: Incident) -> IncidentORM:
        """Apply domain Incident fields to an existing IncidentORM row (in-place)."""
        orm.correlation_id = incident.correlation_id
        orm.title = incident.title
        orm.description = incident.description
        orm.severity = incident.severity.value
        orm.status = incident.status.value
        orm.environment = incident.environment
        orm.tenant_id = incident.tenant_id
        orm.affected_services_json = json.dumps(list(incident.affected_services))
        orm.symptoms_json = json.dumps(list(incident.symptoms))
        orm.tags_json = json.dumps(list(incident.tags))
        orm.signals_json = json.dumps(
            [IncidentMapper._signal_to_dict(s) for s in incident.signals]
        )
        orm.metadata_json = json.dumps(incident.metadata)
        orm.detected_at = incident.detected_at
        orm.updated_at = datetime.now(UTC)
        return orm

    @staticmethod
    def to_domain(orm: IncidentORM) -> Incident:
        """Convert an IncidentORM row back to a domain Incident."""
        affected_services: tuple[str, ...] = tuple(
            json.loads(orm.affected_services_json or "[]")
        )
        symptoms: tuple[str, ...] = tuple(json.loads(orm.symptoms_json or "[]"))
        tags: tuple[str, ...] = tuple(json.loads(orm.tags_json or "[]"))
        signals: tuple[IncidentSignal, ...] = tuple(
            IncidentMapper._dict_to_signal(d)
            for d in json.loads(orm.signals_json or "[]")
        )
        metadata: dict[str, Any] = json.loads(orm.metadata_json or "{}")

        return Incident(
            incident_id=orm.incident_id,
            title=orm.title,
            severity=IncidentSeverity(orm.severity),
            status=IncidentStatus(orm.status),
            affected_services=affected_services,
            description=orm.description,
            detected_at=orm.detected_at,
            signals=signals,
            symptoms=symptoms,
            tenant_id=orm.tenant_id,
            environment=orm.environment,
            correlation_id=orm.correlation_id,
            tags=tags,
            metadata=metadata,
        )

    @staticmethod
    def _signal_to_dict(signal: IncidentSignal) -> dict[str, Any]:
        return {
            "signal_id": signal.signal_id,
            "signal_type": signal.signal_type.value,
            "source": signal.source.value,
            "title": signal.title,
            "description": signal.description,
            "severity": signal.severity.value,
            "received_at": signal.received_at.isoformat(),
            "service": signal.service,
            "environment": signal.environment,
            "raw_payload": signal.raw_payload,
            "labels": signal.labels,
            "metadata": signal.metadata,
        }

    @staticmethod
    def _dict_to_signal(d: dict[str, Any]) -> IncidentSignal:
        from datetime import datetime
        return IncidentSignal(
            signal_id=d["signal_id"],
            signal_type=SignalType(d["signal_type"]),
            source=SignalSource(d["source"]),
            title=d["title"],
            description=d["description"],
            severity=IncidentSeverity(d["severity"]),
            received_at=datetime.fromisoformat(d["received_at"]),
            service=d.get("service"),
            environment=d.get("environment"),
            raw_payload=d.get("raw_payload", {}),
            labels=d.get("labels", {}),
            metadata=d.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# PostgreSQLIncidentRepository
# ---------------------------------------------------------------------------


class PostgreSQLIncidentRepository(SQLAlchemyRepositoryBase[IncidentORM]):
    """Async PostgreSQL implementation of the IncidentRepository protocol.

    Satisfies the ``IncidentRepository`` structural protocol from
    ``backend.services.incident_repository`` so it can be injected anywhere
    the protocol is expected.

    Transaction management:
      The caller (usually the ingestion service) is responsible for transaction
      boundaries.  The repository performs ``flush()`` (not ``commit()``) after
      writes so the caller controls atomicity.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session, IncidentORM)

    async def save(self, incident: Incident) -> None:
        """Persist or update an incident.

        Uses SQLAlchemy ``merge()`` so both insert and update are handled
        atomically without a prior ``get()`` round-trip.

        Raises:
            IncidentDuplicateError: when a concurrent insert of the same ID
                violated the primary-key constraint.
        """
        now = datetime.now(UTC)
        existing = await self.session.get(IncidentORM, incident.incident_id)
        try:
            if existing is not None:
                # Rebuild a fresh ORM row and merge it — avoids mutating
                # instrumented mapped attributes outside a write transaction.
                new_orm = IncidentMapper.to_orm(incident, now=now)
                new_orm.created_at = existing.created_at  # preserve original created_at
                await self.session.merge(new_orm)
                await self.flush()
                log.debug("incident_updated", incident_id=incident.incident_id)
            else:
                orm_row = IncidentMapper.to_orm(incident, now=now)
                await self.add(orm_row)
                log.debug("incident_created", incident_id=incident.incident_id)
        except IntegrityError as exc:
            await self.rollback()
            raise IncidentDuplicateError(incident.incident_id) from exc

    async def get(self, incident_id: str) -> Incident | None:  # type: ignore[override]
        """Return the incident with *incident_id*, or None if not found."""
        orm = await self.session.get(IncidentORM, incident_id)
        if orm is None:
            return None
        return IncidentMapper.to_domain(orm)

    async def get_or_raise(self, incident_id: str) -> Incident:
        """Return the incident, raising IncidentNotFoundError when absent."""
        incident = await self.get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        return incident

    async def exists_by_correlation(self, correlation_id: str) -> Incident | None:
        """Return an open incident with the given correlation_id, or None.

        Only non-terminal statuses are considered open.
        """
        terminal = {IncidentStatus.RESOLVED.value, IncidentStatus.CANCELLED.value}
        stmt = (
            select(IncidentORM)
            .where(IncidentORM.correlation_id == correlation_id)
            .where(IncidentORM.status.notin_(terminal))
            .limit(1)
        )
        orm = await self.one_or_none(stmt)
        if orm is None:
            return None
        return IncidentMapper.to_domain(orm)

    async def list_open(self) -> list[Incident]:
        """Return all non-terminal incidents, ordered by detected_at descending."""
        terminal = {IncidentStatus.RESOLVED.value, IncidentStatus.CANCELLED.value}
        stmt = (
            select(IncidentORM)
            .where(IncidentORM.status.notin_(terminal))
            .order_by(IncidentORM.detected_at.desc())
        )
        rows = await self.list(stmt)
        return [IncidentMapper.to_domain(r) for r in rows]

    async def list_all(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        severity: str | None = None,
    ) -> list[Incident]:
        """Return incidents with optional filtering, ordered by detected_at descending."""
        stmt = select(IncidentORM).order_by(IncidentORM.detected_at.desc())
        if status:
            stmt = stmt.where(IncidentORM.status == status)
        if severity:
            stmt = stmt.where(IncidentORM.severity == severity)
        stmt = stmt.limit(limit).offset(offset)
        rows = await self.list(stmt)
        return [IncidentMapper.to_domain(r) for r in rows]
