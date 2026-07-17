"""Tests for backend/retrieval/ — Knowledge Engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from backend.retrieval.chunking import ChunkingEngine
from backend.retrieval.context import RetrievalContext
from backend.retrieval.exceptions import (
    DocumentProcessingError,
    EmbeddingError,
    GraphTraversalError,
    KnowledgeManagerError,
    RetrievalProviderError,
)
from backend.retrieval.factory import EmbeddingFactory
from backend.retrieval.graph import GraphRetriever
from backend.retrieval.hybrid import HybridRetriever
from backend.retrieval.manager import KnowledgeManager
from backend.retrieval.models import (
    Chunk,
    ChunkStrategy,
    Document,
    DocumentMetadata,
    DocumentSource,
    RetrievalMetadata,
    RetrievalResult,
    RetrievalStrategy,
)
from backend.retrieval.optimizer import ContextOptimizer, _estimate_tokens
from backend.retrieval.pipeline import RetrievalPipeline
from backend.retrieval.processor import DocumentProcessor

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeEmbeddingProvider:
    _name: str = "fake-embedder"
    _version: str | None = "1.0.0"
    _dimensions: int = 4
    _fail: bool = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def version(self) -> str | None:
        return self._version

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self._fail:
            raise RuntimeError("embed failure")
        return [[float(i % 4) for i in range(self._dimensions)] for _ in texts]

    async def embed_single(self, text: str) -> list[float]:
        if self._fail:
            raise RuntimeError("embed_single failure")
        return [float(i % 4) for i in range(self._dimensions)]


@dataclass(slots=True)
class FakeRetrievalProvider:
    _name: str = "fake-provider"
    _strategy: RetrievalStrategy = RetrievalStrategy.VECTOR
    results: list[RetrievalResult] = field(default_factory=list)
    _fail: bool = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def strategy(self) -> RetrievalStrategy:
        return self._strategy

    async def retrieve(
        self,
        context: RetrievalContext,
        embedding: list[float] | None = None,
    ) -> list[RetrievalResult]:
        if self._fail:
            raise RuntimeError("retrieval failure")
        return self.results


@dataclass(slots=True)
class FakeNeo4jRepositoryBase:
    records: list[dict[str, Any]] = field(default_factory=list)
    _fail: bool = False

    async def read(
        self,
        query: str,
        parameters: Any = None,
        *,
        database: Any = None,
    ) -> list[dict[str, Any]]:
        if self._fail:
            raise RuntimeError("neo4j failure")
        return self.records

    async def write(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def single(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        return self.records[0] if self.records else None


@dataclass(slots=True)
class FakeRedisCache:
    _store: dict[str, Any] = field(default_factory=dict)

    async def get_json(self, key: str) -> Any | None:
        return self._store.get(key)

    async def set_json(
        self, key: str, value: Any, *, ttl_seconds: float | None = None
    ) -> bool:
        self._store[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, *, ttl_seconds: float | None = None) -> bool:
        return True

    async def delete(self, *keys: str) -> int:
        return 0

    async def exists(self, *keys: str) -> bool:
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metadata(
    doc_id: str = "doc-1", source: DocumentSource = DocumentSource.INLINE
) -> DocumentMetadata:
    return DocumentMetadata(document_id=doc_id, source=source)


def _context(
    query: str = "test query",
    strategy: RetrievalStrategy = RetrievalStrategy.VECTOR,
    top_k: int = 5,
    **kwargs: Any,
) -> RetrievalContext:
    return RetrievalContext(
        retrieval_id="ret-1",
        query=query,
        strategy=strategy,
        top_k=top_k,
        **kwargs,
    )


def _chunk(
    content: str = "hello world",
    index: int = 0,
    doc_id: str = "doc-1",
) -> Chunk:
    return Chunk.make(doc_id, content, index)


def _result(
    score: float = 0.9,
    content: str = "hello world",
    retrieval_id: str = "ret-1",
    source: DocumentSource = DocumentSource.INLINE,
) -> RetrievalResult:
    chunk = _chunk(content)
    return RetrievalResult.from_chunk(
        chunk,
        score,
        retrieval_id=retrieval_id,
        source=source,
    )


# ---------------------------------------------------------------------------
# DocumentMetadata and Document
# ---------------------------------------------------------------------------


class TestDocumentModels:
    def test_document_metadata_defaults(self) -> None:
        meta = _metadata()
        assert meta.document_id == "doc-1"
        assert meta.tags == ()
        assert meta.custom == {}

    def test_document_source_propagation(self) -> None:
        meta = _metadata(source=DocumentSource.BLOB)
        doc = Document(document_id="doc-1", content="text", metadata=meta)
        assert doc.source == DocumentSource.BLOB

    def test_chunk_make(self) -> None:
        chunk = Chunk.make("doc-1", "some content", 3, char_offset=100)
        assert chunk.chunk_id == "doc-1:chunk:3"
        assert chunk.index == 3
        assert chunk.char_offset == 100
        assert chunk.token_count is not None and chunk.token_count > 0

    def test_retrieval_result_score_validation(self) -> None:
        chunk = _chunk()
        with pytest.raises(ValueError, match="score"):
            RetrievalResult(
                result_id="r1",
                chunk=chunk,
                score=1.5,
                source=DocumentSource.INLINE,
            )

    def test_retrieval_result_from_chunk(self) -> None:
        chunk = _chunk()
        result = RetrievalResult.from_chunk(
            chunk, 0.85, retrieval_id="ret-1", source=DocumentSource.CACHE
        )
        assert result.score == 0.85
        assert result.result_id == "ret-1:doc-1:chunk:0"


# ---------------------------------------------------------------------------
# RetrievalContext
# ---------------------------------------------------------------------------


class TestRetrievalContext:
    def test_defaults(self) -> None:
        ctx = _context()
        assert ctx.top_k == 5
        assert ctx.graph_depth == 2
        assert ctx.use_cache is True
        assert ctx.graph_relationship_types == ()

    def test_with_filters(self) -> None:
        ctx = _context()
        updated = ctx.with_filters(anchor_id="svc-1", tenant_id="t1")
        assert updated.filters["anchor_id"] == "svc-1"
        assert updated.filters["tenant_id"] == "t1"
        assert ctx.filters == {}

    def test_with_metadata(self) -> None:
        ctx = _context()
        updated = ctx.with_metadata(run="42")
        assert updated.metadata["run"] == "42"

    def test_has_timed_out_no_deadline(self) -> None:
        ctx = _context()
        assert ctx.has_timed_out() is False

    def test_remaining_seconds_no_deadline(self) -> None:
        ctx = _context()
        assert ctx.remaining_seconds() is None


# ---------------------------------------------------------------------------
# DocumentProcessor
# ---------------------------------------------------------------------------


class TestDocumentProcessor:
    @pytest.fixture()
    def processor(self) -> DocumentProcessor:
        return DocumentProcessor()

    @pytest.mark.asyncio()
    async def test_process_plain_text(self, processor: DocumentProcessor) -> None:
        meta = _metadata()
        doc = await processor.process("hello world", meta)
        assert doc.document_id == "doc-1"
        assert doc.content == "hello world"
        assert doc.metadata.checksum is not None
        assert doc.metadata.byte_size == 11

    @pytest.mark.asyncio()
    async def test_process_bytes(self, processor: DocumentProcessor) -> None:
        meta = _metadata()
        doc = await processor.process(b"bytes content", meta)
        assert doc.content == "bytes content"

    @pytest.mark.asyncio()
    async def test_normalizes_crlf(self, processor: DocumentProcessor) -> None:
        meta = _metadata()
        doc = await processor.process("line1\r\nline2", meta)
        assert "\r\n" not in doc.content
        assert "line1\nline2" == doc.content

    @pytest.mark.asyncio()
    async def test_rejects_oversized_content(self, processor: DocumentProcessor) -> None:
        processor.max_bytes = 5
        meta = _metadata()
        with pytest.raises(DocumentProcessingError, match="exceeds max size"):
            await processor.process("hello world", meta)

    @pytest.mark.asyncio()
    async def test_rejects_unsupported_content_type(
        self, processor: DocumentProcessor
    ) -> None:
        meta = DocumentMetadata(
            document_id="doc-1",
            source=DocumentSource.INLINE,
            content_type="application/pdf",
        )
        with pytest.raises(DocumentProcessingError, match="Unsupported content type"):
            await processor.process("hello", meta)

    @pytest.mark.asyncio()
    async def test_accepts_none_content_type(self, processor: DocumentProcessor) -> None:
        meta = _metadata()
        doc = await processor.process("anything", meta)
        assert doc.content == "anything"

    @pytest.mark.asyncio()
    async def test_normalizes_json(self, processor: DocumentProcessor) -> None:
        meta = DocumentMetadata(
            document_id="doc-1",
            source=DocumentSource.INLINE,
            content_type="application/json",
        )
        raw = '{"b": 2, "a": 1}'
        doc = await processor.process(raw, meta)
        assert '"a": 1' in doc.content
        assert '"b": 2' in doc.content
        # keys sorted — 'a' before 'b'
        assert doc.content.index('"a"') < doc.content.index('"b"')


# ---------------------------------------------------------------------------
# ChunkingEngine
# ---------------------------------------------------------------------------


class TestChunkingEngine:
    @pytest.fixture()
    def doc(self) -> Document:
        content = "First sentence. Second sentence. Third sentence."
        meta = _metadata()
        return Document(document_id="doc-1", content=content, metadata=meta)

    @pytest.mark.asyncio()
    async def test_fixed_strategy(self, doc: Document) -> None:
        engine = ChunkingEngine(strategy=ChunkStrategy.FIXED, chunk_size=10, chunk_overlap=0)
        chunks = await engine.chunk(doc)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c.content) <= 10
            assert c.document_id == "doc-1"

    @pytest.mark.asyncio()
    async def test_fixed_with_overlap(self, doc: Document) -> None:
        engine = ChunkingEngine(strategy=ChunkStrategy.FIXED, chunk_size=20, chunk_overlap=5)
        chunks = await engine.chunk(doc)
        assert all(c.char_offset is not None for c in chunks)

    @pytest.mark.asyncio()
    async def test_sentence_strategy(self, doc: Document) -> None:
        engine = ChunkingEngine(strategy=ChunkStrategy.SENTENCE, chunk_size=50)
        chunks = await engine.chunk(doc)
        assert chunks
        for c in chunks:
            assert c.document_id == "doc-1"

    @pytest.mark.asyncio()
    async def test_paragraph_strategy(self) -> None:
        content = "Para one content.\n\nPara two content.\n\nPara three."
        meta = _metadata()
        doc = Document(document_id="doc-1", content=content, metadata=meta)
        engine = ChunkingEngine(strategy=ChunkStrategy.PARAGRAPH, chunk_size=1000)
        chunks = await engine.chunk(doc)
        assert len(chunks) == 1  # all fit in one chunk at 1000 char budget

    @pytest.mark.asyncio()
    async def test_paragraph_strategy_splits(self) -> None:
        content = "A" * 100 + "\n\n" + "B" * 100
        meta = _metadata()
        doc = Document(document_id="doc-1", content=content, metadata=meta)
        engine = ChunkingEngine(strategy=ChunkStrategy.PARAGRAPH, chunk_size=50)
        chunks = await engine.chunk(doc)
        assert len(chunks) >= 2

    @pytest.mark.asyncio()
    async def test_semantic_falls_back_to_paragraph(self, doc: Document) -> None:
        engine = ChunkingEngine(strategy=ChunkStrategy.SEMANTIC, chunk_size=500)
        chunks = await engine.chunk(doc)
        assert chunks

    @pytest.mark.asyncio()
    async def test_empty_document_returns_empty(self) -> None:
        meta = _metadata()
        doc = Document(document_id="doc-1", content="   ", metadata=meta)
        engine = ChunkingEngine()
        chunks = await engine.chunk(doc)
        assert chunks == []

    @pytest.mark.asyncio()
    async def test_chunk_ids_are_unique(self, doc: Document) -> None:
        engine = ChunkingEngine(strategy=ChunkStrategy.FIXED, chunk_size=10, chunk_overlap=0)
        chunks = await engine.chunk(doc)
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# EmbeddingFactory
# ---------------------------------------------------------------------------


class TestEmbeddingFactory:
    @pytest.fixture()
    def factory(self) -> EmbeddingFactory:
        return EmbeddingFactory()

    def test_register_and_create(self, factory: EmbeddingFactory) -> None:
        factory.register(
            name="fake",
            version="1.0.0",
            provider=lambda deps: FakeEmbeddingProvider(),
        )
        p = factory.create("fake")
        assert p.name == "fake-embedder"

    def test_resolve_default_version(self, factory: EmbeddingFactory) -> None:
        factory.register(
            name="enc",
            version="1.0.0",
            provider=lambda _: FakeEmbeddingProvider(_name="enc-1"),
        )
        factory.register(
            name="enc",
            version="2.0.0",
            provider=lambda _: FakeEmbeddingProvider(_name="enc-2"),
        )
        # highest semver should be default
        version, _ = factory.resolve("enc")
        assert version == "2.0.0"

    def test_explicit_default_overrides_semver(self, factory: EmbeddingFactory) -> None:
        factory.register(
            name="enc",
            version="2.0.0",
            provider=lambda _: FakeEmbeddingProvider(_name="enc-2"),
        )
        factory.register(
            name="enc",
            version="1.0.0",
            provider=lambda _: FakeEmbeddingProvider(_name="enc-1"),
            default=True,
        )
        version, _ = factory.resolve("enc")
        assert version == "1.0.0"

    def test_override_flag(self, factory: EmbeddingFactory) -> None:
        factory.register(
            name="enc",
            version="1.0.0",
            provider=lambda _: FakeEmbeddingProvider(_name="v1"),
        )
        factory.register(
            name="enc",
            version="1.0.0",
            provider=lambda _: FakeEmbeddingProvider(_name="v1-patched"),
            override=True,
        )
        p = factory.create("enc", "1.0.0")
        assert p.name == "v1-patched"

    def test_duplicate_registration_raises(self, factory: EmbeddingFactory) -> None:
        factory.register(
            name="enc",
            version="1.0.0",
            provider=lambda _: FakeEmbeddingProvider(),
        )
        with pytest.raises(EmbeddingError, match="already registered"):
            factory.register(
                name="enc",
                version="1.0.0",
                provider=lambda _: FakeEmbeddingProvider(),
            )

    def test_resolve_unknown_name_raises(self, factory: EmbeddingFactory) -> None:
        with pytest.raises(EmbeddingError, match="not registered"):
            factory.resolve("no-such-provider")

    def test_names_and_versions(self, factory: EmbeddingFactory) -> None:
        factory.register(
            name="a",
            version="1.0.0",
            provider=lambda _: FakeEmbeddingProvider(),
        )
        factory.register(
            name="a",
            version="2.0.0",
            provider=lambda _: FakeEmbeddingProvider(),
        )
        assert "a" in factory.names()
        assert "1.0.0" in factory.versions("a")
        assert "2.0.0" in factory.versions("a")


# ---------------------------------------------------------------------------
# ContextOptimizer
# ---------------------------------------------------------------------------


class TestContextOptimizer:
    @pytest.fixture()
    def optimizer(self) -> ContextOptimizer:
        return ContextOptimizer(max_tokens=200, dedup_threshold=0.92)

    @pytest.fixture()
    def ctx(self) -> RetrievalContext:
        return _context(top_k=10, min_score=0.0)

    @pytest.mark.asyncio()
    async def test_empty_results(self, optimizer: ContextOptimizer, ctx: RetrievalContext) -> None:
        results = await optimizer.optimize([], ctx)
        assert results == []

    @pytest.mark.asyncio()
    async def test_sorts_by_score(self, optimizer: ContextOptimizer, ctx: RetrievalContext) -> None:
        r1 = _result(score=0.5, content="alpha content here")
        r2 = _result(score=0.9, content="beta content here different")
        results = await optimizer.optimize([r1, r2], ctx)
        assert results[0].score >= results[1].score

    @pytest.mark.asyncio()
    async def test_deduplicates_identical(
        self, optimizer: ContextOptimizer, ctx: RetrievalContext
    ) -> None:
        content = "This is a repeated sentence used for deduplication."
        r1 = _result(score=0.9, content=content)
        r2 = _result(score=0.8, content=content)
        results = await optimizer.optimize([r1, r2], ctx)
        assert len(results) == 1
        assert results[0].score == 0.9

    @pytest.mark.asyncio()
    async def test_keeps_distinct_content(
        self, optimizer: ContextOptimizer, ctx: RetrievalContext
    ) -> None:
        r1 = _result(score=0.9, content="apple orchard farming techniques")
        r2 = _result(score=0.8, content="deep sea marine biology research")
        results = await optimizer.optimize([r1, r2], ctx)
        assert len(results) == 2

    @pytest.mark.asyncio()
    async def test_trims_to_token_budget(self, optimizer: ContextOptimizer) -> None:
        ctx = _context(top_k=100, min_score=0.0, max_tokens=10)
        long_content = "word " * 100  # ~500 tokens
        r = _result(score=0.9, content=long_content)
        results = await optimizer.optimize([r], ctx)
        assert results == []

    @pytest.mark.asyncio()
    async def test_applies_min_score_filter(self, optimizer: ContextOptimizer) -> None:
        ctx = _context(top_k=10, min_score=0.8)
        r1 = _result(score=0.9, content="high score content one")
        r2 = _result(score=0.5, content="low score content two")
        results = await optimizer.optimize([r1, r2], ctx)
        assert all(r.score >= 0.8 for r in results)

    def test_estimate_tokens(self) -> None:
        assert _estimate_tokens("a" * 8) == 2
        assert _estimate_tokens("") == 1  # clamped to 1


# ---------------------------------------------------------------------------
# GraphRetriever
# ---------------------------------------------------------------------------


class TestGraphRetriever:
    @pytest.fixture()
    def repo(self) -> FakeNeo4jRepositoryBase:
        return FakeNeo4jRepositoryBase(
            records=[
                {
                    "node_id": "svc-1",
                    "node_labels": ["Service"],
                    "node_name": "auth-service",
                    "node_description": "Handles authentication",
                }
            ]
        )

    @pytest.fixture()
    def retriever(self, repo: FakeNeo4jRepositoryBase) -> GraphRetriever:
        return GraphRetriever(repository=repo)  # type: ignore[arg-type]

    @pytest.mark.asyncio()
    async def test_retrieve_basic(self, retriever: GraphRetriever) -> None:
        ctx = _context(
            strategy=RetrievalStrategy.GRAPH,
            filters={"anchor_id": "svc-root"},
        )
        results = await retriever.retrieve(ctx)
        assert len(results) == 1
        assert results[0].source == DocumentSource.GRAPH
        assert "auth-service" in results[0].chunk.content

    @pytest.mark.asyncio()
    async def test_retrieve_missing_anchor_raises(self, retriever: GraphRetriever) -> None:
        ctx = _context(strategy=RetrievalStrategy.GRAPH)
        with pytest.raises(GraphTraversalError, match="anchor_id"):
            await retriever.retrieve(ctx)

    @pytest.mark.asyncio()
    async def test_retrieve_invalid_relationship_type_raises(
        self, retriever: GraphRetriever
    ) -> None:
        ctx = _context(
            strategy=RetrievalStrategy.GRAPH,
            filters={"anchor_id": "svc-1"},
            graph_relationship_types=("INVALID_REL",),
        )
        with pytest.raises(GraphTraversalError, match="not permitted"):
            await retriever.retrieve(ctx)

    @pytest.mark.asyncio()
    async def test_retrieve_with_valid_relationship_types(
        self, retriever: GraphRetriever
    ) -> None:
        ctx = _context(
            strategy=RetrievalStrategy.GRAPH,
            filters={"anchor_id": "svc-1"},
            graph_relationship_types=("DEPENDS_ON", "AFFECTED"),
        )
        results = await retriever.retrieve(ctx)
        assert results

    @pytest.mark.asyncio()
    async def test_retrieve_uses_cache(self, repo: FakeNeo4jRepositoryBase) -> None:
        cache = FakeRedisCache()
        retriever = GraphRetriever(repository=repo, cache=cache)  # type: ignore[arg-type]
        ctx = _context(
            strategy=RetrievalStrategy.GRAPH,
            filters={"anchor_id": "svc-1"},
            use_cache=True,
        )
        await retriever.retrieve(ctx)
        assert len(cache._store) == 1
        # Second call should hit cache (repo failure won't matter)
        repo._fail = True
        results = await retriever.retrieve(ctx)
        assert results  # came from cache

    @pytest.mark.asyncio()
    async def test_neo4j_failure_raises(self, repo: FakeNeo4jRepositoryBase) -> None:
        repo._fail = True
        retriever = GraphRetriever(repository=repo)  # type: ignore[arg-type]
        ctx = _context(
            strategy=RetrievalStrategy.GRAPH,
            filters={"anchor_id": "svc-1"},
            use_cache=False,
        )
        with pytest.raises(GraphTraversalError, match="traversal failed"):
            await retriever.retrieve(ctx)


# ---------------------------------------------------------------------------
# HybridRetriever
# ---------------------------------------------------------------------------


class TestHybridRetriever:
    @pytest.fixture()
    def vector_provider(self) -> FakeRetrievalProvider:
        chunk_v = Chunk.make("doc-1", "vector content about auth", 0)
        chunk_shared = Chunk.make("doc-2", "shared chunk content", 0)
        return FakeRetrievalProvider(
            _strategy=RetrievalStrategy.VECTOR,
            results=[
                RetrievalResult.from_chunk(
                    chunk_v, 0.9, retrieval_id="ret-1", source=DocumentSource.INLINE
                ),
                RetrievalResult.from_chunk(
                    chunk_shared, 0.7, retrieval_id="ret-1", source=DocumentSource.INLINE
                ),
            ],
        )

    @pytest.fixture()
    def keyword_provider(self) -> FakeRetrievalProvider:
        chunk_k = Chunk.make("doc-3", "keyword content about auth", 0)
        chunk_shared = Chunk.make("doc-2", "shared chunk content", 0)
        return FakeRetrievalProvider(
            _strategy=RetrievalStrategy.KEYWORD,
            results=[
                RetrievalResult.from_chunk(
                    chunk_k, 0.8, retrieval_id="ret-1", source=DocumentSource.DATABASE
                ),
                RetrievalResult.from_chunk(
                    chunk_shared, 0.6, retrieval_id="ret-1", source=DocumentSource.DATABASE
                ),
            ],
        )

    @pytest.mark.asyncio()
    async def test_hybrid_merges_results(
        self,
        vector_provider: FakeRetrievalProvider,
        keyword_provider: FakeRetrievalProvider,
    ) -> None:
        retriever = HybridRetriever(
            vector_provider=vector_provider, keyword_provider=keyword_provider
        )
        ctx = _context(strategy=RetrievalStrategy.HYBRID, top_k=10)
        results = await retriever.retrieve(ctx)
        chunk_ids = {r.chunk.chunk_id for r in results}
        assert "doc-1:chunk:0" in chunk_ids  # vector only
        assert "doc-3:chunk:0" in chunk_ids  # keyword only
        assert "doc-2:chunk:0" in chunk_ids  # shared

    @pytest.mark.asyncio()
    async def test_shared_chunk_fused_score(
        self,
        vector_provider: FakeRetrievalProvider,
        keyword_provider: FakeRetrievalProvider,
    ) -> None:
        retriever = HybridRetriever(
            vector_provider=vector_provider,
            keyword_provider=keyword_provider,
            vector_weight=0.7,
            keyword_weight=0.3,
        )
        ctx = _context(strategy=RetrievalStrategy.HYBRID, top_k=10)
        results = await retriever.retrieve(ctx)
        shared = next(r for r in results if r.chunk.chunk_id == "doc-2:chunk:0")
        expected = 0.7 * 0.7 + 0.3 * 0.6
        assert abs(shared.score - expected) < 1e-9

    def test_weight_normalization(
        self,
        vector_provider: FakeRetrievalProvider,
        keyword_provider: FakeRetrievalProvider,
    ) -> None:
        retriever = HybridRetriever(
            vector_provider=vector_provider,
            keyword_provider=keyword_provider,
            vector_weight=7.0,
            keyword_weight=3.0,
        )
        assert abs(retriever.vector_weight - 0.7) < 1e-9
        assert abs(retriever.keyword_weight - 0.3) < 1e-9

    @pytest.mark.asyncio()
    async def test_tolerates_provider_failure(
        self, keyword_provider: FakeRetrievalProvider
    ) -> None:
        failing_vector = FakeRetrievalProvider(_fail=True)
        retriever = HybridRetriever(
            vector_provider=failing_vector, keyword_provider=keyword_provider
        )
        ctx = _context(strategy=RetrievalStrategy.HYBRID, top_k=10)
        results = await retriever.retrieve(ctx)
        # keyword results should still be returned
        assert results

    @pytest.mark.asyncio()
    async def test_top_k_applied(
        self,
        vector_provider: FakeRetrievalProvider,
        keyword_provider: FakeRetrievalProvider,
    ) -> None:
        retriever = HybridRetriever(
            vector_provider=vector_provider, keyword_provider=keyword_provider
        )
        ctx = _context(strategy=RetrievalStrategy.HYBRID, top_k=1)
        results = await retriever.retrieve(ctx)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# RetrievalPipeline
# ---------------------------------------------------------------------------


class TestRetrievalPipeline:
    @pytest.fixture()
    def provider(self) -> FakeRetrievalProvider:
        return FakeRetrievalProvider(results=[_result()])

    @pytest.mark.asyncio()
    async def test_run_basic(self, provider: FakeRetrievalProvider) -> None:
        pipeline = RetrievalPipeline(provider=provider)
        ctx = _context(use_cache=False)
        results, meta = await pipeline.run(ctx)
        assert len(results) == 1
        assert meta.cached is False
        assert meta.provider_name == "fake-provider"

    @pytest.mark.asyncio()
    async def test_run_with_embedding_provider(self) -> None:
        embedder = FakeEmbeddingProvider()
        provider = FakeRetrievalProvider(results=[_result()])
        pipeline = RetrievalPipeline(
            provider=provider, embedding_provider=embedder
        )
        ctx = _context(strategy=RetrievalStrategy.VECTOR, use_cache=False)
        _results, meta = await pipeline.run(ctx)
        assert meta.query_embedding_model == "fake-embedder"

    @pytest.mark.asyncio()
    async def test_run_caches_results(self, provider: FakeRetrievalProvider) -> None:
        cache = FakeRedisCache()
        pipeline = RetrievalPipeline(provider=provider, cache=cache)
        ctx = _context(use_cache=True)
        await pipeline.run(ctx)
        assert len(cache._store) == 1

    @pytest.mark.asyncio()
    async def test_cache_hit_returns_cached(self, provider: FakeRetrievalProvider) -> None:
        cache = FakeRedisCache()
        pipeline = RetrievalPipeline(provider=provider, cache=cache)
        ctx = _context(use_cache=True)
        await pipeline.run(ctx)
        # poison the provider so a live call would return different data
        provider.results = []
        results, meta = await pipeline.run(ctx)
        assert meta.cached is True
        assert len(results) == 1  # from cache

    @pytest.mark.asyncio()
    async def test_optimizer_applied(self, provider: FakeRetrievalProvider) -> None:
        optimizer = ContextOptimizer(max_tokens=100)
        pipeline = RetrievalPipeline(provider=provider, optimizer=optimizer)
        ctx = _context(use_cache=False)
        results, _ = await pipeline.run(ctx)
        assert results

    @pytest.mark.asyncio()
    async def test_provider_failure_raises(self) -> None:
        failing = FakeRetrievalProvider(_fail=True)
        pipeline = RetrievalPipeline(provider=failing)
        ctx = _context(use_cache=False)
        with pytest.raises(RetrievalProviderError, match="Retrieval failed"):
            await pipeline.run(ctx)

    @pytest.mark.asyncio()
    async def test_embedding_failure_raises(self) -> None:
        failing_embedder = FakeEmbeddingProvider(_fail=True)
        provider = FakeRetrievalProvider(results=[])
        pipeline = RetrievalPipeline(
            provider=provider, embedding_provider=failing_embedder
        )
        ctx = _context(strategy=RetrievalStrategy.VECTOR, use_cache=False)
        with pytest.raises(RetrievalProviderError, match="Embedding failed"):
            await pipeline.run(ctx)


# ---------------------------------------------------------------------------
# KnowledgeManager
# ---------------------------------------------------------------------------


class TestKnowledgeManager:
    @pytest.fixture()
    def factory(self) -> EmbeddingFactory:
        f = EmbeddingFactory()
        f.register(
            name="fake",
            version="1.0.0",
            provider=lambda _: FakeEmbeddingProvider(),
        )
        return f

    @pytest.fixture()
    def manager(self, factory: EmbeddingFactory) -> KnowledgeManager:
        return KnowledgeManager(
            processor=DocumentProcessor(),
            chunking_engine=ChunkingEngine(),
            embedding_factory=factory,
        )

    @pytest.mark.asyncio()
    async def test_ingest_returns_document_and_chunks(
        self, manager: KnowledgeManager
    ) -> None:
        meta = _metadata()
        doc, chunks = await manager.ingest("Paragraph one.\n\nParagraph two.", meta)
        assert doc.document_id == "doc-1"
        assert chunks

    @pytest.mark.asyncio()
    async def test_ingest_with_embeddings(self, manager: KnowledgeManager) -> None:
        manager.default_embedding_provider_name = "fake"
        meta = _metadata()
        _, chunks = await manager.ingest(
            "Some content for embedding.",
            meta,
            generate_embeddings=True,
        )
        assert all(c.embedding is not None for c in chunks)

    @pytest.mark.asyncio()
    async def test_ingest_embeddings_without_provider_raises(
        self, manager: KnowledgeManager
    ) -> None:
        meta = _metadata()
        with pytest.raises(KnowledgeManagerError, match="no embedding provider"):
            await manager.ingest("text", meta, generate_embeddings=True)

    @pytest.mark.asyncio()
    async def test_retrieve_without_pipeline_raises(
        self, manager: KnowledgeManager
    ) -> None:
        ctx = _context()
        with pytest.raises(KnowledgeManagerError, match="No RetrievalPipeline"):
            await manager.retrieve(ctx)

    @pytest.mark.asyncio()
    async def test_retrieve_with_default_pipeline(
        self, manager: KnowledgeManager
    ) -> None:
        provider = FakeRetrievalProvider(results=[_result()])
        pipeline = RetrievalPipeline(provider=provider)
        manager.default_pipeline = pipeline
        ctx = _context(use_cache=False)
        results, meta = await manager.retrieve(ctx)
        assert results
        assert isinstance(meta, RetrievalMetadata)

    @pytest.mark.asyncio()
    async def test_retrieve_with_explicit_pipeline_overrides_default(
        self, manager: KnowledgeManager
    ) -> None:
        default_provider = FakeRetrievalProvider(results=[])
        manager.default_pipeline = RetrievalPipeline(provider=default_provider)

        override_provider = FakeRetrievalProvider(results=[_result()])
        override_pipeline = RetrievalPipeline(provider=override_provider)

        ctx = _context(use_cache=False)
        results, _ = await manager.retrieve(ctx, pipeline=override_pipeline)
        assert results

    @pytest.mark.asyncio()
    async def test_ingest_invalid_content_raises(self, manager: KnowledgeManager) -> None:
        manager.processor.max_bytes = 1
        meta = _metadata()
        with pytest.raises(KnowledgeManagerError, match="Ingestion failed"):
            await manager.ingest("too long content", meta)
