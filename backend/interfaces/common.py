"""Shared provider contracts and registries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """Execution context propagated to provider calls."""

    correlation_id: str | None = None
    workflow_id: str | None = None
    execution_id: str | None = None
    request_id: str | None = None
    tenant_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Normalized token usage for LLM and embedding providers."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ProviderRegistry[TProvider]:
    """Named provider registry with an optional default provider."""

    def __init__(
        self,
        providers: dict[str, TProvider] | None = None,
        *,
        default_name: str | None = None,
    ) -> None:
        self._providers: dict[str, TProvider] = dict(providers or {})
        self._default_name = default_name
        if self._default_name is not None and self._default_name not in self._providers:
            raise LookupError(f"Default provider '{self._default_name}' is not registered")

    @property
    def default_name(self) -> str | None:
        """Return the configured default provider name."""
        return self._default_name

    def register(
        self,
        name: str,
        provider: TProvider,
        *,
        override: bool = False,
        default: bool = False,
    ) -> None:
        """Register a provider implementation under a stable name."""
        if not override and name in self._providers:
            raise ValueError(f"Provider '{name}' is already registered")
        self._providers[name] = provider
        if default or self._default_name is None:
            self._default_name = name

    def resolve(self, name: str | None = None) -> TProvider:
        """Resolve a named provider or fall back to the default provider."""
        resolved_name = name or self._default_name
        if resolved_name is None:
            raise LookupError("No default provider has been configured")
        try:
            return self._providers[resolved_name]
        except KeyError as exc:
            raise LookupError(f"Provider '{resolved_name}' is not registered") from exc

    def get(self, name: str) -> TProvider | None:
        """Return a provider when registered, otherwise `None`."""
        return self._providers.get(name)

    def names(self) -> tuple[str, ...]:
        """Return the registered provider names."""
        return tuple(self._providers)
