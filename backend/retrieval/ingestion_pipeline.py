"""DocumentIngestionPipeline — end-to-end document ingestion orchestration."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from backend.retrieval.chunking import ChunkingEngine
from backend.retrieval.content_extractor import ContentExtractor
from backend.retrieval.embedding_indexer import EmbeddingIndexer
from backend.retrieval.extractor import MetadataExtractor
from backend.retrieval.factory import EmbeddingFactory
from backend.retrieval.graph_builder import KnowledgeGraphBuilder
from backend.retrieval.ingestion_exceptions import IngestionPipelineError
from backend.retrieval.ingestion_models import (
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
)
from backend.retrieval.ingestion_ports import CacheInvalidator
from backend.retrieval.models import DocumentMetadata
from backend.retrieval.processor import DocumentProcessor
from backend.retrieval.providers import DocumentStore
from backend.retrieval.relationship_builder import RelationshipBuilder
from backend.retrieval.versioning import DocumentVersionManager

_CACHE_PATTERN = "sentinel:retrieval:pipeline:*"


@dataclass(slots=True)
class DocumentIngestionPipeline:
    """Orchestrates the complete document ingestion flow.

    Stages (all optional components are skipped when None):
        1. Checksum + version check   → skip if unchanged and ``skip_if_unchanged``
        2. ContentExtractor           → bytes to text, detect format
        3. DocumentProcessor          → validate + normalize
        4. MetadataExtractor          → title, tags, topics, summary
        5. ChunkingEngine             → split into Chunks
        6. DocumentStore.upload()     → blob storage (when ``blob_upload=True``)
        7. EmbeddingFactory + embed() → generate embedding vectors
        8. EmbeddingIndexer.index()   → write chunks + vectors to vector index
        9. DocumentVersionManager.record() → persist metadata + version
       10. KnowledgeGraphBuilder      → upsert Document node in Neo4j
       11. RelationshipBuilder        → create graph edges to anchors
       12. CacheInvalidator           → invalidate stale retrieval caches

    All collaborators are injected via constructor (no side-effectful defaults).
    The pipeline is stateless and may be shared across concurrent calls.
    """

    processor: DocumentProcessor
    content_extractor: ContentExtractor
    metadata_extractor: MetadataExtractor
    chunking_engine: ChunkingEngine
    version_manager: DocumentVersionManager
    embedding_indexer: EmbeddingIndexer | None = None
    graph_builder: KnowledgeGraphBuilder | None = None
    relationship_builder: RelationshipBuilder | None = None
    document_store: DocumentStore | None = None
    embedding_factory: EmbeddingFactory | None = None
    cache_invalidator: CacheInvalidator | None = None
    default_embedding_provider_name: str | None = None

    async def run(self, request: IngestionRequest) -> IngestionResult:
        """Execute the full ingestion pipeline for a single document.

        Raises:
            IngestionPipelineError: If a non-optional stage fails.
        """
        started_at = time.monotonic()

        try:
            return await self._run(request, started_at)
        except IngestionPipelineError:
            raise
        except Exception as exc:
            raise IngestionPipelineError(
                f"Ingestion pipeline failed for '{request.document_id}': {exc}",
                document_id=request.document_id,
                stage="pipeline",
            ) from exc

    async def _run(
        self, request: IngestionRequest, started_at: float
    ) -> IngestionResult:
        raw_bytes = (
            request.content
            if isinstance(request.content, bytes)
            else request.content.encode("utf-8")
        )
        checksum = hashlib.sha256(raw_bytes).hexdigest()[:16]

        # Stage 1: version check
        try:
            version = await self.version_manager.check(
                request.document_id, checksum, tenant_id=request.tenant_id
            )
        except Exception as exc:
            raise IngestionPipelineError(
                str(exc), document_id=request.document_id, stage="versioning"
            ) from exc

        if request.skip_if_unchanged and not version.has_changed:
            return IngestionResult(
                document_id=request.document_id,
                status=IngestionStatus.SKIPPED,
                version=version,
                message="Document content unchanged",
                duration_ms=(time.monotonic() - started_at) * 1000,
            )

        # Stage 2: content extraction (bytes → text)
        try:
            text, fmt = await self.content_extractor.extract(
                raw_bytes,
                request.content_type,
                document_id=request.document_id,
            )
        except Exception as exc:
            raise IngestionPipelineError(
                str(exc), document_id=request.document_id, stage="content_extraction"
            ) from exc

        # Stage 3: DocumentProcessor (validate + normalize)
        doc_metadata = DocumentMetadata(
            document_id=request.document_id,
            source=request.source,
            content_type="text/plain",
            tenant_id=request.tenant_id,
            author=request.author,
            tags=request.tags,
        )
        try:
            document = await self.processor.process(text, doc_metadata)
        except Exception as exc:
            raise IngestionPipelineError(
                str(exc), document_id=request.document_id, stage="processing"
            ) from exc

        # Stage 4: metadata extraction
        try:
            extracted = await self.metadata_extractor.extract(
                document.content,
                content_type=request.content_type,
                hint_tags=request.tags,
                document_id=request.document_id,
            )
        except Exception as exc:
            raise IngestionPipelineError(
                str(exc), document_id=request.document_id, stage="metadata_extraction"
            ) from exc

        # Stage 5: chunking
        try:
            chunks = await self.chunking_engine.chunk(document)
        except Exception as exc:
            raise IngestionPipelineError(
                str(exc), document_id=request.document_id, stage="chunking"
            ) from exc

        # Stage 6: blob upload
        blob_uri: str | None = None
        if request.blob_upload and self.document_store is not None:
            try:
                blob_uri = await self.document_store.upload(
                    request.document_id,
                    raw_bytes,
                    content_type=request.content_type,
                )
            except Exception as exc:
                raise IngestionPipelineError(
                    f"[blob_upload] {exc}",
                    document_id=request.document_id,
                    stage="blob_upload",
                ) from exc

        # Stage 7: embedding generation
        embeddings: list[list[float]] | None = None
        if request.generate_embeddings and self.embedding_factory is not None:
            provider_name = (
                request.embedding_provider_name or self.default_embedding_provider_name
            )
            if provider_name and chunks:
                try:
                    provider = self.embedding_factory.create(provider_name)
                    embeddings = await provider.embed([c.content for c in chunks])
                except Exception as exc:
                    raise IngestionPipelineError(
                        f"[embedding] {exc}",
                        document_id=request.document_id,
                        stage="embedding",
                    ) from exc

        # Stage 8: vector index
        if self.embedding_indexer is not None and chunks:
            try:
                await self.embedding_indexer.index(
                    request.document_id,
                    chunks,
                    embeddings,
                    version=version.version,
                    tenant_id=request.tenant_id,
                )
            except Exception as exc:
                raise IngestionPipelineError(
                    str(exc),
                    document_id=request.document_id,
                    stage="embedding_index",
                ) from exc

        # Stage 9: persist metadata + version
        merged: dict[str, object] = {
            "document_id": request.document_id,
            "source": str(request.source),
            "content_type": request.content_type,
            "format": str(fmt),
            "tenant_id": request.tenant_id,
            "author": request.author or extracted.author,
            "title": extracted.title,
            "tags": list(set(request.tags) | set(extracted.tags)),
            "topics": list(extracted.topics),
            "blob_uri": blob_uri,
            "word_count": extracted.word_count,
            "chunk_count": len(chunks),
            "summary": extracted.summary,
            **request.metadata,
        }
        try:
            await self.version_manager.record(version, additional=merged)
        except Exception as exc:
            raise IngestionPipelineError(
                str(exc), document_id=request.document_id, stage="version_record"
            ) from exc

        # Stage 10: graph node upsert
        if self.graph_builder is not None:
            try:
                await self.graph_builder.upsert_document(
                    version,
                    source=str(request.source),
                    blob_uri=blob_uri,
                    title=extracted.title,
                    tenant_id=request.tenant_id,
                )
            except Exception as exc:
                raise IngestionPipelineError(
                    str(exc), document_id=request.document_id, stage="graph_build"
                ) from exc

        # Stage 11: graph relationships
        if self.relationship_builder is not None and request.graph_anchors:
            try:
                await self.relationship_builder.build(
                    request.document_id,
                    request.graph_anchors,
                    tenant_id=request.tenant_id,
                )
            except Exception as exc:
                raise IngestionPipelineError(
                    str(exc),
                    document_id=request.document_id,
                    stage="relationship_build",
                ) from exc

        # Stage 12: cache invalidation
        if self.cache_invalidator is not None:
            await self.cache_invalidator.invalidate(_CACHE_PATTERN)

        status = (
            IngestionStatus.CREATED if version.is_new else IngestionStatus.UPDATED
        )
        return IngestionResult(
            document_id=request.document_id,
            status=status,
            version=version,
            chunk_count=len(chunks),
            blob_uri=blob_uri,
            duration_ms=(time.monotonic() - started_at) * 1000,
        )
