"""EmbeddingIndexer — stores chunk embeddings in a vector index."""

from __future__ import annotations

from dataclasses import dataclass

from backend.retrieval.ingestion_exceptions import EmbeddingIndexError
from backend.retrieval.ingestion_ports import ChunkRecord, VectorIndexPort
from backend.retrieval.models import Chunk


@dataclass(slots=True)
class EmbeddingIndexer:
    """Writes Chunk records and their embeddings to a VectorIndexPort.

    Incremental indexing:
        Before inserting new chunks, all existing chunks for the document are
        deleted via ``VectorIndexPort.delete_by_document()``. This ensures stale
        chunks from previous versions are removed regardless of whether the
        document was split into more or fewer chunks.

    Embedding alignment:
        When ``embeddings`` is provided it must have the same length as
        ``chunks`` (one vector per chunk). When it is None, ``ChunkRecord``
        objects are stored without embedding vectors; downstream vector search
        will skip them until re-indexed with embeddings.
    """

    vector_index: VectorIndexPort

    async def index(
        self,
        document_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]] | None,
        *,
        version: int = 1,
        tenant_id: str | None = None,
    ) -> int:
        """Index chunks; return the count stored.

        Raises:
            EmbeddingIndexError: If embeddings length mismatches chunks, or if
                the vector index operation fails.
        """
        if embeddings is not None and len(embeddings) != len(chunks):
            raise EmbeddingIndexError(
                f"Embedding count ({len(embeddings)}) does not match "
                f"chunk count ({len(chunks)}) for document '{document_id}'",
                document_id=document_id,
            )

        try:
            await self.vector_index.delete_by_document(document_id)
        except Exception as exc:
            raise EmbeddingIndexError(
                f"Failed to delete stale chunks for document '{document_id}': {exc}",
                document_id=document_id,
            ) from exc

        records = [
            ChunkRecord(
                chunk_id=chunk.chunk_id,
                document_id=chunk.document_id,
                content=chunk.content,
                index=chunk.index,
                version=version,
                embedding=tuple(embeddings[i]) if embeddings is not None else None,
                token_count=chunk.token_count,
                metadata={
                    **chunk.metadata,
                    **({"tenant_id": tenant_id} if tenant_id else {}),
                },
            )
            for i, chunk in enumerate(chunks)
        ]

        try:
            return await self.vector_index.upsert_chunks(document_id, records)
        except Exception as exc:
            raise EmbeddingIndexError(
                f"Failed to upsert chunks for document '{document_id}': {exc}",
                document_id=document_id,
            ) from exc
