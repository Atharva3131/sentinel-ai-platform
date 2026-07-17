"""RelationshipBuilder — creates Neo4j edges from Document nodes to graph anchors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from backend.retrieval.ingestion_exceptions import RelationshipBuildError
from backend.retrieval.ingestion_models import GraphAnchor
from backend.retrieval.ingestion_ports import Neo4jRepositoryPort

# ---------------------------------------------------------------------------
# Allowlisted relationship types for document → graph-node edges
# ---------------------------------------------------------------------------

_ALLOWED_DOCUMENT_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {"DOCUMENTS", "HAS_RUNBOOK", "OWNED_BY"}
)

# ---------------------------------------------------------------------------
# Parameterized Cypher templates
# ---------------------------------------------------------------------------

_DOCUMENTS_REL = """\
MATCH (d:Document {id: $document_id})
MATCH (target {id: $target_id})
MERGE (d)-[r:DOCUMENTS]->(target)
SET r.tenant_id  = $tenant_id,
    r.updated_at = $updated_at
RETURN type(r) AS rel_type
"""

_HAS_RUNBOOK_REL = """\
MATCH (target {id: $target_id})
MATCH (d:Document {id: $document_id})
MERGE (target)-[r:HAS_RUNBOOK]->(d)
SET r.tenant_id  = $tenant_id,
    r.updated_at = $updated_at
RETURN type(r) AS rel_type
"""

_OWNED_BY_REL = """\
MATCH (d:Document {id: $document_id})
MATCH (team {id: $target_id})
MERGE (d)-[r:OWNED_BY]->(team)
SET r.tenant_id  = $tenant_id,
    r.updated_at = $updated_at
RETURN type(r) AS rel_type
"""

_TEMPLATE_BY_TYPE: dict[str, str] = {
    "DOCUMENTS": _DOCUMENTS_REL,
    "HAS_RUNBOOK": _HAS_RUNBOOK_REL,
    "OWNED_BY": _OWNED_BY_REL,
}


@dataclass(slots=True)
class RelationshipBuilder:
    """Creates allowlisted Neo4j relationships between a Document node and anchors.

    Relationship types must belong to ``_ALLOWED_DOCUMENT_RELATIONSHIP_TYPES``.
    Each type maps to a fixed, parameterized Cypher template — callers supply
    only the node IDs; no Cypher is assembled from user input.

    ``build()`` processes anchors sequentially and collects errors rather than
    short-circuiting on the first failure. All successful relationships are
    created even if one anchor fails.
    """

    repository: Neo4jRepositoryPort

    async def build(
        self,
        document_id: str,
        anchors: tuple[GraphAnchor, ...],
        *,
        tenant_id: str | None = None,
    ) -> int:
        """Create relationships for all anchors; return the count created.

        Raises:
            RelationshipBuildError: If an anchor specifies an unsupported
                relationship type or if all relationships fail. Partial failures
                are collected and re-raised as a single error.
        """
        if not anchors:
            return 0

        unknown_types = {
            a.relationship_type for a in anchors
        } - _ALLOWED_DOCUMENT_RELATIONSHIP_TYPES
        if unknown_types:
            raise RelationshipBuildError(
                f"Relationship types not permitted for document ingestion: "
                f"{sorted(unknown_types)}. "
                f"Allowed: {sorted(_ALLOWED_DOCUMENT_RELATIONSHIP_TYPES)}",
                document_id=document_id,
            )

        now = datetime.now(UTC).isoformat()
        created = 0
        errors: list[str] = []

        for anchor in anchors:
            template = _TEMPLATE_BY_TYPE[anchor.relationship_type]
            params: dict[str, Any] = {
                "document_id": document_id,
                "target_id": anchor.target_node_id,
                "tenant_id": tenant_id or "",
                "updated_at": now,
            }
            try:
                await self.repository.write(template, params)
                created += 1
            except Exception as exc:
                errors.append(
                    f"{anchor.relationship_type} → {anchor.target_node_id}: {exc}"
                )

        if errors and created == 0:
            raise RelationshipBuildError(
                f"All {len(errors)} relationship(s) failed for document "
                f"'{document_id}': {'; '.join(errors)}",
                document_id=document_id,
            )

        return created
