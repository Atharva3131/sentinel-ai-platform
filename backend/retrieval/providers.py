"""Protocol interfaces for embedding, retrieval, and cache providers."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.retrieval.context import RetrievalContext
from backend.retrieval.models import RetrievalResult, RetrievalStrategy


@runtime_checkable
class RetrievalCache(Protocol):
    """Minimal cache interface used by RetrievalPipeline and GraphRetriever.

    Implementations include RedisCache (production) and in-memory fakes (tests).
    """

    async def get_json(self, key: str) -> Any | None:
        """Return a JSON-decoded value or None when the key is absent."""
        ...

    async def set_json(
        self, key: str, value: Any, *, ttl_seconds: float | None = None
    ) -> bool:
        """Serialize ``value`` to JSON and store it under ``key``."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Port for a text embedding service.

    Implementations may back this with Azure OpenAI, Hugging Face, Cohere, etc.
    Callers should obtain instances via EmbeddingFactory, not by constructing directly.
    """

    @property
    def name(self) -> str:
        """Provider identifier (e.g., ``"azure-openai"``, ``"hf-bge"``).."""
        ...

    @property
    def version(self) -> str | None:
        """Optional semver or model version string."""
        ...

    @property
    def dimensions(self) -> int:
        """Dimensionality of the embedding vectors produced."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts; returns one vector per input."""
        ...

    async def embed_single(self, text: str) -> list[float]:
        """Embed a single text string."""
        ...


@runtime_checkable
class RetrievalProvider(Protocol):
    """Port for a retrieval backend (vector store, BM25 index, etc.)."""

    @property
    def name(self) -> str:
        """Provider identifier."""
        ...

    @property
    def strategy(self) -> RetrievalStrategy:
        """The retrieval strategy this provider implements."""
        ...

    async def retrieve(
        self,
        context: RetrievalContext,
        embedding: list[float] | None = None,
    ) -> list[RetrievalResult]:
        """Return ranked results for the given context.

        ``embedding`` is the pre-computed query embedding; providers that do not
        need it (e.g. keyword search) may ignore it.
        """
        ...


@runtime_checkable
class DocumentStore(Protocol):
    """Port for binary document storage (e.g., Azure Blob Storage)."""

    async def upload(
        self,
        document_id: str,
        content: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Store raw document bytes and return the storage URI."""
        ...

    async def download(self, document_id: str) -> bytes:
        """Retrieve raw document bytes by ID."""
        ...

    async def delete(self, document_id: str) -> bool:
        """Delete a document; return True if it existed."""
        ...

    async def exists(self, document_id: str) -> bool:
        """Return True if the document exists in the store."""
        ...


@runtime_checkable
class DocumentMetadataStore(Protocol):
    """Port for structured document metadata storage (e.g., PostgreSQL).

    Stores and queries DocumentMetadata rows for provenance, freshness,
    and filter-based retrieval routing.
    """

    async def upsert(self, document_id: str, metadata: dict[str, Any]) -> None:
        """Insert or update document metadata."""
        ...

    async def get(self, document_id: str) -> dict[str, Any] | None:
        """Return metadata for a single document or None if not found."""
        ...

    async def delete(self, document_id: str) -> bool:
        """Delete metadata row; return True if it existed."""
        ...

    async def query(
        self,
        *,
        tenant_id: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return metadata rows matching the given filters."""
        ...
