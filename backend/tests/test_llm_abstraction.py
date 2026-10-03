"""LLM abstraction tests — categories 7-12.

Tests 7: LLM provider abstraction (protocol compliance, fake providers)
Tests 8: Structured LLM output
Tests 9: Tool-call handling
Tests 10: Timeout
Tests 11: Cancellation
Tests 12: Retry (RateLimitError with retry_after_seconds)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from backend.interfaces.common import ProviderContext, ProviderUsage
from backend.interfaces.fake_llm import (
    FailingFakeLLM,
    FakeLLMProvider,
    StructuredFakeLLM,
    ToolCallFakeLLM,
)
from backend.interfaces.llm import (
    LLMCancelledError,
    LLMError,
    LLMMessage,
    LLMProvider,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMToolCall,
    LLMToolDefinition,
    LLMToolResult,
    LLMUnavailableError,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _request(
    content: str = "Investigate the incident.",
    *,
    structured_schema: dict[str, Any] | None = None,
    tools: tuple[LLMToolDefinition, ...] = (),
    cancellation_token: Any = None,
    timeout_seconds: float | None = None,
) -> LLMRequest:
    return LLMRequest(
        messages=(LLMMessage(role="user", content=content),),
        model="test-model",
        structured_schema=structured_schema,
        tools=tools,
        cancellation_token=cancellation_token,
        timeout_seconds=timeout_seconds,
        context=ProviderContext(correlation_id="corr-test"),
    )


class _CancelToken:
    def __init__(self, *, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    def is_set(self) -> bool:
        return self._cancelled


# ── 7. LLM provider abstraction ────────────────────────────────────────────


def test_fake_llm_satisfies_protocol() -> None:
    """FakeLLMProvider satisfies the LLMProvider structural protocol."""
    fake = FakeLLMProvider()
    assert isinstance(fake, LLMProvider)


def test_failing_fake_llm_satisfies_protocol() -> None:
    """FailingFakeLLM satisfies the LLMProvider structural protocol."""
    fake = FailingFakeLLM()
    assert isinstance(fake, LLMProvider)


def test_structured_fake_llm_satisfies_protocol() -> None:
    """StructuredFakeLLM satisfies the LLMProvider structural protocol."""
    fake = StructuredFakeLLM({"root_cause": "deployment"})
    assert isinstance(fake, LLMProvider)


@pytest.mark.asyncio
async def test_fake_llm_returns_configured_response() -> None:
    """FakeLLMProvider returns configured responses in order."""
    fake = FakeLLMProvider(responses=["first", "second", "third"])

    r1 = await fake.generate(_request())
    r2 = await fake.generate(_request())
    r3 = await fake.generate(_request())
    r4 = await fake.generate(_request())  # repeats last

    assert r1.content == "first"
    assert r2.content == "second"
    assert r3.content == "third"
    assert r4.content == "third"
    assert fake.call_count == 4


@pytest.mark.asyncio
async def test_fake_llm_records_requests() -> None:
    """FakeLLMProvider stores all requests for assertion."""
    fake = FakeLLMProvider()
    req = _request("test")
    await fake.generate(req)

    assert len(fake.requests) == 1
    assert fake.requests[0].messages[0].content == "test"


@pytest.mark.asyncio
async def test_fake_llm_includes_usage() -> None:
    """FakeLLMProvider includes ProviderUsage in the response."""
    usage = ProviderUsage(input_tokens=100, output_tokens=50, total_tokens=150)
    fake = FakeLLMProvider(usage=usage)
    response = await fake.generate(_request())

    assert response.usage is not None
    assert response.usage.total_tokens == 150


def test_llm_response_has_tool_calls_property() -> None:
    """LLMResponse.has_tool_calls reflects the presence of tool calls."""
    no_tools = LLMResponse(content="hello")
    with_tools = LLMResponse(
        content="",
        tool_calls=(LLMToolCall(call_id="c1", tool_name="t", arguments={}),),
    )
    assert not no_tools.has_tool_calls
    assert with_tools.has_tool_calls


def test_llm_response_is_structured_property() -> None:
    """LLMResponse.is_structured reflects the presence of structured_output."""
    plain = LLMResponse(content="plain text")
    structured = LLMResponse(content="{}", structured_output={"key": "val"})
    assert not plain.is_structured
    assert structured.is_structured


# ── 8. Structured LLM output ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_structured_fake_llm_returns_dict() -> None:
    """StructuredFakeLLM always returns the configured dict as structured_output."""
    output = {"root_cause_title": "Database overload", "confidence": 0.85}
    fake = StructuredFakeLLM(output)

    response = await fake.generate(_request(structured_schema={"type": "object"}))

    assert response.structured_output == output
    assert response.is_structured


@pytest.mark.asyncio
async def test_fake_llm_parses_json_content_as_structured() -> None:
    """FakeLLMProvider parses JSON content as structured_output when schema provided."""
    output = {"key": "value", "score": 0.9}
    fake = FakeLLMProvider(responses=[json.dumps(output)])

    response = await fake.generate(
        _request(structured_schema={"type": "object"})
    )

    assert response.structured_output == output


@pytest.mark.asyncio
async def test_fake_llm_wraps_non_json_as_structured() -> None:
    """FakeLLMProvider wraps non-JSON in content key when schema is set."""
    fake = FakeLLMProvider(responses=["not json at all"])

    response = await fake.generate(
        _request(structured_schema={"type": "object"})
    )

    assert response.structured_output == {"content": "not json at all"}


# ── 9. Tool-call handling ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_fake_llm_emits_tool_calls_first() -> None:
    """ToolCallFakeLLM returns tool calls on the first turn."""
    calls = [
        LLMToolCall(
            call_id="c1",
            tool_name="request_evidence",
            arguments={"source_kinds": ["metrics"]},
        )
    ]
    fake = ToolCallFakeLLM(tool_calls=[calls])

    response = await fake.generate(_request())

    assert response.has_tool_calls
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls[0].tool_name == "request_evidence"
    assert response.tool_calls[0].arguments == {"source_kinds": ["metrics"]}


@pytest.mark.asyncio
async def test_tool_call_fake_llm_returns_final_answer_after_calls() -> None:
    """ToolCallFakeLLM returns the final text response after tool rounds."""
    calls = [LLMToolCall(call_id="c1", tool_name="t", arguments={})]
    fake = ToolCallFakeLLM(
        tool_calls=[calls],
        final_response="Root cause identified.",
    )

    r1 = await fake.generate(_request())
    r2 = await fake.generate(_request())

    assert r1.has_tool_calls
    assert not r2.has_tool_calls
    assert r2.content == "Root cause identified."
    assert r2.finish_reason == "stop"


@pytest.mark.asyncio
async def test_tool_result_is_error_flag() -> None:
    """LLMToolResult.is_error correctly flags failed tool calls."""
    success = LLMToolResult(call_id="c1", tool_name="t", content="ok")
    failure = LLMToolResult(call_id="c2", tool_name="t", content="err", is_error=True)

    assert not success.is_error
    assert failure.is_error


# ── 10. Timeout ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slow_provider_is_cancelled_by_timeout() -> None:
    """A provider that hangs should be interrupted by asyncio.wait_for."""

    class _SlowProvider:
        name = "slow"

        async def generate(self, request: LLMRequest) -> LLMResponse:
            await asyncio.sleep(999)
            return LLMResponse(content="never")

    provider = _SlowProvider()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            provider.generate(_request(timeout_seconds=0.05)),
            timeout=0.05,
        )


@pytest.mark.asyncio
async def test_llm_timeout_error_carries_timeout_seconds() -> None:
    """LLMTimeoutError should carry timeout_seconds for observability."""
    error = LLMTimeoutError("timed out", provider="test", timeout_seconds=5.0)
    assert error.timeout_seconds == 5.0
    assert error.provider == "test"


# ── 11. Cancellation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fake_llm_respects_cancellation_token() -> None:
    """FakeLLMProvider raises LLMCancelledError when cancellation is set."""
    token = _CancelToken(cancelled=True)
    fake = FakeLLMProvider()

    with pytest.raises(LLMCancelledError):
        await fake.generate(_request(cancellation_token=token))


@pytest.mark.asyncio
async def test_fake_llm_proceeds_when_cancellation_not_set() -> None:
    """FakeLLMProvider proceeds normally when cancellation token is not set."""
    token = _CancelToken(cancelled=False)
    fake = FakeLLMProvider(responses=["ok"])

    response = await fake.generate(_request(cancellation_token=token))
    assert response.content == "ok"


# ── 12. Retry (rate-limit error with retry_after) ──────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_error_carries_retry_after() -> None:
    """LLMRateLimitError carries retry_after_seconds for the caller."""
    error = LLMRateLimitError("rate limited", provider="test", retry_after_seconds=30.0)
    assert error.retry_after_seconds == 30.0


@pytest.mark.asyncio
async def test_failing_fake_raises_configured_error() -> None:
    """FailingFakeLLM raises whatever error it was configured with."""
    error = LLMRateLimitError("rate limited", provider="test", retry_after_seconds=1.0)
    fake = FailingFakeLLM(error=error)

    with pytest.raises(LLMRateLimitError) as exc_info:
        await fake.generate(_request())

    assert exc_info.value.retry_after_seconds == 1.0


@pytest.mark.asyncio
async def test_failing_fake_raises_unavailable_by_default() -> None:
    """FailingFakeLLM raises LLMUnavailableError when no error is configured."""
    fake = FailingFakeLLM()

    with pytest.raises(LLMUnavailableError):
        await fake.generate(_request())


@pytest.mark.asyncio
async def test_llm_error_hierarchy_is_correct() -> None:
    """All specific LLM errors are subclasses of LLMError."""
    assert issubclass(LLMTimeoutError, LLMError)
    assert issubclass(LLMRateLimitError, LLMError)
    assert issubclass(LLMUnavailableError, LLMError)
    assert issubclass(LLMCancelledError, LLMError)
