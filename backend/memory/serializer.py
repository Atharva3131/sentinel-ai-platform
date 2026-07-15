"""MemoryEntry serialization and deserialization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.memory.exceptions import MemorySerializationError
from backend.memory.models import MemoryClassification, MemoryEntry, MemoryMetadata, MemoryScope


@dataclass(slots=True)
class MemorySerializer:
    """Serialize and deserialize MemoryEntry to/from JSON-compatible dicts."""

    def serialize(self, entry: MemoryEntry) -> dict[str, Any]:
        try:
            return {
                "key": entry.key,
                "value": entry.value,
                "metadata": {
                    "scope": str(entry.metadata.scope),
                    "scope_id": entry.metadata.scope_id,
                    "classification": str(entry.metadata.classification),
                    "source_references": list(entry.metadata.source_references),
                    "tags": list(entry.metadata.tags),
                    "redacted": entry.metadata.redacted,
                    "version": entry.metadata.version,
                    "ttl_seconds": entry.metadata.ttl_seconds,
                },
                "created_at": entry.created_at.isoformat(),
                "updated_at": entry.updated_at.isoformat(),
                "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
                "checksum": entry.checksum,
            }
        except Exception as exc:
            raise MemorySerializationError(f"Failed to serialize entry '{entry.key}'") from exc

    def deserialize(self, data: dict[str, Any]) -> MemoryEntry:
        try:
            raw_meta = data["metadata"]
            metadata = MemoryMetadata(
                scope=MemoryScope(raw_meta["scope"]),
                scope_id=raw_meta["scope_id"],
                classification=MemoryClassification(raw_meta.get("classification", "normal")),
                source_references=tuple(raw_meta.get("source_references", [])),
                tags=tuple(raw_meta.get("tags", [])),
                redacted=bool(raw_meta.get("redacted", False)),
                version=int(raw_meta.get("version", 1)),
                ttl_seconds=raw_meta.get("ttl_seconds"),
            )
            expires_at_raw = data.get("expires_at")
            return MemoryEntry(
                key=data["key"],
                value=data["value"],
                metadata=metadata,
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
                expires_at=datetime.fromisoformat(expires_at_raw) if expires_at_raw else None,
                checksum=data.get("checksum"),
            )
        except Exception as exc:
            raise MemorySerializationError("Failed to deserialize memory entry") from exc

    def compute_checksum(self, value: Any) -> str:
        raw = json.dumps(value, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_json(self, entry: MemoryEntry) -> str:
        return json.dumps(self.serialize(entry), sort_keys=True, default=str)

    def from_json(self, raw: str) -> MemoryEntry:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MemorySerializationError("Failed to parse memory entry JSON") from exc
        return self.deserialize(data)
