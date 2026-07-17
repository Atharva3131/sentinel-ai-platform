"""DocumentVersionManager — tracks document versions and drives incremental indexing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.retrieval.ingestion_exceptions import VersioningError
from backend.retrieval.ingestion_models import DocumentVersion
from backend.retrieval.providers import DocumentMetadataStore


@dataclass(slots=True)
class DocumentVersionManager:
    """Reads and writes document version state via DocumentMetadataStore.

    Version check (``check()``) is a read-only probe: it returns the version
    state without persisting it. Callers call ``record()`` after a successful
    ingestion to commit the new version. This two-phase pattern lets the pipeline
    skip heavy work (embedding, graph update) before touching the store.

    Version numbers are monotonically increasing integers. The first ingestion
    produces version 1. Unchanged documents (same checksum) stay on their
    current version; the pipeline can use ``DocumentVersion.has_changed`` to
    decide whether to re-index.
    """

    metadata_store: DocumentMetadataStore

    async def check(
        self,
        document_id: str,
        checksum: str,
        *,
        tenant_id: str | None = None,
    ) -> DocumentVersion:
        """Return a DocumentVersion describing the current state of the document.

        Raises:
            VersioningError: If the metadata store raises unexpectedly.
        """
        try:
            existing = await self.metadata_store.get(document_id)
        except Exception as exc:
            raise VersioningError(
                f"Failed to read version state for document '{document_id}': {exc}",
                document_id=document_id,
            ) from exc

        if existing is None:
            return DocumentVersion(
                document_id=document_id,
                version=1,
                checksum=checksum,
                is_new=True,
            )

        prev_checksum: str | None = existing.get("checksum")
        prev_version: int = int(existing.get("version", 1))

        if prev_checksum == checksum:
            return DocumentVersion(
                document_id=document_id,
                version=prev_version,
                checksum=checksum,
                previous_version=prev_version,
                previous_checksum=prev_checksum,
                is_new=False,
            )

        return DocumentVersion(
            document_id=document_id,
            version=prev_version + 1,
            checksum=checksum,
            previous_version=prev_version,
            previous_checksum=prev_checksum,
            is_new=False,
        )

    async def record(
        self,
        version: DocumentVersion,
        *,
        additional: dict[str, Any] | None = None,
    ) -> None:
        """Persist the version record to the metadata store.

        ``additional`` is merged into the stored document so the pipeline can
        co-locate extracted metadata (title, tags, blob_uri, etc.) alongside
        the version fields without a second round-trip.

        Raises:
            VersioningError: If the upsert fails.
        """
        payload: dict[str, Any] = {
            "document_id": version.document_id,
            "version": version.version,
            "checksum": version.checksum,
            "previous_version": version.previous_version,
            "previous_checksum": version.previous_checksum,
            "is_new": version.is_new,
            "created_at": version.created_at.isoformat(),
        }
        if additional:
            payload.update(additional)
        try:
            await self.metadata_store.upsert(version.document_id, payload)
        except Exception as exc:
            raise VersioningError(
                f"Failed to persist version for document '{version.document_id}': {exc}",
                document_id=version.document_id,
            ) from exc
