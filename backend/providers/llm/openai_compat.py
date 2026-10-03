"""OpenAI-compatible LLM adapter.

Both Sarvam and Mistral expose an OpenAI-compatible
``POST /v1/chat/completions`` endpoint.  This single adapter handles both by
accepting a configurable ``base_url``.  No vendor SDK is imported; all I/O
goes through ``httpx.AsyncClient``.

Wire contract with the LLMProvider protocol:
  * name           — configurable stable identifier ("sarvam" / "mistral")
  * generate()     — full request/response cycle with tool calls, structured
                     output, timeout, cancellation, retry, and OTel spans.

Error mapping:
  HTTP 429               → LLMRateLimitError (Retry-After header respected)
  HTTP 401 / 403         → LLMInvalidRequestError
  HTTP 400               → LLMInvalidRequestError
  HTTP 5xx               → LLMUnavailableError
  asyncio.TimeoutError   → LLMTimeoutError
  httpx.ConnectError     → LLMUnavailableError
  cancellation_token set → LLMCancelledError

Security:
  * API key is passed as Bearer token; never logged.
  * Prompt/response content is never logged at INFO level.
  * Sensitive headers are not included in OTel attributes.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from backend.interfaces.common import ProviderUsage
from backend.interfaces.llm import (
    LLMCancelledError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMToolCall,
    LLMUnavailableError,
)

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer("sentinel.llm.openai_compat")


@dataclass
class OpenAICompatibleLLMAdapter:
    """HTTP adapter for any OpenAI-compatible chat completions endpoint.

    Inject:
      ``provider_name``   — stable name used in logs and OTel attributes
      ``base_url``        — e.g. "https://api.sarvam.ai/v1" or "https://api.mistral.ai/v1"
      ``api_key``         — bearer token (never logged)
      ``default_model``   — model sent when LLMRequest.model is None
      ``timeout_seconds`` — per-request HTTP timeout
      ``max_retries``     — max retry attempts for transient errors
      ``retry_min_wait``  — initial retry back-off seconds
      ``retry_max_wait``  — maximum retry back-off seconds
      ``temperature``     — default temperature (overridden by request)
      ``max_output_tokens``— default max tokens (overridden by request)
    """

    provider_name: str
    base_url: str
    api_key: str
    default_model: str
    timeout_seconds: float = 60.0
    max_retries: int = 3
    retry_min_wait: float = 1.0
    retry_max_wait: float = 10.0
    temperature: float | None = None
    max_output_tokens: int | None = None
    _http_client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    @property
    def name(self) -> str:
        return self.provider_name

    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        return self._http_client

    async def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    # ── Public protocol method ────────────────────────────────────────────

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Execute a chat completion request against the provider endpoint."""
        # Cooperative cancellation before any I/O
        if request.cancellation_token is not None and request.cancellation_token.is_set():
            raise LLMCancelledError("Request cancelled before send", provider=self.name)

        payload = self._build_payload(request)
        correlation_id = request.context.correlation_id or str(uuid.uuid4())

        with _tracer.start_as_current_span(
            f"llm.generate.{self.provider_name}",
            kind=trace.SpanKind.CLIENT,
        ) as span:
            _set_span_attrs(span, request, self.provider_name)
            t0 = time.monotonic()
            try:
                response = await self._execute_with_retry(
                    payload, correlation_id, request
                )
            except LLMError:
                span.set_status(StatusCode.ERROR)
                raise
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise LLMUnavailableError(str(exc), provider=self.name) from exc

            latency_ms = (time.monotonic() - t0) * 1000
            span.set_attribute("llm.latency_ms", round(latency_ms, 2))
            if response.usage:
                span.set_attribute("llm.tokens.input", response.usage.input_tokens)
                span.set_attribute("llm.tokens.output", response.usage.output_tokens)
                span.set_attribute("llm.tokens.total", response.usage.total_tokens)
            if response.tool_calls:
                span.set_attribute("llm.tool_calls", len(response.tool_calls))
            span.set_status(StatusCode.OK)

        log.info(
            "llm_generate_complete",
            provider=self.provider_name,
            model=response.model,
            latency_ms=round(latency_ms, 2),
            tokens_total=response.usage.total_tokens if response.usage else None,
            tool_calls=len(response.tool_calls),
            correlation_id=correlation_id,
        )
        return response

    # ── Retry loop ────────────────────────────────────────────────────────

    async def _execute_with_retry(
        self,
        payload: dict[str, Any],
        correlation_id: str,
        request: LLMRequest,
    ) -> LLMResponse:
        attempt = 0
        wait = self.retry_min_wait

        while True:
            # Check cancellation before each attempt
            if request.cancellation_token is not None and request.cancellation_token.is_set():
                raise LLMCancelledError(
                    "Request cancelled during retry", provider=self.name
                )

            attempt += 1
            try:
                return await self._send_once(payload, request)
            except LLMRateLimitError as exc:
                if attempt > self.max_retries:
                    raise
                retry_after = exc.retry_after_seconds or wait
                log.warning(
                    "llm_rate_limited_retrying",
                    provider=self.provider_name,
                    attempt=attempt,
                    retry_after=retry_after,
                    correlation_id=correlation_id,
                )
                await asyncio.sleep(retry_after)
                wait = min(wait * 2.0, self.retry_max_wait)
            except LLMUnavailableError:
                if attempt > self.max_retries:
                    raise
                log.warning(
                    "llm_unavailable_retrying",
                    provider=self.provider_name,
                    attempt=attempt,
                    wait=wait,
                    correlation_id=correlation_id,
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2.0, self.retry_max_wait)
            except (LLMTimeoutError, LLMCancelledError, LLMInvalidRequestError):
                # Not retryable
                raise

    async def _send_once(
        self, payload: dict[str, Any], request: LLMRequest
    ) -> LLMResponse:
        effective_timeout = (
            min(request.timeout_seconds, self.timeout_seconds)
            if request.timeout_seconds is not None
            else self.timeout_seconds
        )
        try:
            http_response = await asyncio.wait_for(
                self._client().post("/chat/completions", json=payload),
                timeout=effective_timeout,
            )
        except TimeoutError:
            raise LLMTimeoutError(
                f"Request to {self.provider_name} timed out after {effective_timeout}s",
                provider=self.name,
                timeout_seconds=effective_timeout,
            ) from None
        except httpx.ConnectError as exc:
            raise LLMUnavailableError(
                f"{self.provider_name} unreachable: {exc}", provider=self.name
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(
                f"{self.provider_name} HTTP error: {exc}", provider=self.name
            ) from exc

        return self._parse_response(http_response, request)

    # ── Request / response translation ───────────────────────────────────

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        """Translate a neutral LLMRequest to OpenAI-compatible JSON."""
        messages: list[dict[str, Any]] = []
        for msg in request.messages:
            m: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.name:
                m["name"] = msg.name
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            messages.append(m)

        payload: dict[str, Any] = {
            "model": request.model or self.default_model,
            "messages": messages,
        }

        # Temperature — request overrides adapter default
        temp = request.temperature if request.temperature is not None else self.temperature
        if temp is not None:
            payload["temperature"] = temp

        # Max tokens
        max_tok = (
            request.max_output_tokens
            if request.max_output_tokens is not None
            else self.max_output_tokens
        )
        if max_tok is not None:
            payload["max_tokens"] = max_tok

        # Tools
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema or {"type": "object"},
                    },
                }
                for t in request.tools
            ]
            if request.tool_choice:
                payload["tool_choice"] = request.tool_choice

        # Structured output (JSON Schema mode)
        if request.structured_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "strict": True,
                    "schema": request.structured_schema,
                },
            }

        return payload

    def _parse_response(
        self, http_response: httpx.Response, request: LLMRequest
    ) -> LLMResponse:
        """Translate an HTTP response to a neutral LLMResponse."""
        status = http_response.status_code

        if status == 429:
            retry_after_raw = http_response.headers.get("Retry-After")
            retry_after = float(retry_after_raw) if retry_after_raw else None
            raise LLMRateLimitError(
                f"{self.provider_name} rate limited (429)",
                provider=self.name,
                retry_after_seconds=retry_after,
            )
        if status in (401, 403):
            raise LLMInvalidRequestError(
                f"{self.provider_name} auth error ({status})", provider=self.name
            )
        if status == 400:
            try:
                detail = http_response.json().get("error", {}).get("message", "bad request")
            except Exception:
                detail = "bad request"
            raise LLMInvalidRequestError(
                f"{self.provider_name} invalid request: {detail}", provider=self.name
            )
        if status >= 500:
            raise LLMUnavailableError(
                f"{self.provider_name} server error ({status})", provider=self.name
            )
        if status >= 400:
            raise LLMInvalidRequestError(
                f"{self.provider_name} client error ({status})", provider=self.name
            )

        try:
            body: dict[str, Any] = http_response.json()
        except Exception as exc:
            raise LLMUnavailableError(
                f"{self.provider_name} returned non-JSON response", provider=self.name
            ) from exc

        choice = body["choices"][0]
        message = choice["message"]
        content: str = message.get("content") or ""
        finish_reason: str = choice.get("finish_reason", "stop")

        # Tool calls
        tool_calls: list[LLMToolCall] = []
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function", {})
            raw_args = fn.get("arguments", "{}")
            try:
                args: dict[str, Any] = json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                args = {"raw": raw_args}
            tool_calls.append(
                LLMToolCall(
                    call_id=tc.get("id", str(uuid.uuid4())),
                    tool_name=fn.get("name", ""),
                    arguments=args,
                    raw_arguments=raw_args,
                )
            )

        # Usage
        usage_raw = body.get("usage") or {}
        usage = ProviderUsage(
            input_tokens=int(usage_raw.get("prompt_tokens", 0)),
            output_tokens=int(usage_raw.get("completion_tokens", 0)),
            total_tokens=int(usage_raw.get("total_tokens", 0)),
        )

        # Structured output
        structured: dict[str, Any] | None = None
        if request.structured_schema is not None and content:
            try:
                structured = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                structured = {"content": content}

        return LLMResponse(
            content=content,
            model=body.get("model"),
            finish_reason=finish_reason,
            tool_calls=tuple(tool_calls),
            structured_output=structured,
            usage=usage,
            context=request.context,
            metadata={"provider": self.provider_name},
        )


# ---------------------------------------------------------------------------
# OTel helpers
# ---------------------------------------------------------------------------


def _set_span_attrs(
    span: Any,
    request: LLMRequest,
    provider_name: str,
) -> None:
    from opentelemetry.trace import NonRecordingSpan
    if isinstance(span, NonRecordingSpan):
        return
    span.set_attribute("llm.provider", provider_name)
    span.set_attribute("llm.model", request.model or "default")
    span.set_attribute("llm.message_count", len(request.messages))
    span.set_attribute("llm.tool_count", len(request.tools))
    span.set_attribute("llm.structured_output", request.structured_schema is not None)
    if request.context.correlation_id:
        span.set_attribute("correlation.id", request.context.correlation_id)
    if request.context.workflow_id:
        span.set_attribute("workflow.id", request.context.workflow_id)
    if request.context.execution_id:
        span.set_attribute("execution.id", request.context.execution_id)
