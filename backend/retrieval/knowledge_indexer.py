"""KnowledgeIndexer — top-level entry point for the knowledge ingestion subsystem."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from backend.retrieval.ingestion_exceptions import IngestionPipelineError
from backend.retrieval.ingestion_models import (
    DocumentFormat,
    GraphAnchor,
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
)
from backend.retrieval.ingestion_pipeline import DocumentIngestionPipeline
from backend.retrieval.models import DocumentSource

_CONTENT_TYPE_BY_FORMAT: dict[DocumentFormat, str] = {
    DocumentFormat.PDF: "application/pdf",
    DocumentFormat.MARKDOWN: "text/markdown",
    DocumentFormat.JSON: "application/json",
    DocumentFormat.TEXT: "text/plain",
    DocumentFormat.HTML: "text/html",
    DocumentFormat.UNKNOWN: "text/plain",
}


@dataclass(slots=True)
class KnowledgeIndexer:
    """Top-level coordinator for single and batch document ingestion.

    ``ingest()`` builds an IngestionRequest and delegates to the pipeline.
    ``ingest_batch()`` fans out requests with bounded concurrency so a large
    corpus does not exhaust connection pools or event loop resources.

    Error isolation:
        In batch mode, one failing document produces a FAILED IngestionResult
        and does not abort the remaining batch.
    """

    pipeline: DocumentIngestionPipeline
    default_concurrency: int = 4

    async def ingest(
        self,
        content: bytes | str,
        *,
        document_id: str,
        content_type: str,
        source: DocumentSource = DocumentSource.INLINE,
        tenant_id: str | None = None,
        author: str | None = None,
        tags: tuple[str, ...] = (),
        graph_anchors: tuple[GraphAnchor, ...] = (),
        embedding_provider_name: str | None = None,
        generate_embeddings: bool = False,
        skip_if_unchanged: bool = True,
        blob_upload: bool = True,
        metadata: dict[str, object] | None = None,
    ) -> IngestionResult:
        """Ingest a single document.

        Raises:
            IngestionPipelineError: If a non-optional pipeline stage fails.
        """
        request = IngestionRequest(
            document_id=document_id,
            content=content,
            content_type=content_type,
            source=source,
            tenant_id=tenant_id,
            author=author,
            tags=tags,
            graph_anchors=graph_anchors,
            embedding_provider_name=embedding_provider_name,
            generate_embeddings=generate_embeddings,
            skip_if_unchanged=skip_if_unchanged,
            blob_upload=blob_upload,
            metadata=metadata or {},
        )
        return await self.pipeline.run(request)

    async def ingest_batch(
        self,
        requests: list[IngestionRequest],
        *,
        concurrency: int | None = None,
    ) -> list[IngestionResult]:
        """Ingest a list of documents with bounded concurrency.

        Results are returned in the same order as ``requests``.
        A failed document produces a FAILED IngestionResult; other documents
        in the batch continue unaffected.
        """
        semaphore = asyncio.Semaphore(concurrency or self.default_concurrency)

        async def _run_one(request: IngestionRequest) -> IngestionResult:
            async with semaphore:
                try:
                    return await self.pipeline.run(request)
                except IngestionPipelineError as exc:
                    return IngestionResult(
                        document_id=request.document_id,
                        status=IngestionStatus.FAILED,
                        message=str(exc),
                    )
                except Exception as exc:
                    return IngestionResult(
                        document_id=request.document_id,
                        status=IngestionStatus.FAILED,
                        message=f"Unexpected error: {exc}",
                    )

        return list(await asyncio.gather(*[_run_one(r) for r in requests]))
