"""KnowledgeManager — top-level coordinator for the retrieval subsystem."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.retrieval.chunking import ChunkingEngine
from backend.retrieval.context import RetrievalContext
from backend.retrieval.exceptions import KnowledgeManagerError
from backend.retrieval.factory import EmbeddingFactory
from backend.retrieval.models import (
    Chunk,
    Document,
    DocumentMetadata,
    RetrievalMetadata,
    RetrievalResult,
)
from backend.retrieval.optimizer import ContextOptimizer
from backend.retrieval.pipeline import RetrievalPipeline
from backend.retrieval.processor import DocumentProcessor


@dataclass(slots=True)
class KnowledgeManager:
    """Coordinates document ingestion and retrieval across the subsystem.

    Ingest path:
        raw content → DocumentProcessor → Document → ChunkingEngine → list[Chunk]
        Optionally: generate embeddings per Chunk via EmbeddingFactory.

    Retrieve path:
        RetrievalContext → RetrievalPipeline.run() → (results, RetrievalMetadata)
        A default pipeline is used when none is supplied; callers can inject a
        custom pipeline (e.g., with a different provider) per request.

    Dependency injection:
        All collaborators are passed via constructor. The manager does not create
        infrastructure resources (Redis, Neo4j, embedding clients) itself.
    """

    processor: DocumentProcessor
    chunking_engine: ChunkingEngine
    embedding_factory: EmbeddingFactory
    optimizer: ContextOptimizer = field(default_factory=ContextOptimizer)
    default_pipeline: RetrievalPipeline | None = None
    default_embedding_provider_name: str | None = None

    async def ingest(
        self,
        content: bytes | str,
        metadata: DocumentMetadata,
        *,
        embedding_provider_name: str | None = None,
        generate_embeddings: bool = False,
    ) -> tuple[Document, list[Chunk]]:
        """Process raw content and split into chunks.

        Args:
            content: Raw bytes or UTF-8 text.
            metadata: Provenance envelope for the document.
            embedding_provider_name: Override the default embedding provider.
                Ignored when ``generate_embeddings=False``.
            generate_embeddings: When True, embed each chunk via the resolved
                provider and attach the vector to the Chunk (as a new Chunk with
                the embedding field populated).

        Returns:
            ``(document, chunks)`` — the processed Document and its Chunks.

        Raises:
            KnowledgeManagerError: If processing or chunking fails.
            DocumentProcessingError: If the content fails validation.
            ChunkingError: If text splitting fails.
        """
        try:
            document = await self.processor.process(content, metadata)
            chunks = await self.chunking_engine.chunk(document)
        except Exception as exc:
            raise KnowledgeManagerError(
                f"Ingestion failed for document '{metadata.document_id}': {exc}"
            ) from exc

        if generate_embeddings and chunks:
            provider_name = (
                embedding_provider_name or self.default_embedding_provider_name
            )
            if provider_name is None:
                raise KnowledgeManagerError(
                    "generate_embeddings=True but no embedding provider configured. "
                    "Pass embedding_provider_name or set default_embedding_provider_name."
                )
            provider = self.embedding_factory.create(provider_name)
            texts = [c.content for c in chunks]
            vectors = await provider.embed(texts)
            chunks = [
                Chunk(
                    chunk_id=c.chunk_id,
                    document_id=c.document_id,
                    content=c.content,
                    index=c.index,
                    token_count=c.token_count,
                    char_offset=c.char_offset,
                    metadata=c.metadata,
                    embedding=tuple(v),
                )
                for c, v in zip(chunks, vectors, strict=True)
            ]

        return document, chunks

    async def retrieve(
        self,
        context: RetrievalContext,
        *,
        pipeline: RetrievalPipeline | None = None,
    ) -> tuple[list[RetrievalResult], RetrievalMetadata]:
        """Run the retrieval pipeline and return ranked results.

        Args:
            context: Retrieval request specification.
            pipeline: Override the default pipeline for this request.

        Returns:
            ``(results, metadata)`` — ranked RetrievalResults and observability
            metadata for the pipeline run.

        Raises:
            KnowledgeManagerError: If no pipeline is available.
            RetrievalProviderError: If the provider fails.
        """
        active_pipeline = pipeline or self.default_pipeline
        if active_pipeline is None:
            raise KnowledgeManagerError(
                "No RetrievalPipeline configured. Pass a pipeline argument or set "
                "default_pipeline on KnowledgeManager."
            )
        return await active_pipeline.run(context)
