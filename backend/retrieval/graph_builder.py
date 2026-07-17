"""KnowledgeGraphBuilder — upserts Document nodes in the Neo4j knowledge graph."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.retrieval.ingestion_exceptions import GraphBuildError
from backend.retrieval.ingestion_models import DocumentVersion
from backend.retrieval.ingestion_ports import KnowledgeEventPublisher, Neo4jRepositoryPort

# ---------------------------------------------------------------------------
# Parameterized Cypher templates — never assembled from user-supplied strings
# ---------------------------------------------------------------------------

_UPSERT_DOCUMENT_NODE = """\
MERGE (d:Document {id: $document_id})
SET d.source      = $source,
    d.blob_uri    = $blob_uri,
    d.checksum    = $checksum,
    d.version     = $version,
    d.tenant_id   = $tenant_id,
    d.title       = $title,
    d.updated_at  = $updated_at
RETURN d.id AS document_id
"""

_GET_DOCUMENT_NODE = """\
MATCH (d:Document {id: $document_id})
RETURN d.id AS document_id,
       d.checksum AS checksum,
       d.version AS version,
       d.source AS source,
       d.blob_uri AS blob_uri,
       d.title AS title,
       d.updated_at AS updated_at
"""

_DELETE_DOCUMENT_NODE = """\
MATCH (d:Document {id: $document_id})
DETACH DELETE d
"""

_KNOWLEDGE_EVENT_GRAPH_UPDATED = "knowledge.graph.document.updated"
_KNOWLEDGE_EVENT_GRAPH_DELETED = "knowledge.graph.document.deleted"


@dataclass(slots=True)
class KnowledgeGraphBuilder:
    """Creates and maintains Document nodes in the Neo4j knowledge graph.

    Each ingested document becomes a ``Document`` node with provenance
    properties (blob_uri, checksum, version, source). Relationships to other
    node types (Service, Component, etc.) are created separately by
    RelationshipBuilder.

    Graph mutations emit knowledge events via the optional publisher so that
    downstream consumers (cache invalidators, search updaters) can react
    without polling Neo4j.
    """

    repository: Neo4jRepositoryPort
    event_publisher: KnowledgeEventPublisher | None = None

    async def upsert_document(
        self,
        version: DocumentVersion,
        *,
        source: str,
        blob_uri: str | None = None,
        title: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        """Create or update a Document node.

        Raises:
            GraphBuildError: If the Neo4j write fails.
        """
        params: dict[str, Any] = {
            "document_id": version.document_id,
            "source": source,
            "blob_uri": blob_uri or "",
            "checksum": version.checksum,
            "version": version.version,
            "tenant_id": tenant_id or "",
            "title": title or "",
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            await self.repository.write(_UPSERT_DOCUMENT_NODE, params)
        except Exception as exc:
            raise GraphBuildError(
                f"Failed to upsert Document node '{version.document_id}': {exc}",
                document_id=version.document_id,
                node_type="Document",
            ) from exc

        if self.event_publisher is not None:
            await self.event_publisher.publish(
                _KNOWLEDGE_EVENT_GRAPH_UPDATED,
                {
                    "document_id": version.document_id,
                    "version": version.version,
                    "source": source,
                },
                tenant_id=tenant_id,
            )

    async def get_document(self, document_id: str) -> dict[str, Any] | None:
        """Return Document node properties or None if not found.

        Raises:
            GraphBuildError: If the Neo4j read fails.
        """
        try:
            return await self.repository.single(
                _GET_DOCUMENT_NODE, {"document_id": document_id}
            )
        except Exception as exc:
            raise GraphBuildError(
                f"Failed to read Document node '{document_id}': {exc}",
                document_id=document_id,
                node_type="Document",
            ) from exc

    async def delete_document(self, document_id: str) -> None:
        """Remove a Document node and all its relationships.

        Raises:
            GraphBuildError: If the Neo4j write fails.
        """
        try:
            await self.repository.write(
                _DELETE_DOCUMENT_NODE, {"document_id": document_id}
            )
        except Exception as exc:
            raise GraphBuildError(
                f"Failed to delete Document node '{document_id}': {exc}",
                document_id=document_id,
                node_type="Document",
            ) from exc

        if self.event_publisher is not None:
            await self.event_publisher.publish(
                _KNOWLEDGE_EVENT_GRAPH_DELETED,
                {"document_id": document_id},
            )
