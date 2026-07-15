"""EvaluationRegistry — versioned registration and resolution of evaluation strategies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.evaluation.exceptions import EvaluationRegistryError
from backend.evaluation.strategy import EvaluationStrategy

EvaluationStrategyProvider = Callable[[dict[str, Any] | None], EvaluationStrategy]


def _version_key(version: str) -> tuple[int, ...]:
    cleaned = version.strip().lower().removeprefix("v")
    parts = cleaned.split(".")
    if all(part.isdigit() for part in parts):
        return (1, *[int(part) for part in parts])
    return (0, *[ord(c) for c in cleaned])


@dataclass(slots=True)
class EvaluationRegistry:
    """Register and resolve EvaluationStrategy providers by name and version.

    Follows the same version-resolution contract as AgentRegistry and ToolRegistry:
    the highest semver is the default unless explicitly overridden.
    """

    _providers: dict[str, dict[str, EvaluationStrategyProvider]] = field(
        default_factory=dict
    )
    _default_versions: dict[str, str] = field(default_factory=dict)

    def register(
        self,
        *,
        name: str,
        version: str,
        provider: EvaluationStrategyProvider,
        default: bool = False,
        override: bool = False,
    ) -> None:
        """Register a strategy provider under a stable name and version.

        Args:
            name: Stable strategy name (e.g. "latency", "tool_success").
            version: Semver string (e.g. "1.0.0").
            provider: Callable that returns an EvaluationStrategy instance.
            default: Force this version to be the default resolution target.
            override: Allow replacing an existing registration for the same version.
        """
        if not name.strip():
            raise ValueError("Strategy name is required")
        if not version.strip():
            raise ValueError("Strategy version is required")
        versions = self._providers.setdefault(name, {})
        if not override and version in versions:
            raise ValueError(
                f"Strategy '{name}' version '{version}' is already registered"
            )
        versions[version] = provider

        if default or name not in self._default_versions:
            self._default_versions[name] = version
        else:
            current = self._default_versions[name]
            if _version_key(version) >= _version_key(current):
                self._default_versions[name] = version

    def resolve(
        self,
        name: str,
        version: str | None = None,
    ) -> tuple[str, EvaluationStrategyProvider]:
        """Return the resolved version string and provider for a strategy.

        Raises:
            EvaluationRegistryError: When the strategy or version is not registered.
        """
        versions = self._providers.get(name)
        if versions is None:
            raise EvaluationRegistryError(
                f"Evaluation strategy '{name}' is not registered"
            )
        resolved = version or self._default_versions.get(name)
        if resolved is None:
            resolved = max(versions, key=_version_key)
        if resolved not in versions:
            raise EvaluationRegistryError(
                f"Evaluation strategy '{name}' version '{resolved}' is not registered"
            )
        return resolved, versions[resolved]

    def create(
        self,
        name: str,
        version: str | None = None,
        *,
        dependencies: dict[str, Any] | None = None,
    ) -> EvaluationStrategy:
        """Resolve and instantiate a strategy via its provider.

        Args:
            name: Strategy name.
            version: Optional version override.
            dependencies: Optional dependency map forwarded to the provider.
        """
        _, provider = self.resolve(name, version)
        return provider(dependencies)

    def names(self) -> tuple[str, ...]:
        """Return all registered strategy names."""
        return tuple(self._providers)

    def versions(self, name: str) -> tuple[str, ...]:
        """Return sorted registered versions for a given strategy name."""
        versions = self._providers.get(name)
        if versions is None:
            raise EvaluationRegistryError(
                f"Evaluation strategy '{name}' is not registered"
            )
        return tuple(sorted(versions, key=_version_key))
