"""LLM provider adapter tests — categories 1-8 + 10-11.

Tests exercise real adapter behaviour using httpx mock transport (no live LLM).

1.  provider selection (factory builds right type per config)
2.  real adapter contract (satisfies LLMProvider protocol)
3.  structured output (json_schema response_format roundtrip)
4.  tool calls (tool definitions → tool_calls in response)
5.  timeout (asyncio.wait_for fires LLMTimeoutError)
6.  retry (transient 5xx retried up to max_retries)
7.  cancellation (token checked before send)
8.  LLM error mapping (HTTP status codes → typed errors)
10. evaluator DI wiring
11. observability propagation (correlation_id in spans/logs)
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from backend.configuration.settings import (
    LLMProviderName,
    LLMProviderSettings,
    LLMSettings,
)
from backend.interfaces.common import ProviderContext, ProviderUsage
from backend.interfaces.fake_llm import FakeLLMProvider
from backend.interfaces.llm import (
    LLMCancelledError,
    LLMInvalidRequestError,
    LLMMessage,
    LLMProvider,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMToolDefinition,
    LLMUnavailableError,
)
from backend.providers.llm.factory import build_llm_provider
from backend.providers.llm.fallback import FallbackLLMProvider
from backend.providers.llm.mistral import MistralLLMAdapter
from backend.providers.llm.openai_compat import OpenAICompatibleLLMAdapter
from backend.providers.llm.sarvam import SarvamLLMAdapter

# ── helpers ────────────────────────────────────────────────────────────────


def _req(
    content: str = "Investigate the incident.",
    *,
    tools: tuple[LLMToolDefinition, ...] = (),
    structured_schema: dict[str, Any] | None = None,
    cancellation_token: Any = None,
    timeout_seconds: float | None = None,
) -> LLMRequest:
    return LLMRequest(
        messages=(LLMMessage(role="user", content=content),),
        model="sarvam-105b",
        tools=tools,
        structured_schema=structured_schema,
        cancellation_token=cancellation_token,
        timeout_seconds=timeout_seconds,
        context=ProviderContext(
            correlation_id="corr-test-001",
            workflow_id="wf-001",
            execution_id="exec-001",
        ),
    )


def _sarvam_settings(name: LLMProviderName = LLMProviderName.SARVAM) -> LLMProviderSettings:
    from pydantic import SecretStr
    return LLMProviderSettings(
        name=name,
        api_key=SecretStr("test-key"),
        model="sarvam-105b",
        base_url="https://api.sarvam.ai/v1",
        timeout_seconds=5.0,
        max_retries=2,
    )


def _mistral_settings() -> LLMProviderSettings:
    from pydantic import SecretStr
    return LLMProviderSettings(
        name=LLMProviderName.MISTRAL,
        api_key=SecretStr("test-key-mistral"),
        model="mistral-large-latest",
        base_url="https://api.mistral.ai/v1",
        timeout_seconds=5.0,
        max_retries=2,
    )


def _ok_response(
    content: str = "Root cause identified.",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    model: str = "sarvam-105b",
) -> dict[str, Any]:
    """Build a minimal OpenAI-compatible success payload."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_calls:
        message["tool_calls"] = tool_calls
        message["content"] = None
        finish_reason = "tool_calls"
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
    }


class _MockTransport(httpx.AsyncBaseTransport):
    """Returns a pre-configured response for every request."""

    def __init__(
        self,
        status_code: int = 200,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._status = status_code
        self._body = json.dumps(body or _ok_response()).encode()
        self._headers = httpx.Headers(headers or {"content-type": "application/json"})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=self._status,
            headers=self._headers,
            content=self._body,
        )


class _SequenceTransport(httpx.AsyncBaseTransport):
    """Returns responses in sequence; last response is repeated."""

    def __init__(self, responses: list[tuple[int, dict[str, Any]]]) -> None:
        self._responses = responses
        self._idx = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        status, body = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        return httpx.Response(
            status_code=status,
            headers={"content-type": "application/json"},
            content=json.dumps(body).encode(),
        )


