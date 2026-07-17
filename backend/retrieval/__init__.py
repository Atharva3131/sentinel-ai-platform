"""Retrieval subsystem public API — Knowledge Engine."""

from __future__ import annotations

from backend.retrieval.chunking import ChunkingEngine
from backend.retrieval.context import RetrievalContext
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
from backend.retrieval.factory import EmbeddingFactory, EmbeddingProviderFactory
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

__all__ = [
    "Chunk",
    "ChunkStrategy",
    "ChunkingEngine",
    "ChunkingError",
    "ContextOptimizationError",
    "ContextOptimizer",
    "Document",
    "DocumentMetadata",
    "DocumentMetadataStore",
    "DocumentProcessingError",
    "DocumentProcessor",
    "DocumentSource",
    "DocumentStore",
    "EmbeddingError",
    "EmbeddingFactory",
    "EmbeddingProvider",
    "EmbeddingProviderFactory",
    "GraphRetriever",
    "GraphTraversalError",
    "HybridRetriever",
    "KnowledgeManager",
    "KnowledgeManagerError",
    "RetrievalCache",
    "RetrievalContext",
    "RetrievalError",
    "RetrievalMetadata",
    "RetrievalPipeline",
    "RetrievalProvider",
    "RetrievalProviderError",
    "RetrievalResult",
    "RetrievalStrategy",
]
