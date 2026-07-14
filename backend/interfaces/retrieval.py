"""Vector and evidence retrieval contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.interfaces.common import ProviderContext


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """Provider-neutral retrieval request."""

    query: str
    top_k: int = 10
    filters: dict[str, Any] = field(default_factory=dict)
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    """Normalized retrieval hit with provenance metadata."""

    identifier: str
    source: str
    content: str
    score: float
    uri: str | None = None
    chunk_index: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievalResponse:
    """Provider-neutral retrieval response."""

    hits: tuple[RetrievalHit, ...]
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Retriever(Protocol):
    """Contract implemented by evidence and vector retrievers."""

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        """Retrieve ranked evidence for a query."""
        ...

