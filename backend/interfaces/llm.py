"""LLM provider contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.interfaces.common import ProviderContext, ProviderUsage


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """Normalized chat message passed to a provider."""

    role: str
    content: str
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """Provider-neutral generation request."""

    messages: tuple[LLMMessage, ...]
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Provider-neutral generation result."""

    content: str
    model: str | None = None
    finish_reason: str | None = None
    usage: ProviderUsage | None = None
    context: ProviderContext = field(default_factory=ProviderContext)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    """Contract implemented by all chat/completions providers."""

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Produce a single response for the supplied request."""
        ...

