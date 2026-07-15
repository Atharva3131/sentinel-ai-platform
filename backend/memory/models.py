"""Memory entry value objects and scope enumerations."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any


class MemoryScope(StrEnum):
    """Lifecycle scope of a memory entry."""

    EXECUTION = "execution"
    WORKFLOW = "workflow"
    SESSION = "session"
    GLOBAL = "global"


class MemoryClassification(StrEnum):
    """Data sensitivity classification."""

    NORMAL = "normal"
    SENSITIVE = "sensitive"


@dataclass(frozen=True, slots=True)
class MemoryMetadata:
    """Immutable metadata attached to a memory entry."""

    scope: MemoryScope
    scope_id: str
    classification: MemoryClassification = MemoryClassification.NORMAL
    source_references: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    redacted: bool = False
    version: int = 1
    ttl_seconds: float | None = None

    def with_version(self, version: int) -> MemoryMetadata:
        return replace(self, version=version)

    def with_redacted(self) -> MemoryMetadata:
        return replace(self, redacted=True, classification=MemoryClassification.SENSITIVE)


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """Immutable, versioned memory entry with provenance metadata."""

    key: str
    value: Any
    metadata: MemoryMetadata
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    checksum: str | None = None

    @classmethod
    def create(
        cls,
        key: str,
        value: Any,
        metadata: MemoryMetadata,
        *,
        ttl_seconds: float | None = None,
    ) -> MemoryEntry:
        now = datetime.now(UTC)
        effective_ttl = ttl_seconds if ttl_seconds is not None else metadata.ttl_seconds
        expires_at = now + timedelta(seconds=effective_ttl) if effective_ttl is not None else None
        return cls(
            key=key, value=value, metadata=metadata,
            created_at=now, updated_at=now, expires_at=expires_at,
        )

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(UTC)) >= self.expires_at

    def with_version(self, version: int) -> MemoryEntry:
        return replace(
            self, metadata=self.metadata.with_version(version), updated_at=datetime.now(UTC)
        )

    def with_checksum(self, checksum: str) -> MemoryEntry:
        return replace(self, checksum=checksum)

    def with_value(self, value: Any) -> MemoryEntry:
        return replace(self, value=value, updated_at=datetime.now(UTC))

    def with_expires_at(self, expires_at: datetime | None) -> MemoryEntry:
        return replace(self, expires_at=expires_at)
