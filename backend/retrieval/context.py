"""RetrievalContext — immutable evidence envelope for a single retrieval request."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from backend.retrieval.models import RetrievalStrategy


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """Immutable specification of a single retrieval request.

    ``query`` is the natural-language or structured query string.
    ``strategy`` selects the retrieval algorithm applied by the pipeline.
    ``top_k`` is the maximum number of results returned after optimization.
    ``max_tokens`` is the context-window token budget for ContextOptimizer trimming.
    ``graph_depth`` and ``graph_node_types``/``graph_relationship_types`` are used
    exclusively by GraphRetriever; other providers ignore them.
    ``filters`` carries arbitrary provider-specific key-value constraints (e.g.,
    tenant_id, date range, anchor node ID for graph traversal).
    ``use_cache`` and ``cache_ttl_seconds`` control pipeline-level result caching.
    """

    retrieval_id: str
    query: str
    strategy: RetrievalStrategy
    top_k: int = 10
    min_score: float = 0.0
    max_tokens: int | None = None
    tenant_id: str | None = None
    workflow_id: str | None = None
    execution_id: str | None = None
    correlation_id: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    graph_depth: int = 2
    graph_node_types: tuple[str, ...] = ()
    graph_relationship_types: tuple[str, ...] = ()
    include_embeddings: bool = False
    use_cache: bool = True
    cache_ttl_seconds: float = 300.0
    deadline: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def has_timed_out(self, *, now: datetime | None = None) -> bool:
        if self.deadline is None:
            return False
        return (now or datetime.now(UTC)) >= self.deadline

    def remaining_seconds(self, *, now: datetime | None = None) -> float | None:
        if self.deadline is None:
            return None
        delta = self.deadline - (now or datetime.now(UTC))
        return max(delta.total_seconds(), 0.0)

    def with_filters(self, **kwargs: Any) -> RetrievalContext:
        return replace(self, filters={**self.filters, **kwargs})

    def with_metadata(self, **kwargs: Any) -> RetrievalContext:
        return replace(self, metadata={**self.metadata, **kwargs})
