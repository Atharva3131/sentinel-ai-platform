"""Framework- and provider-neutral application interfaces."""

from backend.interfaces.common import ProviderContext, ProviderRegistry, ProviderUsage
from backend.interfaces.embeddings import (
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
)
from backend.interfaces.evidence import (
    ConfigurationEvidenceProvider,
    DeploymentEvidenceProvider,
    EvidenceProvider,
    HistoricalIncidentsProvider,
    InfrastructureEvidenceProvider,
    KnowledgeBaseProvider,
    LogsEvidenceProvider,
    MetricsEvidenceProvider,
    TracesEvidenceProvider,
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
    "ConfigurationEvidenceProvider",
    "DeploymentEvidenceProvider",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "EvidenceProvider",
    "HistoricalIncidentsProvider",
    "InfrastructureEvidenceProvider",
    "KnowledgeBaseProvider",
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LogsEvidenceProvider",
    "MetricsEvidenceProvider",
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
    "TracesEvidenceProvider",
]
