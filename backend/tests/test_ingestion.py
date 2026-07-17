"""Tests for backend/retrieval/ knowledge ingestion subsystem."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from backend.retrieval.chunking import ChunkingEngine
from backend.retrieval.content_extractor import ContentExtractor
from backend.retrieval.embedding_indexer import EmbeddingIndexer
from backend.retrieval.extractor import MetadataExtractor
from backend.retrieval.factory import EmbeddingFactory
from backend.retrieval.graph_builder import KnowledgeGraphBuilder
from backend.retrieval.ingestion_exceptions import (
    EmbeddingIndexError,
    GraphBuildError,
    IngestionPipelineError,
    RelationshipBuildError,
    VersioningError,
)
from backend.retrieval.ingestion_models import (
    DocumentFormat,
    DocumentVersion,
    GraphAnchor,
    IngestionRequest,
    IngestionStatus,
)
from backend.retrieval.ingestion_pipeline import DocumentIngestionPipeline
from backend.retrieval.ingestion_ports import ChunkRecord
from backend.retrieval.knowledge_indexer import KnowledgeIndexer
from backend.retrieval.models import Chunk, DocumentSource
from backend.retrieval.processor import DocumentProcessor
from backend.retrieval.relationship_builder import RelationshipBuilder
from backend.retrieval.versioning import DocumentVersionManager

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FakeMetadataStore:
    _store: dict[str, dict[str, Any]] = field(default_factory=dict)
    _fail: bool = False

    async def upsert(self, document_id: str, metadata: dict[str, Any]) -> None:
        if self._fail:
            raise RuntimeError("store failure")
        self._store[document_id] = metadata

    async def get(self, document_id: str) -> dict[str, Any] | None:
        if self._fail:
            raise RuntimeError("store failure")
        return self._store.get(document_id)

    async def delete(self, document_id: str) -> bool:
        existed = document_id in self._store
        self._store.pop(document_id, None)
        return existed

    async def query(
        self,
        *,
        tenant_id: str | None = None,
        tags: tuple[str, ...] = (),
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return list(self._store.values())[:limit]


@dataclass(slots=True)
class FakeVectorIndex:
    stored: dict[str, list[ChunkRecord]] = field(default_factory=dict)
    _fail: bool = False

    async def upsert_chunks(
        self, document_id: str, chunks: list[ChunkRecord]
    ) -> int:
        if self._fail:
            raise RuntimeError("index failure")
        self.stored[document_id] = chunks
        return len(chunks)

    async def delete_by_document(self, document_id: str) -> int:
        count = len(self.stored.pop(document_id, []))
        return count


@dataclass(slots=True)
class FakeNeo4jRepository:
    writes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    reads: list[dict[str, Any]] = field(default_factory=list)
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
        return self.reads

    async def write(
        self,
        query: str,
        parameters: Any = None,
        *,
        database: Any = None,
    ) -> list[dict[str, Any]]:
        if self._fail:
            raise RuntimeError("neo4j failure")
        self.writes.append((query, dict(parameters or {})))
        return []

    async def single(
        self,
        query: str,
        parameters: Any = None,
        *,
        database: Any = None,
    ) -> dict[str, Any] | None:
        if self._fail:
            raise RuntimeError("neo4j failure")
        return self.reads[0] if self.reads else None


@dataclass(slots=True)
class FakeDocumentStore:
    blobs: dict[str, bytes] = field(default_factory=dict)
    _fail: bool = False

    async def upload(
        self,
        document_id: str,
        content: bytes,
        *,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if self._fail:
            raise RuntimeError("blob failure")
        self.blobs[document_id] = content
        return f"blob://fake/{document_id}"

    async def download(self, document_id: str) -> bytes:
        return self.blobs.get(document_id, b"")

    async def delete(self, document_id: str) -> bool:
        existed = document_id in self.blobs
        self.blobs.pop(document_id, None)
        return existed

    async def exists(self, document_id: str) -> bool:
        return document_id in self.blobs


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
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    async def embed_single(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3, 0.4]


@dataclass(slots=True)
class FakeEventPublisher:
    events: list[dict[str, Any]] = field(default_factory=list)

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        tenant_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        self.events.append(
            {"event_type": event_type, "payload": payload, "tenant_id": tenant_id}
        )


@dataclass(slots=True)
class FakeCacheInvalidator:
    invalidated: list[str] = field(default_factory=list)

    async def invalidate(self, pattern: str) -> int:
        self.invalidated.append(pattern)
        return 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _embedding_factory(fail: bool = False) -> EmbeddingFactory:
    factory = EmbeddingFactory()
    factory.register(
        name="fake",
        version="1.0.0",
        provider=lambda _: FakeEmbeddingProvider(_fail=fail),
    )
    return factory


def _make_pipeline(
    *,
    metadata_store: FakeMetadataStore | None = None,
    vector_index: FakeVectorIndex | None = None,
    neo4j_repo: FakeNeo4jRepository | None = None,
    doc_store: FakeDocumentStore | None = None,
    event_publisher: FakeEventPublisher | None = None,
    cache_invalidator: FakeCacheInvalidator | None = None,
    embedding_factory: EmbeddingFactory | None = None,
    default_embedding_provider: str | None = None,
) -> DocumentIngestionPipeline:
    meta_store = metadata_store or FakeMetadataStore()
    return DocumentIngestionPipeline(
        processor=DocumentProcessor(),
        content_extractor=ContentExtractor(),
        metadata_extractor=MetadataExtractor(),
        chunking_engine=ChunkingEngine(),
        version_manager=DocumentVersionManager(metadata_store=meta_store),
        embedding_indexer=(
            EmbeddingIndexer(vector_index=vector_index)
            if vector_index is not None
            else None
        ),
        graph_builder=(
            KnowledgeGraphBuilder(
                repository=neo4j_repo,
                event_publisher=event_publisher,
            )
            if neo4j_repo is not None
            else None
        ),
        relationship_builder=(
            RelationshipBuilder(repository=neo4j_repo)
            if neo4j_repo is not None
            else None
        ),
        document_store=doc_store,
        embedding_factory=embedding_factory,
        cache_invalidator=cache_invalidator,
        default_embedding_provider_name=default_embedding_provider,
    )


def _request(
    document_id: str = "doc-1",
    content: bytes | str = "Hello world document.",
    content_type: str = "text/plain",
    source: DocumentSource = DocumentSource.INLINE,
    **kwargs: Any,
) -> IngestionRequest:
    return IngestionRequest(
        document_id=document_id,
        content=content,
        content_type=content_type,
        source=source,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# ContentExtractor
# ---------------------------------------------------------------------------


class TestContentExtractor:
    @pytest.mark.asyncio()
    async def test_plain_text(self) -> None:
        extractor = ContentExtractor()
        text, fmt = await extractor.extract(b"hello world", "text/plain")
        assert text == "hello world"
        assert fmt == DocumentFormat.TEXT

    @pytest.mark.asyncio()
    async def test_markdown(self) -> None:
        extractor = ContentExtractor()
        text, fmt = await extractor.extract(b"# Hello\nWorld", "text/markdown")
        assert "Hello" in text
        assert fmt == DocumentFormat.MARKDOWN

    @pytest.mark.asyncio()
    async def test_json(self) -> None:
        extractor = ContentExtractor()
        payload = json.dumps({"title": "Test"}).encode()
        text, fmt = await extractor.extract(payload, "application/json")
        assert "Test" in text
        assert fmt == DocumentFormat.JSON

    @pytest.mark.asyncio()
    async def test_html_strips_tags(self) -> None:
        extractor = ContentExtractor()
        text, fmt = await extractor.extract(b"<h1>Title</h1><p>Body</p>", "text/html")
        assert "<h1>" not in text
        assert "Title" in text
        assert fmt == DocumentFormat.HTML

    @pytest.mark.asyncio()
    async def test_pdf_returns_text(self) -> None:
        extractor = ContentExtractor()
        # Minimal synthetic PDF snippet
        pdf_bytes = b"%PDF-1.4\nBT\n(Hello PDF)\nTj\nET\n"
        text, fmt = await extractor.extract(pdf_bytes, "application/pdf")
        assert fmt == DocumentFormat.PDF
        assert isinstance(text, str)

    @pytest.mark.asyncio()
    async def test_unknown_content_type_best_effort(self) -> None:
        extractor = ContentExtractor()
        text, fmt = await extractor.extract(b"raw content", "application/octet-stream")
        assert fmt == DocumentFormat.UNKNOWN
        assert "raw content" in text

    @pytest.mark.asyncio()
    async def test_content_type_with_charset(self) -> None:
        extractor = ContentExtractor()
        text, fmt = await extractor.extract(
            b"hello", "text/plain; charset=utf-8"
        )
        assert text == "hello"
        assert fmt == DocumentFormat.TEXT


# ---------------------------------------------------------------------------
# MetadataExtractor
# ---------------------------------------------------------------------------


class TestMetadataExtractor:
    @pytest.fixture()
    def extractor(self) -> MetadataExtractor:
        return MetadataExtractor()

    @pytest.mark.asyncio()
    async def test_extract_text_title_from_first_line(
        self, extractor: MetadataExtractor
    ) -> None:
        meta = await extractor.extract("My Document Title\n\nBody text.", content_type="text/plain")
        assert meta.title == "My Document Title"

    @pytest.mark.asyncio()
    async def test_extract_text_word_count(self, extractor: MetadataExtractor) -> None:
        meta = await extractor.extract("one two three", content_type="text/plain")
        assert meta.word_count == 3

    @pytest.mark.asyncio()
    async def test_extract_markdown_frontmatter(self, extractor: MetadataExtractor) -> None:
        content = "---\ntitle: My Guide\nauthor: Alice\ntags: ops, sre\n---\n\n# Body"
        meta = await extractor.extract(content, content_type="text/markdown")
        assert meta.title == "My Guide"
        assert meta.author == "Alice"
        assert "ops" in meta.tags

    @pytest.mark.asyncio()
    async def test_extract_markdown_h1_fallback(self, extractor: MetadataExtractor) -> None:
        meta = await extractor.extract(
            "# Hello World\n\nsome content", content_type="text/markdown"
        )
        assert meta.title == "Hello World"

    @pytest.mark.asyncio()
    async def test_extract_markdown_summary(self, extractor: MetadataExtractor) -> None:
        meta = await extractor.extract(
            "# Title\n\nFirst paragraph here.\n\nSecond paragraph.",
            content_type="text/markdown",
        )
        assert meta.summary is not None
        assert "First paragraph" in meta.summary

    @pytest.mark.asyncio()
    async def test_extract_json_title(self, extractor: MetadataExtractor) -> None:
        content = json.dumps({"title": "JSON Doc", "author": "Bob", "tags": "a,b"})
        meta = await extractor.extract(content, content_type="application/json")
        assert meta.title == "JSON Doc"
        assert meta.author == "Bob"
        assert "a" in meta.tags

    @pytest.mark.asyncio()
    async def test_extract_json_invalid_json_falls_back_to_text(
        self, extractor: MetadataExtractor
    ) -> None:
        meta = await extractor.extract("not json at all", content_type="application/json")
        assert meta.title is not None

    @pytest.mark.asyncio()
    async def test_hint_tags_merged(self, extractor: MetadataExtractor) -> None:
        meta = await extractor.extract(
            "simple text", content_type="text/plain", hint_tags=("sre", "ops")
        )
        assert "sre" in meta.tags
        assert "ops" in meta.tags

    @pytest.mark.asyncio()
    async def test_no_duplicate_tags(self, extractor: MetadataExtractor) -> None:
        content = "---\ntitle: X\ntags: ops\n---\nbody"
        meta = await extractor.extract(
            content, content_type="text/markdown", hint_tags=("ops", "sre")
        )
        assert meta.tags.count("ops") == 1


# ---------------------------------------------------------------------------
# DocumentVersionManager
# ---------------------------------------------------------------------------


class TestDocumentVersionManager:
    @pytest.fixture()
    def store(self) -> FakeMetadataStore:
        return FakeMetadataStore()

    @pytest.fixture()
    def manager(self, store: FakeMetadataStore) -> DocumentVersionManager:
        return DocumentVersionManager(metadata_store=store)

    @pytest.mark.asyncio()
    async def test_new_document_version_1(self, manager: DocumentVersionManager) -> None:
        v = await manager.check("doc-1", "abc123")
        assert v.version == 1
        assert v.is_new is True
        assert v.has_changed is True

    @pytest.mark.asyncio()
    async def test_unchanged_document_skipped(
        self, manager: DocumentVersionManager, store: FakeMetadataStore
    ) -> None:
        store._store["doc-1"] = {"version": 2, "checksum": "same"}
        v = await manager.check("doc-1", "same")
        assert v.version == 2
        assert v.has_changed is False

    @pytest.mark.asyncio()
    async def test_changed_document_increments_version(
        self, manager: DocumentVersionManager, store: FakeMetadataStore
    ) -> None:
        store._store["doc-1"] = {"version": 2, "checksum": "old"}
        v = await manager.check("doc-1", "new")
        assert v.version == 3
        assert v.previous_version == 2
        assert v.has_changed is True

    @pytest.mark.asyncio()
    async def test_record_persists_version(
        self, manager: DocumentVersionManager, store: FakeMetadataStore
    ) -> None:
        v = DocumentVersion(document_id="doc-1", version=1, checksum="abc", is_new=True)
        await manager.record(v, additional={"title": "Test Doc"})
        saved = store._store["doc-1"]
        assert saved["version"] == 1
        assert saved["title"] == "Test Doc"

    @pytest.mark.asyncio()
    async def test_store_failure_raises_versioning_error(
        self, store: FakeMetadataStore
    ) -> None:
        store._fail = True
        manager = DocumentVersionManager(metadata_store=store)
        with pytest.raises(VersioningError, match="Failed to read"):
            await manager.check("doc-1", "abc")


# ---------------------------------------------------------------------------
# EmbeddingIndexer
# ---------------------------------------------------------------------------


class TestEmbeddingIndexer:
    @pytest.fixture()
    def vector_index(self) -> FakeVectorIndex:
        return FakeVectorIndex()

    @pytest.fixture()
    def indexer(self, vector_index: FakeVectorIndex) -> EmbeddingIndexer:
        return EmbeddingIndexer(vector_index=vector_index)

    def _chunks(self, n: int = 3, doc_id: str = "doc-1") -> list[Chunk]:
        return [Chunk.make(doc_id, f"content {i}", i) for i in range(n)]

    @pytest.mark.asyncio()
    async def test_index_without_embeddings(
        self, indexer: EmbeddingIndexer, vector_index: FakeVectorIndex
    ) -> None:
        chunks = self._chunks(2)
        count = await indexer.index("doc-1", chunks, None)
        assert count == 2
        assert all(r.embedding is None for r in vector_index.stored["doc-1"])

    @pytest.mark.asyncio()
    async def test_index_with_embeddings(
        self, indexer: EmbeddingIndexer, vector_index: FakeVectorIndex
    ) -> None:
        chunks = self._chunks(2)
        embeddings = [[0.1, 0.2], [0.3, 0.4]]
        await indexer.index("doc-1", chunks, embeddings)
        records = vector_index.stored["doc-1"]
        assert records[0].embedding == (0.1, 0.2)
        assert records[1].embedding == (0.3, 0.4)

    @pytest.mark.asyncio()
    async def test_incremental_deletes_old_chunks(
        self, indexer: EmbeddingIndexer, vector_index: FakeVectorIndex
    ) -> None:
        vector_index.stored["doc-1"] = [
            ChunkRecord(
                chunk_id="old",
                document_id="doc-1",
                content="stale",
                index=0,
                version=1,
            )
        ]
        await indexer.index("doc-1", self._chunks(2), None)
        assert len(vector_index.stored["doc-1"]) == 2

    @pytest.mark.asyncio()
    async def test_embedding_length_mismatch_raises(
        self, indexer: EmbeddingIndexer
    ) -> None:
        chunks = self._chunks(3)
        with pytest.raises(EmbeddingIndexError, match="Embedding count"):
            await indexer.index("doc-1", chunks, [[0.1, 0.2]])

    @pytest.mark.asyncio()
    async def test_index_failure_raises(self, vector_index: FakeVectorIndex) -> None:
        vector_index._fail = True
        indexer = EmbeddingIndexer(vector_index=vector_index)
        with pytest.raises(EmbeddingIndexError, match="Failed to upsert"):
            await indexer.index("doc-1", self._chunks(1), None)

    @pytest.mark.asyncio()
    async def test_version_stored_on_records(
        self, indexer: EmbeddingIndexer, vector_index: FakeVectorIndex
    ) -> None:
        await indexer.index("doc-1", self._chunks(1), None, version=5)
        assert vector_index.stored["doc-1"][0].version == 5


# ---------------------------------------------------------------------------
# KnowledgeGraphBuilder
# ---------------------------------------------------------------------------


class TestKnowledgeGraphBuilder:
    @pytest.fixture()
    def repo(self) -> FakeNeo4jRepository:
        return FakeNeo4jRepository()

    @pytest.fixture()
    def publisher(self) -> FakeEventPublisher:
        return FakeEventPublisher()

    @pytest.fixture()
    def builder(
        self, repo: FakeNeo4jRepository, publisher: FakeEventPublisher
    ) -> KnowledgeGraphBuilder:
        return KnowledgeGraphBuilder(
            repository=repo,
            event_publisher=publisher,
        )

    @pytest.mark.asyncio()
    async def test_upsert_document_writes_to_neo4j(
        self, builder: KnowledgeGraphBuilder, repo: FakeNeo4jRepository
    ) -> None:
        v = DocumentVersion(document_id="doc-1", version=1, checksum="abc", is_new=True)
        await builder.upsert_document(v, source="blob", blob_uri="blob://x", title="T")
        assert len(repo.writes) == 1
        params = repo.writes[0][1]
        assert params["document_id"] == "doc-1"
        assert params["checksum"] == "abc"

    @pytest.mark.asyncio()
    async def test_upsert_emits_event(
        self, builder: KnowledgeGraphBuilder, publisher: FakeEventPublisher
    ) -> None:
        v = DocumentVersion(document_id="doc-1", version=1, checksum="abc", is_new=True)
        await builder.upsert_document(v, source="blob")
        assert len(publisher.events) == 1
        assert "updated" in publisher.events[0]["event_type"]

    @pytest.mark.asyncio()
    async def test_delete_document_writes_to_neo4j(
        self, builder: KnowledgeGraphBuilder, repo: FakeNeo4jRepository
    ) -> None:
        await builder.delete_document("doc-1")
        assert any("doc-1" == w[1].get("document_id") for w in repo.writes)

    @pytest.mark.asyncio()
    async def test_delete_emits_event(
        self, builder: KnowledgeGraphBuilder, publisher: FakeEventPublisher
    ) -> None:
        await builder.delete_document("doc-1")
        assert any("deleted" in e["event_type"] for e in publisher.events)

    @pytest.mark.asyncio()
    async def test_get_document_returns_none_when_not_found(
        self, repo: FakeNeo4jRepository
    ) -> None:
        builder = KnowledgeGraphBuilder(repository=repo)
        result = await builder.get_document("missing")
        assert result is None

    @pytest.mark.asyncio()
    async def test_get_document_returns_record(
        self, repo: FakeNeo4jRepository
    ) -> None:
        repo.reads = [{"document_id": "doc-1", "version": 2}]
        builder = KnowledgeGraphBuilder(repository=repo)
        result = await builder.get_document("doc-1")
        assert result is not None
        assert result["version"] == 2

    @pytest.mark.asyncio()
    async def test_neo4j_failure_raises_graph_build_error(
        self, repo: FakeNeo4jRepository
    ) -> None:
        repo._fail = True
        builder = KnowledgeGraphBuilder(repository=repo)
        v = DocumentVersion(document_id="doc-1", version=1, checksum="x", is_new=True)
        with pytest.raises(GraphBuildError, match="Failed to upsert"):
            await builder.upsert_document(v, source="blob")


# ---------------------------------------------------------------------------
# RelationshipBuilder
# ---------------------------------------------------------------------------


class TestRelationshipBuilder:
    @pytest.fixture()
    def repo(self) -> FakeNeo4jRepository:
        return FakeNeo4jRepository()

    @pytest.fixture()
    def builder(self, repo: FakeNeo4jRepository) -> RelationshipBuilder:
        return RelationshipBuilder(repository=repo)

    @pytest.mark.asyncio()
    async def test_build_documents_relationship(
        self, builder: RelationshipBuilder, repo: FakeNeo4jRepository
    ) -> None:
        anchors = (GraphAnchor(target_node_id="svc-1", relationship_type="DOCUMENTS"),)
        count = await builder.build("doc-1", anchors)
        assert count == 1
        params = repo.writes[0][1]
        assert params["target_id"] == "svc-1"

    @pytest.mark.asyncio()
    async def test_build_has_runbook_relationship(
        self, builder: RelationshipBuilder, repo: FakeNeo4jRepository
    ) -> None:
        anchors = (GraphAnchor(target_node_id="svc-1", relationship_type="HAS_RUNBOOK"),)
        count = await builder.build("doc-1", anchors)
        assert count == 1

    @pytest.mark.asyncio()
    async def test_build_owned_by_relationship(
        self, builder: RelationshipBuilder, repo: FakeNeo4jRepository
    ) -> None:
        anchors = (GraphAnchor(target_node_id="team-1", relationship_type="OWNED_BY"),)
        count = await builder.build("doc-1", anchors)
        assert count == 1

    @pytest.mark.asyncio()
    async def test_invalid_relationship_type_raises(
        self, builder: RelationshipBuilder
    ) -> None:
        anchors = (GraphAnchor(target_node_id="svc-1", relationship_type="DEPENDS_ON"),)
        with pytest.raises(RelationshipBuildError, match="not permitted"):
            await builder.build("doc-1", anchors)

    @pytest.mark.asyncio()
    async def test_empty_anchors_returns_zero(
        self, builder: RelationshipBuilder
    ) -> None:
        count = await builder.build("doc-1", ())
        assert count == 0

    @pytest.mark.asyncio()
    async def test_multiple_anchors(
        self, builder: RelationshipBuilder, repo: FakeNeo4jRepository
    ) -> None:
        anchors = (
            GraphAnchor(target_node_id="svc-1", relationship_type="DOCUMENTS"),
            GraphAnchor(target_node_id="team-1", relationship_type="OWNED_BY"),
        )
        count = await builder.build("doc-1", anchors)
        assert count == 2
        assert len(repo.writes) == 2

    @pytest.mark.asyncio()
    async def test_all_failures_raises(
        self, repo: FakeNeo4jRepository
    ) -> None:
        repo._fail = True
        builder = RelationshipBuilder(repository=repo)
        anchors = (GraphAnchor(target_node_id="svc-1", relationship_type="DOCUMENTS"),)
        with pytest.raises(RelationshipBuildError, match="failed"):
            await builder.build("doc-1", anchors)


# ---------------------------------------------------------------------------
# DocumentIngestionPipeline
# ---------------------------------------------------------------------------


class TestDocumentIngestionPipeline:
    @pytest.mark.asyncio()
    async def test_new_document_created(self) -> None:
        pipeline = _make_pipeline()
        result = await pipeline.run(_request())
        assert result.status == IngestionStatus.CREATED
        assert result.version is not None
        assert result.version.version == 1

    @pytest.mark.asyncio()
    async def test_unchanged_document_skipped(self) -> None:
        store = FakeMetadataStore()
        pipeline = _make_pipeline(metadata_store=store)
        req = _request(content="Hello world document.")
        # First ingestion
        await pipeline.run(req)
        # Second ingestion with same content
        result = await pipeline.run(req)
        assert result.status == IngestionStatus.SKIPPED

    @pytest.mark.asyncio()
    async def test_changed_document_updated(self) -> None:
        store = FakeMetadataStore()
        pipeline = _make_pipeline(metadata_store=store)
        await pipeline.run(_request(content="v1 content"))
        result = await pipeline.run(_request(content="v2 content different"))
        assert result.status == IngestionStatus.UPDATED
        assert result.version is not None
        assert result.version.version == 2

    @pytest.mark.asyncio()
    async def test_chunk_count_in_result(self) -> None:
        pipeline = _make_pipeline()
        result = await pipeline.run(_request(content="Para one.\n\nPara two."))
        assert result.chunk_count > 0

    @pytest.mark.asyncio()
    async def test_blob_upload_performed(self) -> None:
        doc_store = FakeDocumentStore()
        pipeline = _make_pipeline(doc_store=doc_store)
        result = await pipeline.run(_request(blob_upload=True))
        assert result.blob_uri is not None
        assert "blob://fake/doc-1" == result.blob_uri

    @pytest.mark.asyncio()
    async def test_blob_upload_skipped_when_false(self) -> None:
        doc_store = FakeDocumentStore()
        pipeline = _make_pipeline(doc_store=doc_store)
        result = await pipeline.run(_request(blob_upload=False))
        assert result.blob_uri is None

    @pytest.mark.asyncio()
    async def test_graph_node_upserted(self) -> None:
        repo = FakeNeo4jRepository()
        pipeline = _make_pipeline(neo4j_repo=repo)
        await pipeline.run(_request())
        assert any("Document" in q for q, _ in repo.writes)

    @pytest.mark.asyncio()
    async def test_graph_relationships_created(self) -> None:
        repo = FakeNeo4jRepository()
        pipeline = _make_pipeline(neo4j_repo=repo)
        anchors = (GraphAnchor(target_node_id="svc-1", relationship_type="DOCUMENTS"),)
        await pipeline.run(_request(graph_anchors=anchors))
        assert len(repo.writes) >= 2  # upsert + relationship

    @pytest.mark.asyncio()
    async def test_cache_invalidated_after_ingestion(self) -> None:
        invalidator = FakeCacheInvalidator()
        pipeline = _make_pipeline(cache_invalidator=invalidator)
        await pipeline.run(_request())
        assert len(invalidator.invalidated) == 1

    @pytest.mark.asyncio()
    async def test_embeddings_generated_and_indexed(self) -> None:
        vector_index = FakeVectorIndex()
        factory = _embedding_factory()
        pipeline = _make_pipeline(
            vector_index=vector_index,
            embedding_factory=factory,
            default_embedding_provider="fake",
        )
        await pipeline.run(_request(generate_embeddings=True))
        assert "doc-1" in vector_index.stored
        records = vector_index.stored["doc-1"]
        assert all(r.embedding is not None for r in records)

    @pytest.mark.asyncio()
    async def test_markdown_ingestion(self) -> None:
        pipeline = _make_pipeline()
        content = "---\ntitle: Runbook\nauthor: SRE\n---\n\n# Steps\n\nDo this first."
        result = await pipeline.run(
            _request(content=content, content_type="text/markdown")
        )
        assert result.status == IngestionStatus.CREATED

    @pytest.mark.asyncio()
    async def test_json_ingestion(self) -> None:
        pipeline = _make_pipeline()
        content = json.dumps({"title": "Config Doc", "description": "Settings guide"})
        result = await pipeline.run(
            _request(content=content, content_type="application/json")
        )
        assert result.status == IngestionStatus.CREATED

    @pytest.mark.asyncio()
    async def test_pdf_ingestion(self) -> None:
        pipeline = _make_pipeline()
        pdf_bytes = b"%PDF-1.4\nBT\n(Section One)\nTj\nET\n"
        result = await pipeline.run(
            _request(content=pdf_bytes, content_type="application/pdf")
        )
        assert result.status == IngestionStatus.CREATED

    @pytest.mark.asyncio()
    async def test_metadata_stored_after_ingestion(self) -> None:
        store = FakeMetadataStore()
        pipeline = _make_pipeline(metadata_store=store)
        await pipeline.run(_request())
        assert "doc-1" in store._store

    @pytest.mark.asyncio()
    async def test_duration_ms_populated(self) -> None:
        pipeline = _make_pipeline()
        result = await pipeline.run(_request())
        assert result.duration_ms is not None
        assert result.duration_ms >= 0.0

    @pytest.mark.asyncio()
    async def test_blob_failure_raises_pipeline_error(self) -> None:
        doc_store = FakeDocumentStore()
        doc_store._fail = True
        pipeline = _make_pipeline(doc_store=doc_store)
        with pytest.raises(IngestionPipelineError, match="blob_upload"):
            await pipeline.run(_request(blob_upload=True))

    @pytest.mark.asyncio()
    async def test_embedding_failure_raises_pipeline_error(self) -> None:
        vector_index = FakeVectorIndex()
        factory = _embedding_factory(fail=True)
        pipeline = _make_pipeline(
            vector_index=vector_index,
            embedding_factory=factory,
            default_embedding_provider="fake",
        )
        with pytest.raises(IngestionPipelineError, match="embedding"):
            await pipeline.run(_request(generate_embeddings=True))


# ---------------------------------------------------------------------------
# KnowledgeIndexer
# ---------------------------------------------------------------------------


class TestKnowledgeIndexer:
    @pytest.fixture()
    def indexer(self) -> KnowledgeIndexer:
        return KnowledgeIndexer(pipeline=_make_pipeline())

    @pytest.mark.asyncio()
    async def test_ingest_single_document(self, indexer: KnowledgeIndexer) -> None:
        result = await indexer.ingest(
            "Hello world",
            document_id="doc-1",
            content_type="text/plain",
            source=DocumentSource.INLINE,
        )
        assert result.status == IngestionStatus.CREATED

    @pytest.mark.asyncio()
    async def test_ingest_batch_all_succeed(self) -> None:
        indexer = KnowledgeIndexer(pipeline=_make_pipeline())
        requests = [
            _request(document_id=f"doc-{i}", content=f"content {i}")
            for i in range(5)
        ]
        results = await indexer.ingest_batch(requests)
        assert len(results) == 5
        assert all(r.status == IngestionStatus.CREATED for r in results)

    @pytest.mark.asyncio()
    async def test_ingest_batch_isolates_failures(self) -> None:
        store = FakeMetadataStore()
        store._fail = True  # will cause versioning failures
        pipeline = _make_pipeline(metadata_store=store)
        indexer = KnowledgeIndexer(pipeline=pipeline)
        requests = [_request(document_id=f"doc-{i}") for i in range(3)]
        results = await indexer.ingest_batch(requests)
        assert all(r.status == IngestionStatus.FAILED for r in results)
        assert len(results) == 3

    @pytest.mark.asyncio()
    async def test_ingest_batch_preserves_order(self) -> None:
        indexer = KnowledgeIndexer(pipeline=_make_pipeline())
        requests = [
            _request(document_id=f"doc-{i}", content=f"doc {i} content here")
            for i in range(4)
        ]
        results = await indexer.ingest_batch(requests)
        for i, result in enumerate(results):
            assert result.document_id == f"doc-{i}"

    @pytest.mark.asyncio()
    async def test_ingest_batch_bounded_concurrency(self) -> None:
        indexer = KnowledgeIndexer(pipeline=_make_pipeline(), default_concurrency=2)
        requests = [_request(document_id=f"doc-{i}", content=f"content {i}") for i in range(6)]
        results = await indexer.ingest_batch(requests, concurrency=2)
        assert len(results) == 6

    @pytest.mark.asyncio()
    async def test_ingest_with_tags(self, indexer: KnowledgeIndexer) -> None:
        result = await indexer.ingest(
            "doc content",
            document_id="doc-1",
            content_type="text/plain",
            tags=("runbook", "auth"),
        )
        assert result.succeeded

    @pytest.mark.asyncio()
    async def test_ingest_skip_unchanged(self) -> None:
        store = FakeMetadataStore()
        pipeline = _make_pipeline(metadata_store=store)
        indexer = KnowledgeIndexer(pipeline=pipeline)
        await indexer.ingest(
            "same content",
            document_id="doc-1",
            content_type="text/plain",
        )
        result = await indexer.ingest(
            "same content",
            document_id="doc-1",
            content_type="text/plain",
            skip_if_unchanged=True,
        )
        assert result.status == IngestionStatus.SKIPPED
