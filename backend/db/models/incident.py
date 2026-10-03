"""SQLAlchemy ORM model for the incidents table.

The domain ``Incident`` dataclass remains completely independent of SQLAlchemy.
``IncidentORM`` is the persistence mapping only — business logic stays in the
domain layer.  ``PostgreSQLIncidentRepository`` translates between the two.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db.base import Base


class IncidentORM(Base):
    """Persistence representation of an Incident.

    Every field stores what is needed for query and lifecycle management.
    Complex nested structures (signals, symptoms, tags) are serialised as
    JSON text so the domain model stays generic and schema-agnostic.
    """

    __tablename__ = "incidents"

    # Primary identity
    incident_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Core fields
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # JSON-serialised lists / complex structures
    affected_services_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    symptoms_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    signals_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # Timestamps (timezone-aware UTC stored as TIMESTAMP WITH TIME ZONE)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # Fast lookup of open incidents by status
        Index("ix_incidents_status", "status"),
        # Fast deduplication query
        Index("ix_incidents_correlation_id", "correlation_id"),
    )