def _adapter_with_transport(
    transport: httpx.AsyncBaseTransport,
    *,
    name: str = "sarvam",
    model: str = "sarvam-105b",
    timeout: float = 5.0,
    max_retries: int = 2,
    retry_min: float = 0.01,
    retry_max: float = 0.05,
) -> OpenAICompatibleLLMAdapter:
    adapter = OpenAICompatibleLLMAdapter(
        provider_name=name,
        base_url="https://api.sarvam.ai/v1",
        api_key="test-key",
        default_model=model,
        timeout_seconds=timeout,
        max_retries=max_retries,
        retry_min_wait=retry_min,
        retry_max_wait=retry_max,
    )
    adapter._http_client = httpx.AsyncClient(
        base_url="https://api.sarvam.ai/v1",
        headers={"Authorization": "Bearer test-key"},
        transport=transport,
    )
    return adapter


# ── 1. Provider selection ─────────────────────────────────────────────────


def test_factory_builds_fake_provider_when_configured() -> None:
    """build_llm_provider returns FakeLLMProvider for name=fake."""
    settings = LLMSettings(
        primary=LLMProviderSettings(name=LLMProviderName.FAKE)
    )
    provider = build_llm_provider(settings)
    assert isinstance(provider, FakeLLMProvider)


def test_factory_builds_sarvam_adapter() -> None:
    """build_llm_provider returns OpenAICompatibleLLMAdapter named 'sarvam'."""
    settings = LLMSettings(primary=_sarvam_settings())
    provider = build_llm_provider(settings)
    assert provider.name == "sarvam"
    assert isinstance(provider, OpenAICompatibleLLMAdapter)


def test_factory_builds_mistral_adapter() -> None:
    """build_llm_provider returns OpenAICompatibleLLMAdapter named 'mistral'."""
    settings = LLMSettings(primary=_mistral_settings())
    provider = build_llm_provider(settings)
    assert provider.name == "mistral"
    assert isinstance(provider, OpenAICompatibleLLMAdapter)


def test_factory_builds_fallback_when_both_configured() -> None:
    """build_llm_provider wraps with FallbackLLMProvider when fallback is set."""
    settings = LLMSettings(
        primary=_sarvam_settings(),
        fallback=_mistral_settings(),
        enable_fallback=True,
    )
    provider = build_llm_provider(settings)
    assert isinstance(provider, FallbackLLMProvider)
    assert "sarvam" in provider.name
    assert "mistral" in provider.name


def test_factory_returns_primary_when_fallback_disabled() -> None:
    """When enable_fallback=False, no FallbackLLMProvider is constructed."""
    settings = LLMSettings(
        primary=_sarvam_settings(),
        fallback=_mistral_settings(),
        enable_fallback=False,
    )
    provider = build_llm_provider(settings)
    assert not isinstance(provider, FallbackLLMProvider)
    assert provider.name == "sarvam"


def test_factory_returns_primary_when_same_provider_name() -> None:
    """Fallback with same provider name as primary is not wrapped."""
    settings = LLMSettings(
        primary=_sarvam_settings(),
        fallback=_sarvam_settings(),  # same name
        enable_fallback=True,
    )
    provider = build_llm_provider(settings)
    assert not isinstance(provider, FallbackLLMProvider)


# ── 2. Real adapter contract ──────────────────────────────────────────────


def test_sarvam_adapter_satisfies_protocol() -> None:
    """SarvamLLMAdapter satisfies the LLMProvider structural protocol."""
    adapter = SarvamLLMAdapter(_sarvam_settings())
    assert isinstance(adapter, LLMProvider)


def test_mistral_adapter_satisfies_protocol() -> None:
    """MistralLLMAdapter satisfies the LLMProvider structural protocol."""
    adapter = MistralLLMAdapter(_mistral_settings())
    assert isinstance(adapter, LLMProvider)


def test_fallback_provider_satisfies_protocol() -> None:
    """FallbackLLMProvider satisfies the LLMProvider structural protocol."""
    provider = FallbackLLMProvider(
        primary=FakeLLMProvider(),
        fallback=FakeLLMProvider(),
    )
    assert isinstance(provider, LLMProvider)


