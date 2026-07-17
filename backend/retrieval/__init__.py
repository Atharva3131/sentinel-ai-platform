"""Retrieval subsystem public API — Knowledge Engine."""

from __future__ import annotations

from backend.retrieval.benchmark_runner import BenchmarkRunner, RetrievalFn
from backend.retrieval.chunking import ChunkingEngine
from backend.retrieval.content_extractor import ContentExtractor
from backend.retrieval.context import RetrievalContext
from backend.retrieval.context_scorer import ContextQualityScorer
from backend.retrieval.embedding_indexer import EmbeddingIndexer
from backend.retrieval.exceptions import (
    ChunkingError,
    ContextOptimizationError,
    DocumentProcessingError,
    EmbeddingError,
    GraphTraversalError,
    KnowledgeManagerError,
    RetrievalError,
    RetrievalProviderError,
)
from backend.retrieval.extractor import MetadataExtractor
from backend.retrieval.factory import EmbeddingFactory, EmbeddingProviderFactory
from backend.retrieval.graph import GraphRetriever
from backend.retrieval.graph_builder import KnowledgeGraphBuilder
from backend.retrieval.groundedness_scorer import GroundednessScorer
from backend.retrieval.hybrid import HybridRetriever
from backend.retrieval.ingestion_exceptions import (
    ContentExtractionError,
    EmbeddingIndexError,
    GraphBuildError,
    IngestionError,
    IngestionPipelineError,
    MetadataExtractionError,
    RelationshipBuildError,
    VersioningError,
)
from backend.retrieval.ingestion_models import (
    DocumentFormat,
    DocumentVersion,
    ExtractedMetadata,
    GraphAnchor,
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
)
from backend.retrieval.ingestion_pipeline import DocumentIngestionPipeline
from backend.retrieval.ingestion_ports import (
    CacheInvalidator,
    ChunkRecord,
    KnowledgeEventPublisher,
    Neo4jRepositoryPort,
    VectorIndexPort,
)
from backend.retrieval.knowledge_indexer import KnowledgeIndexer
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
from backend.retrieval.optimizer import ContextOptimizer
from backend.retrieval.pipeline import RetrievalPipeline
from backend.retrieval.processor import DocumentProcessor
from backend.retrieval.providers import (
    DocumentMetadataStore,
    DocumentStore,
    EmbeddingProvider,
    RetrievalCache,
    RetrievalProvider,
)
from backend.retrieval.relationship_builder import RelationshipBuilder
from backend.retrieval.retrieval_eval_models import (
    BenchmarkCase,
    BenchmarkResult,
    JudgeProvider,
    RetrievalEvaluationReport,
    RetrievalMetrics,
)
from backend.retrieval.retrieval_evaluator import (
    RetrievalEvaluator,
    register_retrieval_strategies,
)
from backend.retrieval.versioning import DocumentVersionManager

__all__ = [
    "BenchmarkCase",
    "BenchmarkResult",
    "BenchmarkRunner",
    "CacheInvalidator",
    "Chunk",
    "ChunkRecord",
    "ChunkStrategy",
    "ChunkingEngine",
    "ChunkingError",
    "ContentExtractionError",
    "ContentExtractor",
    "ContextOptimizationError",
    "ContextOptimizer",
    "ContextQualityScorer",
    "Document",
    "DocumentFormat",
    "DocumentIngestionPipeline",
    "DocumentMetadata",
    "DocumentMetadataStore",
    "DocumentProcessingError",
    "DocumentProcessor",
    "DocumentSource",
    "DocumentStore",
    "DocumentVersion",
    "DocumentVersionManager",
    "EmbeddingError",
    "EmbeddingFactory",
    "EmbeddingIndexError",
    "EmbeddingIndexer",
    "EmbeddingProvider",
    "EmbeddingProviderFactory",
    "ExtractedMetadata",
    "GraphAnchor",
    "GraphBuildError",
    "GraphRetriever",
    "GraphTraversalError",
    "GroundednessScorer",
    "HybridRetriever",
    "IngestionError",
    "IngestionPipelineError",
    "IngestionRequest",
    "IngestionResult",
    "IngestionStatus",
    "JudgeProvider",
    "KnowledgeEventPublisher",
    "KnowledgeGraphBuilder",
    "KnowledgeIndexer",
    "KnowledgeManager",
    "KnowledgeManagerError",
    "MetadataExtractionError",
    "MetadataExtractor",
    "Neo4jRepositoryPort",
    "RelationshipBuildError",
    "RelationshipBuilder",
    "RetrievalCache",
    "RetrievalContext",
    "RetrievalError",
    "RetrievalEvaluationReport",
    "RetrievalEvaluator",
    "RetrievalFn",
    "RetrievalMetadata",
    "RetrievalMetrics",
    "RetrievalPipeline",
    "RetrievalProvider",
    "RetrievalProviderError",
    "RetrievalResult",
    "RetrievalStrategy",
    "VectorIndexPort",
    "VersioningError",
    "register_retrieval_strategies",
]
