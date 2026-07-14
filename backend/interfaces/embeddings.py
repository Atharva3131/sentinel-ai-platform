"""Embedding provider contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.interfaces.common import ProviderContext, ProviderUsage


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    """Provider-neutral embedding request."""

    inputs: tuple[str, ...]
    model: str | None = None
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    """Provider-neutral embedding result."""

    vectors: tuple[tuple[float, ...], ...]
    model: str | None = None
    dimensions: int | None = None
    usage: ProviderUsage | None = None
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Contract implemented by all embedding providers."""

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Compute embeddings for the supplied text inputs."""
        ...

