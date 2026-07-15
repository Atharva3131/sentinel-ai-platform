"""Agent registry for versioned agent implementations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.agents.agent import Agent

AgentProvider = Callable[[dict[str, Any] | None], Agent]


def _version_key(version: str) -> tuple[int, ...]:
    cleaned = version.strip().lower().removeprefix("v")
    parts = cleaned.split(".")
    if all(part.isdigit() for part in parts):
        return (1, *[int(part) for part in parts])
    return (0, *[ord(character) for character in cleaned])


@dataclass(slots=True)
class AgentRegistry:
    """Register and resolve agent providers by name and version."""

    _providers: dict[str, dict[str, AgentProvider]] = field(default_factory=dict)
    _default_versions: dict[str, str] = field(default_factory=dict)

    def register(
        self,
        *,
        name: str,
        version: str,
        provider: AgentProvider,
        default: bool = False,
        override: bool = False,
    ) -> None:
        """Register an agent provider under a stable name and version."""
        if not name.strip():
            raise ValueError("Agent name is required")
        if not version.strip():
            raise ValueError("Agent version is required")
        versions = self._providers.setdefault(name, {})
        if not override and version in versions:
            raise ValueError(f"Agent '{name}' version '{version}' is already registered")
        versions[version] = provider

        if default or name not in self._default_versions:
            self._default_versions[name] = version
        else:
            current_default = self._default_versions[name]
            if _version_key(version) >= _version_key(current_default):
                self._default_versions[name] = version

    def resolve(
        self,
        name: str,
        version: str | None = None,
    ) -> tuple[str, AgentProvider]:
        """Resolve a registered agent provider and its version."""
        versions = self._providers.get(name)
        if versions is None:
            raise LookupError(f"Agent '{name}' is not registered")

        resolved_version = version or self._default_versions.get(name)
        if resolved_version is None:
            resolved_version = max(versions, key=_version_key)
        try:
            return resolved_version, versions[resolved_version]
        except KeyError as exc:
            raise LookupError(
                f"Agent '{name}' version '{resolved_version}' is not registered"
            ) from exc

    def names(self) -> tuple[str, ...]:
        """Return registered agent names."""
        return tuple(self._providers)

    def versions(self, name: str) -> tuple[str, ...]:
        """Return registered versions for a given agent."""
        versions = self._providers.get(name)
        if versions is None:
            raise LookupError(f"Agent '{name}' is not registered")
        return tuple(sorted(versions, key=_version_key))
