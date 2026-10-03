"""Provider interface tests."""

from __future__ import annotations

import pytest

from backend.interfaces import (
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderContext,
    ProviderRegistry,
    ProviderUsage,
    RetrievalHit,
    RetrievalRequest,
    RetrievalResponse,
    Retriever,
    ToolDefinition,
    ToolExecutionResult,
    ToolExecutor,
    ToolInvocation,
)


class FakeLLMProvider:
    name = "fake-llm-test"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            content=request.messages[-1].content.upper(),
            model=request.model,
            usage=ProviderUsage(input_tokens=4, output_tokens=2, total_tokens=6),
            context=request.context,
        )


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        return EmbeddingResponse(
            vectors=tuple(tuple(float(ord(char)) for char in text[:3]) for text in request.inputs),
            model=request.model,
            dimensions=3,
            context=request.context,
        )


class FakeRetriever:
    async def retrieve(self, request: RetrievalRequest) -> RetrievalResponse:
        return RetrievalResponse(
            hits=(
                RetrievalHit(
                    identifier="doc-1",
                    source="vector",
                    content=request.query,
                    score=0.98,
                ),
            ),
            context=request.context,
        )


class FakeToolExecutor:
    def list_tools(self) -> tuple[ToolDefinition, ...]:
        return (
            ToolDefinition(
                name="echo",
                description="Return the supplied payload.",
                input_schema={"type": "object"},
            ),
        )

    async def execute(self, invocation: ToolInvocation) -> ToolExecutionResult:
        return ToolExecutionResult(
            tool_name=invocation.tool_name,
            success=True,
            output=invocation.arguments,
            context=invocation.context,
        )


@pytest.mark.asyncio
async def test_named_provider_registry_resolves_configured_provider() -> None:
    registry: ProviderRegistry[LLMProvider] = ProviderRegistry()
    provider = FakeLLMProvider()
    registry.register("openai", provider, default=True)

    resolved = registry.resolve()
    response = await resolved.generate(
        LLMRequest(
            messages=(LLMMessage(role="user", content="hello"),),
            model="gpt-4.1-mini",
            context=ProviderContext(correlation_id="corr-1"),
        )
    )

    assert resolved is provider
    assert response.content == "HELLO"
    assert response.context.correlation_id == "corr-1"
    assert registry.names() == ("openai",)


def test_registry_rejects_duplicate_registration_without_override() -> None:
    registry: ProviderRegistry[ToolExecutor] = ProviderRegistry()
    executor = FakeToolExecutor()
    registry.register("default", executor)

    with pytest.raises(ValueError):
        registry.register("default", executor)


@pytest.mark.asyncio
async def test_embedding_retrieval_and_tool_protocols_are_structural() -> None:
    embedding_provider = FakeEmbeddingProvider()
    retriever = FakeRetriever()
    executor = FakeToolExecutor()

    assert isinstance(embedding_provider, EmbeddingProvider)
    assert isinstance(retriever, Retriever)
    assert isinstance(executor, ToolExecutor)

    embedding_response = await embedding_provider.embed(
        EmbeddingRequest(inputs=("alpha", "beta"), model="text-embedding-3-small")
    )
    retrieval_response = await retriever.retrieve(RetrievalRequest(query="incident timeline"))
    execution_response = await executor.execute(
        ToolInvocation(tool_name="echo", arguments={"message": "ok"})
    )

    assert embedding_response.dimensions == 3
    assert retrieval_response.hits[0].source == "vector"
    assert execution_response.output == {"message": "ok"}