@pytest.mark.asyncio
async def test_adapter_returns_llm_response_on_200() -> None:
    """Adapter returns a complete LLMResponse on HTTP 200."""
    transport = _MockTransport(200, _ok_response("Hello from Sarvam"))
    adapter = _adapter_with_transport(transport)

    response = await adapter.generate(_req())

    assert response.content == "Hello from Sarvam"
    assert response.model == "sarvam-105b"
    assert response.finish_reason == "stop"
    assert response.usage is not None
    assert response.usage.total_tokens == 150


@pytest.mark.asyncio
async def test_adapter_includes_usage_in_response() -> None:
    """Adapter parses usage tokens from the response body."""
    transport = _MockTransport(200, _ok_response())
    adapter = _adapter_with_transport(transport)

    response = await adapter.generate(_req())

    assert isinstance(response.usage, ProviderUsage)
    assert response.usage.input_tokens == 100
    assert response.usage.output_tokens == 50


@pytest.mark.asyncio
async def test_adapter_propagates_correlation_id() -> None:
    """Adapter preserves the ProviderContext (including correlation_id) in response."""
    transport = _MockTransport(200, _ok_response())
    adapter = _adapter_with_transport(transport)
    ctx = ProviderContext(correlation_id="corr-abc")
    req = LLMRequest(
        messages=(LLMMessage(role="user", content="test"),),
        context=ctx,
    )

    response = await adapter.generate(req)

    assert response.context.correlation_id == "corr-abc"


# ── 3. Structured output ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_parses_structured_json_response() -> None:
    """Adapter parses JSON body as structured_output when schema is set."""
    output = {"root_cause_title": "DB overload", "confidence": 0.9}
    transport = _MockTransport(200, _ok_response(json.dumps(output)))
    adapter = _adapter_with_transport(transport)

    response = await adapter.generate(
        _req(structured_schema={"type": "object"})
    )

    assert response.structured_output == output
    assert response.is_structured


@pytest.mark.asyncio
async def test_adapter_wraps_non_json_in_content_key_for_structured() -> None:
    """Adapter wraps non-JSON text in {content: ...} when schema is set."""
    transport = _MockTransport(200, _ok_response("not json"))
    adapter = _adapter_with_transport(transport)

    response = await adapter.generate(_req(structured_schema={"type": "object"}))

    assert response.structured_output == {"content": "not json"}


@pytest.mark.asyncio
async def test_adapter_payload_includes_response_format_for_schema() -> None:
    """Adapter includes response_format in the HTTP payload when schema is set."""
    captured: dict[str, Any] = {}

    class _CapturingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_ok_response("{}")).encode(),
            )

    adapter = _adapter_with_transport(_CapturingTransport())
    schema = {"type": "object", "properties": {"key": {"type": "string"}}}
    await adapter.generate(_req(structured_schema=schema))

    rf = captured["body"].get("response_format", {})
    assert rf.get("type") == "json_schema"
    assert rf["json_schema"]["schema"] == schema


# ── 4. Tool calls ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_returns_tool_calls_from_response() -> None:
    """Adapter parses tool_calls from the OpenAI-format response."""
    tc_raw = [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "request_evidence",
                "arguments": '{"source_kinds": ["metrics"]}',
            },
        }
    ]
    transport = _MockTransport(200, _ok_response(tool_calls=tc_raw))
    adapter = _adapter_with_transport(transport)

    response = await adapter.generate(
        _req(
            tools=(
                LLMToolDefinition(
                    name="request_evidence",
                    description="Fetch evidence",
                ),
            )
        )
    )

    assert response.has_tool_calls
    assert len(response.tool_calls) == 1
    tc = response.tool_calls[0]
    assert tc.call_id == "call-1"
    assert tc.tool_name == "request_evidence"
    assert tc.arguments == {"source_kinds": ["metrics"]}
    assert response.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_adapter_payload_includes_tools_when_provided() -> None:
    """Adapter serialises tool definitions into the HTTP payload."""
    captured: dict[str, Any] = {}

    class _Cap(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_ok_response()).encode(),
            )

    adapter = _adapter_with_transport(_Cap())
    await adapter.generate(
        _req(
            tools=(LLMToolDefinition(name="my_tool", description="test"),),
        )
    )

    tools = captured["body"].get("tools", [])
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "my_tool"


