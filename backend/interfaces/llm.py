"""LLM provider contracts — extended for tool calls, structured output, and errors.

This is the ONLY place in the codebase that defines the LLM surface.
No domain, service, or agent module may import a vendor SDK directly.
All LLM access goes through LLMProvider.generate() or LLMProvider.generate_structured().

Extension over the original interface:
  - LLMToolDefinition   — describes a callable tool the model may invoke
  - LLMToolCall         — a tool invocation emitted by the model
  - LLMToolResult       — the result returned back to the model
  - LLMStreamChunk      — for future streaming support (defined now, used later)
  - structured_schema   — optional JSON Schema on LLMRequest for structured output
  - tool_calls          — list of tool invocations on LLMResponse
  - LLMError hierarchy  — typed errors for timeout, rate-limit, and unavailability
  - timeout_seconds     — per-request timeout budget
  - cancellation_token  — cooperative cancellation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.interfaces.common import ProviderContext, ProviderUsage

# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """Normalized chat message passed to a provider."""

    role: str   # "system" | "user" | "assistant" | "tool"
    content: str
    name: str | None = None
    tool_call_id: str | None = None  # populated for role="tool" result messages
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tool call types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMToolDefinition:
    """Describes a callable tool the model may invoke.

    ``input_schema`` is a JSON Schema dict describing the expected arguments.
    Provider adapters translate this into their native tool/function format.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMToolCall:
    """A single tool invocation emitted by the model in its response."""

    call_id: str                    # provider-assigned unique call identifier
    tool_name: str                  # must match LLMToolDefinition.name
    arguments: dict[str, Any]       # parsed JSON arguments
    raw_arguments: str = ""         # raw JSON string from the model (for audit)


@dataclass(frozen=True, slots=True)
class LLMToolResult:
    """The result of executing one tool call, fed back to the model."""

    call_id: str
    tool_name: str
    content: str                    # stringified result the model will see
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Provider-neutral generation request.

    ``structured_schema``: when set, the provider MUST return JSON conforming
    to this JSON Schema.  Providers that do not support structured output
    should raise ``LLMStructuredOutputError``.

    ``tools``: when provided, the model may emit ``tool_calls`` in its response.

    ``tool_choice``: optional hint — "auto" | "none" | a specific tool name.

    ``timeout_seconds``: per-request budget.  None means no timeout.

    ``cancellation_token``: cooperative cancellation.  The provider should check
    ``is_set()`` before making network calls and raise ``LLMCancelledError`` when set.
    """

    messages: tuple[LLMMessage, ...]
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    tools: tuple[LLMToolDefinition, ...] = ()
    tool_choice: str | None = None
    structured_schema: dict[str, Any] | None = None
    timeout_seconds: float | None = None
    cancellation_token: Any | None = None   # LLMCancellationToken structural
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Provider-neutral generation result."""

    content: str                            # text content (may be "" when tool_calls present)
    model: str | None = None
    finish_reason: str | None = None        # "stop" | "tool_calls" | "length" | "error"
    tool_calls: tuple[LLMToolCall, ...] = ()
    structured_output: dict[str, Any] | None = None  # parsed JSON when structured_schema used
    usage: ProviderUsage | None = None
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def is_structured(self) -> bool:
        return self.structured_output is not None


# ---------------------------------------------------------------------------
# Streaming (defined now, used by future streaming providers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMStreamChunk:
    """One chunk from a streaming response."""

    delta: str
    finish_reason: str | None = None
    tool_call_delta: LLMToolCall | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base class for all LLM provider errors."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class LLMTimeoutError(LLMError):
    """Raised when a request exceeds its timeout budget."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.timeout_seconds = timeout_seconds


class LLMRateLimitError(LLMError):
    """Raised when the provider rate-limits the request."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after_seconds = retry_after_seconds


class LLMUnavailableError(LLMError):
    """Raised when the provider is unreachable or returns a 5xx error."""


class LLMCancelledError(LLMError):
    """Raised when the request was cancelled via the cancellation token."""


class LLMStructuredOutputError(LLMError):
    """Raised when the provider cannot return structured output matching the schema."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        raw_content: str = "",
    ) -> None:
        super().__init__(message, provider=provider)
        self.raw_content = raw_content


class LLMInvalidRequestError(LLMError):
    """Raised when the request is malformed (bad model, exceeds context length, etc.)."""


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """Contract implemented by all chat/completions providers.

    Concrete adapters (OpenAI, Anthropic, fake test doubles) must satisfy
    this protocol structurally — they do NOT need to inherit from it.

    ``generate`` is the primary method for standard completion and tool-calling.

    ``generate_structured`` is a convenience wrapper that sets ``structured_schema``
    on the request and validates the output; providers may override it for
    native JSON-mode support.
    """

    @property
    def name(self) -> str:
        """Return the stable provider name for registry lookup and logging."""
        ...

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Produce a single response for the supplied request.

        Raises:
            LLMTimeoutError: when the request exceeds its timeout budget.
            LLMRateLimitError: when the provider rate-limits the request.
            LLMUnavailableError: when the provider is unreachable.
            LLMCancelledError: when the cancellation token is set.
            LLMInvalidRequestError: for malformed requests.
            LLMError: for any other provider error.
        """
        ...


# ---------------------------------------------------------------------------
# Cancellation token protocol (structural — no import dependency)
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMCancellationToken(Protocol):
    """Cooperative cancellation for LLM requests."""

    def is_set(self) -> bool:
        """Return True when the request should be cancelled."""
        ...
