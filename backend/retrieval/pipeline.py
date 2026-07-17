"""RetrievalPipeline — end-to-end retrieval orchestration."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from backend.retrieval.context import RetrievalContext
from backend.retrieval.exceptions import RetrievalProviderError
from backend.retrieval.models import (
    DocumentSource,
    RetrievalMetadata,
    RetrievalResult,
    RetrievalStrategy,
)
from backend.retrieval.optimizer import ContextOptimizer
from backend.retrieval.providers import EmbeddingProvider, RetrievalCache, RetrievalProvider


@dataclass(slots=True)
class RetrievalPipeline:
    """Orchestrates the query embedding → retrieval → optimization flow.

    Execution steps:
        1. Check pipeline-level cache; return cached results if present.
        2. Embed the query using ``embedding_provider`` when provided and the
           strategy requires a query vector.
        3. Call ``provider.retrieve(context, embedding)``.
        4. Optimize results with ``optimizer`` when provided.
        5. Store results in cache when ``context.use_cache`` is True.
        6. Return ``(results, RetrievalMetadata)``.

    Caching is keyed on a hash of the query, strategy, top_k, filters, and
    tenant_id so that structurally identical requests hit the same cache entry.
    """

    provider: RetrievalProvider
    embedding_provider: EmbeddingProvider | None = None
    optimizer: ContextOptimizer | None = None
    cache: RetrievalCache | None = None

    async def run(
        self,
        context: RetrievalContext,
    ) -> tuple[list[RetrievalResult], RetrievalMetadata]:
        """Execute the retrieval pipeline and return ranked results with metadata."""
        start_ms = time.monotonic() * 1_000

        cache_key = self._cache_key(context)
        if context.use_cache and self.cache is not None:
            cached_raw = await self.cache.get_json(cache_key)
            if cached_raw is not None:
                results = self._dicts_to_results(cached_raw)
                latency_ms = time.monotonic() * 1_000 - start_ms
                metadata = RetrievalMetadata(
                    retrieval_id=context.retrieval_id,
                    strategy=context.strategy,
                    provider_name=self.provider.name,
                    latency_ms=latency_ms,
                    total_candidates=len(results),
                    cached=True,
                )
                return results, metadata

        embedding: list[float] | None = None
        embedding_model: str | None = None
        if (
            self.embedding_provider is not None
            and context.strategy
            in (RetrievalStrategy.VECTOR, RetrievalStrategy.HYBRID)
        ):
            try:
                embedding = await self.embedding_provider.embed_single(context.query)
                embedding_model = self.embedding_provider.name
            except Exception as exc:
                raise RetrievalProviderError(
                    f"Embedding failed for query: {exc}",
                    provider_name=getattr(self.embedding_provider, "name", "unknown"),
                    strategy=str(context.strategy),
                ) from exc

        try:
            raw_results = await self.provider.retrieve(context, embedding)
        except Exception as exc:
            raise RetrievalProviderError(
                f"Retrieval failed: {exc}",
                provider_name=self.provider.name,
                strategy=str(context.strategy),
            ) from exc

        results = (
            await self.optimizer.optimize(raw_results, context)
            if self.optimizer is not None
            else raw_results[: context.top_k]
        )

        if context.use_cache and self.cache is not None:
            raw = [self._result_to_dict(r) for r in results]
            await self.cache.set_json(
                cache_key, raw, ttl_seconds=context.cache_ttl_seconds
            )

        latency_ms = time.monotonic() * 1_000 - start_ms
        metadata = RetrievalMetadata(
            retrieval_id=context.retrieval_id,
            strategy=context.strategy,
            provider_name=self.provider.name,
            query_embedding_model=embedding_model,
            latency_ms=latency_ms,
            total_candidates=len(raw_results),
            cached=False,
            graph_depth=(
                context.graph_depth
                if context.strategy == RetrievalStrategy.GRAPH
                else None
            ),
        )
        return results, metadata

    def _cache_key(self, context: RetrievalContext) -> str:
        import hashlib

        key_data = json.dumps(
            {
                "query": context.query,
                "strategy": str(context.strategy),
                "top_k": context.top_k,
                "min_score": context.min_score,
                "tenant_id": context.tenant_id,
                "filters": context.filters,
                "graph_depth": context.graph_depth,
                "graph_node_types": sorted(context.graph_node_types),
                "graph_relationship_types": sorted(
                    context.graph_relationship_types
                ),
            },
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(key_data.encode()).hexdigest()[:16]
        return f"sentinel:retrieval:pipeline:{digest}"

    def _result_to_dict(self, result: RetrievalResult) -> dict[str, Any]:
        return {
            "result_id": result.result_id,
            "chunk_id": result.chunk.chunk_id,
            "document_id": result.chunk.document_id,
            "content": result.chunk.content,
            "index": result.chunk.index,
            "token_count": result.chunk.token_count,
            "char_offset": result.chunk.char_offset,
            "chunk_metadata": result.chunk.metadata,
            "score": result.score,
            "source": str(result.source),
            "provenance": result.provenance,
            "metadata": result.metadata,
        }

    def _dicts_to_results(self, raw: list[dict[str, Any]]) -> list[RetrievalResult]:
        results = []
        for item in raw:
            from backend.retrieval.models import Chunk

            chunk = Chunk(
                chunk_id=item["chunk_id"],
                document_id=item["document_id"],
                content=item["content"],
                index=item["index"],
                token_count=item.get("token_count"),
                char_offset=item.get("char_offset"),
                metadata=item.get("chunk_metadata", {}),
            )
            result = RetrievalResult(
                result_id=item["result_id"],
                chunk=chunk,
                score=item["score"],
                source=DocumentSource(item["source"]),
                provenance=item.get("provenance"),
                metadata=item.get("metadata", {}),
            )
            results.append(result)
        return results