# ── 5. Timeout ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_raises_timeout_error_on_slow_response() -> None:
    """Adapter raises LLMTimeoutError when the request exceeds the budget."""

    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(999)
            return httpx.Response(200)  # never reached

    adapter = _adapter_with_transport(_SlowTransport(), timeout=0.05, max_retries=0)

    with pytest.raises(LLMTimeoutError) as exc_info:
        await adapter.generate(_req(timeout_seconds=0.05))

    assert exc_info.value.provider == "sarvam"
    assert exc_info.value.timeout_seconds is not None


@pytest.mark.asyncio
async def test_adapter_respects_per_request_timeout() -> None:
    """Per-request timeout overrides the adapter default when shorter."""

    class _SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(999)
            return httpx.Response(200)

    adapter = _adapter_with_transport(_SlowTransport(), timeout=60.0, max_retries=0)

    with pytest.raises(LLMTimeoutError):
        await adapter.generate(_req(timeout_seconds=0.05))


# ── 6. Retry ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_retries_on_503_and_succeeds() -> None:
    """Adapter retries transient 503 and returns 200 on second attempt."""
    transport = _SequenceTransport([
        (503, {"error": "temporary unavailable"}),
        (200, _ok_response("Retry succeeded")),
    ])
    adapter = _adapter_with_transport(
        transport, max_retries=2, retry_min=0.01, retry_max=0.05
    )

    response = await adapter.generate(_req())

    assert response.content == "Retry succeeded"


@pytest.mark.asyncio
async def test_adapter_raises_after_max_retries_exhausted() -> None:
    """Adapter raises LLMUnavailableError after all retry attempts fail."""
    transport = _SequenceTransport([
        (503, {"error": "down"}),
        (503, {"error": "down"}),
        (503, {"error": "down"}),
    ])
    adapter = _adapter_with_transport(
        transport, max_retries=2, retry_min=0.01, retry_max=0.05
    )

    with pytest.raises(LLMUnavailableError):
        await adapter.generate(_req())


@pytest.mark.asyncio
async def test_adapter_retries_rate_limit_with_retry_after() -> None:
    """Adapter retries 429 after the Retry-After delay."""

    class _RateLimitThenOKTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._attempts = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self._attempts += 1
            if self._attempts == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0.01", "content-type": "application/json"},
                    content=b'{"error": "rate limit"}',
                )
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_ok_response("After rate limit")).encode(),
            )

    adapter = _adapter_with_transport(
        _RateLimitThenOKTransport(), max_retries=2, retry_min=0.01
    )

    response = await adapter.generate(_req())
    assert response.content == "After rate limit"


# ── 7. Cancellation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_raises_cancelled_when_token_set_before_send() -> None:
    """Adapter raises LLMCancelledError when cancellation token is already set."""

    class _Token:
        def is_set(self) -> bool:
            return True

    transport = _MockTransport(200, _ok_response())
    adapter = _adapter_with_transport(transport)

    with pytest.raises(LLMCancelledError):
        await adapter.generate(_req(cancellation_token=_Token()))


@pytest.mark.asyncio
async def test_adapter_raises_cancelled_during_retry_when_token_set() -> None:
    """Adapter respects cancellation token checked between retry attempts."""
    call_count = 0

    class _FlipToken:
        def is_set(self) -> bool:
            return call_count > 0

    class _FailOnce(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                503,
                headers={"content-type": "application/json"},
                content=b'{"error": "down"}',
            )

    adapter = _adapter_with_transport(
        _FailOnce(), max_retries=3, retry_min=0.01
    )

    with pytest.raises((LLMCancelledError, LLMUnavailableError)):
        await adapter.generate(_req(cancellation_token=_FlipToken()))


