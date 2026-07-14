"""Framework- and provider-neutral application interfaces."""

from backend.interfaces.common import ProviderContext, ProviderRegistry, ProviderUsage
from backend.interfaces.embeddings import (
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
)
from backend.interfaces.llm import LLMMessage, LLMProvider, LLMRequest, LLMResponse
from backend.interfaces.retrieval import (
    RetrievalHit,
    RetrievalRequest,
    RetrievalResponse,
    Retriever,
)
from backend.interfaces.tools import (
    ToolDefinition,
    ToolExecutionResult,
    ToolExecutor,
    ToolInvocation,
)

__all__ = [
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "ProviderContext",
    "ProviderRegistry",
    "ProviderUsage",
    "RetrievalHit",
    "RetrievalRequest",
    "RetrievalResponse",
    "Retriever",
    "ToolDefinition",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolInvocation",
]
