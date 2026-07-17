"""EmbeddingFactory — versioned registry for EmbeddingProvider instances."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.retrieval.exceptions import EmbeddingError
from backend.retrieval.providers import EmbeddingProvider

EmbeddingProviderFactory = Callable[[dict[str, Any] | None], EmbeddingProvider]


def _parse_semver(version: str) -> tuple[int, ...]:
    """Parse a semver string into a comparable integer tuple."""
    parts = version.split(".")
    result = []
    for part in parts:
        numeric = "".join(c for c in part if c.isdigit())
        result.append(int(numeric) if numeric else 0)
    return tuple(result)


@dataclass(slots=True)
class EmbeddingFactory:
    """Versioned registry of EmbeddingProvider factories.

    Providers are registered under a ``(name, version)`` key. When ``version``
    is omitted in ``resolve()`` / ``create()``, the default version is used.
    The highest registered semver is auto-promoted as default unless an explicit
    ``default=True`` was set during registration.

    Usage::

        factory = EmbeddingFactory()
        factory.register(
            name="azure-openai",
            version="1.0.0",
            provider=lambda deps: AzureOpenAIEmbeddingProvider(deps),
            default=True,
        )
        provider = factory.create("azure-openai", dependencies={"client": client})
    """

    _providers: dict[str, dict[str, EmbeddingProviderFactory]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    _default_versions: dict[str, str] = field(default_factory=dict)

    def register(
        self,
        *,
        name: str,
        version: str,
        provider: EmbeddingProviderFactory,
        default: bool = False,
        override: bool = False,
    ) -> None:
        """Register a provider factory under ``(name, version)``.

        Raises:
            EmbeddingError: If the version is already registered and
                ``override=False``.
        """
        if version in self._providers[name] and not override:
            raise EmbeddingError(
                f"EmbeddingProvider '{name}' version '{version}' is already registered; "
                "pass override=True to replace it",
                provider_name=name,
            )
        self._providers[name][version] = provider
        if default or name not in self._default_versions:
            self._default_versions[name] = version
        else:
            current_default = self._default_versions[name]
            try:
                if _parse_semver(version) > _parse_semver(current_default):
                    self._default_versions[name] = version
            except (ValueError, TypeError):
                pass

    def resolve(
        self, name: str, version: str | None = None
    ) -> tuple[str, EmbeddingProviderFactory]:
        """Return ``(resolved_version, factory)`` for the given provider name.

        Raises:
            EmbeddingError: If the name or version is not registered.
        """
        if name not in self._providers:
            raise EmbeddingError(
                f"EmbeddingProvider '{name}' is not registered",
                provider_name=name,
            )
        resolved = version or self._default_versions.get(name)
        if resolved is None or resolved not in self._providers[name]:
            available = ", ".join(self._providers[name])
            raise EmbeddingError(
                f"EmbeddingProvider '{name}' version '{version}' not found "
                f"(available: {available})",
                provider_name=name,
            )
        return resolved, self._providers[name][resolved]

    def create(
        self,
        name: str,
        version: str | None = None,
        *,
        dependencies: dict[str, Any] | None = None,
    ) -> EmbeddingProvider:
        """Instantiate and return an EmbeddingProvider."""
        _, factory = self.resolve(name, version)
        return factory(dependencies)

    def names(self) -> tuple[str, ...]:
        """Return all registered provider names."""
        return tuple(sorted(self._providers))

    def versions(self, name: str) -> tuple[str, ...]:
        """Return registered versions for a provider name."""
        if name not in self._providers:
            raise EmbeddingError(
                f"EmbeddingProvider '{name}' is not registered",
                provider_name=name,
            )
        return tuple(sorted(self._providers[name]))