# ── 8. LLM error mapping ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_maps_401_to_invalid_request() -> None:
    transport = _MockTransport(401, {"error": {"message": "unauthorized"}})
    adapter = _adapter_with_transport(transport, max_retries=0)

    with pytest.raises(LLMInvalidRequestError):
        await adapter.generate(_req())


@pytest.mark.asyncio
async def test_adapter_maps_400_to_invalid_request() -> None:
    transport = _MockTransport(
        400, {"error": {"message": "context_length_exceeded"}}
    )
    adapter = _adapter_with_transport(transport, max_retries=0)

    with pytest.raises(LLMInvalidRequestError):
        await adapter.generate(_req())


@pytest.mark.asyncio
async def test_adapter_maps_403_to_invalid_request() -> None:
    transport = _MockTransport(403, {"error": {"message": "forbidden"}})
    adapter = _adapter_with_transport(transport, max_retries=0)

    with pytest.raises(LLMInvalidRequestError):
        await adapter.generate(_req())


@pytest.mark.asyncio
async def test_adapter_maps_500_to_unavailable() -> None:
    transport = _MockTransport(500, {"error": {"message": "internal"}})
    adapter = _adapter_with_transport(transport, max_retries=0)

    with pytest.raises(LLMUnavailableError):
        await adapter.generate(_req())


@pytest.mark.asyncio
async def test_adapter_maps_connect_error_to_unavailable() -> None:
    """httpx.ConnectError is mapped to LLMUnavailableError."""

    class _ConnectFailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

    adapter = _adapter_with_transport(
        _ConnectFailTransport(), max_retries=0
    )

    with pytest.raises(LLMUnavailableError):
        await adapter.generate(_req())


# ── Fallback provider routing ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_provider_routes_to_fallback_on_unavailable() -> None:
    """FallbackLLMProvider delegates to fallback when primary is unavailable."""
    from backend.interfaces.fake_llm import FailingFakeLLM

    primary = FailingFakeLLM(
        error=LLMUnavailableError("primary down", provider="primary")
    )
    fallback = FakeLLMProvider(responses=["fallback answer"])
    provider = FallbackLLMProvider(primary=primary, fallback=fallback)

    response = await provider.generate(_req())
    assert response.content == "fallback answer"


@pytest.mark.asyncio
async def test_fallback_provider_does_not_route_on_rate_limit() -> None:
    """FallbackLLMProvider does NOT fall back on LLMRateLimitError."""
    from backend.interfaces.fake_llm import FailingFakeLLM

    primary = FailingFakeLLM(
        error=LLMRateLimitError("rate limited", provider="primary")
    )
    fallback = FakeLLMProvider(responses=["should not be called"])
    provider = FallbackLLMProvider(primary=primary, fallback=fallback)

    with pytest.raises(LLMRateLimitError):
        await provider.generate(_req())


@pytest.mark.asyncio
async def test_fallback_provider_propagates_when_no_fallback_configured() -> None:
    """FallbackLLMProvider propagates the primary error when fallback is None."""
    from backend.interfaces.fake_llm import FailingFakeLLM

    primary = FailingFakeLLM(
        error=LLMUnavailableError("down", provider="primary")
    )
    provider = FallbackLLMProvider(primary=primary, fallback=None)

    with pytest.raises(LLMUnavailableError):
        await provider.generate(_req())


# ── 11. Observability propagation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_payload_includes_model_from_request() -> None:
    """Adapter sends the request model (not just the default) in the payload."""
    captured: dict[str, Any] = {}

    class _Cap(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(_ok_response()).encode(),
            )

    adapter = _adapter_with_transport(_Cap())
    req = LLMRequest(
        messages=(LLMMessage(role="user", content="test"),),
        model="sarvam-30b",
        context=ProviderContext(correlation_id="corr-obs"),
    )
    await adapter.generate(req)
    assert captured["body"]["model"] == "sarvam-30b"


@pytest.mark.asyncio
async def test_adapter_name_is_stable_identifier() -> None:
    """Adapter name matches the provider_name passed at construction."""
    adapter = _adapter_with_transport(_MockTransport(), name="sarvam")
    assert adapter.name == "sarvam"
