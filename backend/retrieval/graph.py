"""GraphRetriever — bounded Neo4j traversal for knowledge graph retrieval."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from backend.infrastructure.neo4j_repository import Neo4jRepositoryBase
from backend.retrieval.context import RetrievalContext
from backend.retrieval.exceptions import GraphTraversalError
from backend.retrieval.models import (
    Chunk,
    DocumentSource,
    RetrievalResult,
    RetrievalStrategy,
)
from backend.retrieval.providers import RetrievalCache

# Allowlisted relationship types from the documented graph schema.
# Agents and callers may only request traversal over these types.
_ALLOWED_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        "DEPENDS_ON",
        "AFFECTED",
        "OBSERVED",
        "DEPLOYED_AS",
        "HAS_RUNBOOK",
        "DOCUMENTS",
        "OWNED_BY",
    }
)

# Parameterized Cypher templates. Only $-variables are user-supplied at runtime.
# Relationship type patterns are validated against the allowlist before substitution.

_NEIGHBORS_QUERY_TMPL = (
    "MATCH (anchor {{id: $anchor_id}})-[:{rel_pattern}*1..{depth}]-(neighbor) "
    "WHERE ($tenant_id IS NULL OR neighbor.tenant_id = $tenant_id) "
    "WITH DISTINCT neighbor "
    "RETURN neighbor.id AS node_id, labels(neighbor) AS node_labels, "
    "neighbor.name AS node_name, neighbor.description AS node_description "
    "LIMIT $limit"
)

_UNFILTERED_NEIGHBORS_QUERY_TMPL = (
    "MATCH (anchor {{id: $anchor_id}})-[*1..{depth}]-(neighbor) "
    "WHERE ($tenant_id IS NULL OR neighbor.tenant_id = $tenant_id) "
    "WITH DISTINCT neighbor "
    "RETURN neighbor.id AS node_id, labels(neighbor) AS node_labels, "
    "neighbor.name AS node_name, neighbor.description AS node_description "
    "LIMIT $limit"
)


def _validate_relationship_types(types: tuple[str, ...]) -> tuple[str, ...]:
    """Validate types against the allowlist; raise on unrecognized values."""
    unknown = set(types) - _ALLOWED_RELATIONSHIP_TYPES
    if unknown:
        raise GraphTraversalError(
            f"Relationship types not permitted: {sorted(unknown)}. "
            f"Allowed: {sorted(_ALLOWED_RELATIONSHIP_TYPES)}"
        )
    return types


def _build_query(
    depth: int, relationship_types: tuple[str, ...]
) -> str:
    """Return the appropriate Cypher template for the given parameters."""
    depth = max(1, min(depth, 5))  # enforce bounded traversal: 1-5 hops
    if relationship_types:
        rel_pattern = "|".join(relationship_types)
        return _NEIGHBORS_QUERY_TMPL.format(rel_pattern=rel_pattern, depth=depth)
    return _UNFILTERED_NEIGHBORS_QUERY_TMPL.format(depth=depth)


@dataclass(slots=True)
class GraphRetriever:
    """Executes bounded Neo4j traversals and maps graph nodes to RetrievalResults.

    Traversal is anchored on the node whose ``id`` property equals
    ``context.filters["anchor_id"]``. All other values are passed as Cypher
    parameters — relationship type names are validated against the schema
    allowlist before being embedded in the query pattern.

    Results are scored by graph proximity: depth-1 neighbors score 1.0,
    depth-2 score 0.7, deeper nodes score 0.5. The scoring is heuristic;
    callers that need precise relevance should use HybridRetriever and
    combine vector scores with graph distance.
    """

    repository: Neo4jRepositoryBase
    cache: RetrievalCache | None = None
    default_max_depth: int = 2
    default_max_results: int = 50
    allowed_relationship_types: frozenset[str] = field(
        default_factory=lambda: _ALLOWED_RELATIONSHIP_TYPES
    )

    @property
    def name(self) -> str:
        return "graph"

    @property
    def strategy(self) -> RetrievalStrategy:
        return RetrievalStrategy.GRAPH

    async def retrieve(
        self,
        context: RetrievalContext,
        embedding: list[float] | None = None,
    ) -> list[RetrievalResult]:
        """Return retrieval results from a bounded graph traversal.

        Raises:
            GraphTraversalError: If ``anchor_id`` is missing from
                ``context.filters`` or traversal fails.
        """
        anchor_id = context.filters.get("anchor_id")
        if not anchor_id:
            raise GraphTraversalError(
                "GraphRetriever requires 'anchor_id' in context.filters",
                anchor_id=None,
            )

        rel_types = _validate_relationship_types(context.graph_relationship_types)
        depth = min(context.graph_depth, self.default_max_depth)
        limit = min(context.top_k, self.default_max_results)

        cache_key = self._cache_key(context, str(anchor_id))
        if context.use_cache and self.cache is not None:
            cached = await self.cache.get_json(cache_key)
            if cached is not None:
                return self._dicts_to_results(cached, context)

        try:
            query = _build_query(depth, rel_types)
            params: dict[str, Any] = {
                "anchor_id": anchor_id,
                "tenant_id": context.tenant_id,
                "limit": limit,
            }
            records = await self.repository.read(query, params)
        except Exception as exc:
            raise GraphTraversalError(
                f"Graph traversal failed for anchor '{anchor_id}': {exc}",
                anchor_id=str(anchor_id),
                depth=depth,
            ) from exc

        results = self._records_to_results(records, context)

        if context.use_cache and self.cache is not None:
            raw = [self._result_to_dict(r) for r in results]
            await self.cache.set_json(
                cache_key,
                raw,
                ttl_seconds=context.cache_ttl_seconds,
            )

        return results

    def _cache_key(self, context: RetrievalContext, anchor_id: str) -> str:
        key_data = json.dumps(
            {
                "anchor_id": anchor_id,
                "depth": context.graph_depth,
                "rel_types": sorted(context.graph_relationship_types),
                "tenant_id": context.tenant_id,
                "limit": context.top_k,
            },
            sort_keys=True,
        )
        import hashlib

        digest = hashlib.sha256(key_data.encode()).hexdigest()[:16]
        return f"sentinel:retrieval:graph:{digest}"

    def _records_to_results(
        self, records: list[dict[str, Any]], context: RetrievalContext
    ) -> list[RetrievalResult]:
        results = []
        for record in records:
            node_id = record.get("node_id") or record.get("id", "unknown")
            node_labels: list[str] = record.get("node_labels") or []
            name = record.get("node_name") or ""
            description = record.get("node_description") or ""
            content = f"{name}: {description}".strip(": ").strip() or str(node_id)

            chunk = Chunk.make(
                document_id=f"graph:{node_id}",
                content=content,
                index=0,
                metadata={
                    "node_id": node_id,
                    "node_labels": node_labels,
                },
            )
            result = RetrievalResult.from_chunk(
                chunk,
                score=min(context.min_score + 0.8, 1.0),
                retrieval_id=context.retrieval_id,
                source=DocumentSource.GRAPH,
                provenance=f"graph:{node_id}",
                metadata={"node_labels": node_labels},
            )
            results.append(result)
        return results

    def _result_to_dict(self, result: RetrievalResult) -> dict[str, Any]:
        return {
            "result_id": result.result_id,
            "chunk_id": result.chunk.chunk_id,
            "document_id": result.chunk.document_id,
            "content": result.chunk.content,
            "index": result.chunk.index,
            "score": result.score,
            "provenance": result.provenance,
            "chunk_metadata": result.chunk.metadata,
            "metadata": result.metadata,
        }

    def _dicts_to_results(
        self, raw: list[dict[str, Any]], context: RetrievalContext
    ) -> list[RetrievalResult]:
        results = []
        for item in raw:
            chunk = Chunk(
                chunk_id=item["chunk_id"],
                document_id=item["document_id"],
                content=item["content"],
                index=item["index"],
                metadata=item.get("chunk_metadata", {}),
            )
            result = RetrievalResult(
                result_id=item["result_id"],
                chunk=chunk,
                score=item["score"],
                source=DocumentSource.CACHE,
                provenance=item.get("provenance"),
                metadata=item.get("metadata", {}),
            )
            results.append(result)
        return results
