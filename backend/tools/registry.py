"""Tool registry for versioned tool implementations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.tools.tool import Tool

ToolProvider = Callable[[dict[str, Any] | None], Tool]


def _version_key(version: str) -> tuple[int, ...]:
    cleaned = version.strip().lower().removeprefix("v")
    parts = cleaned.split(".")
    if all(part.isdigit() for part in parts):
        return (1, *[int(part) for part in parts])
    return (0, *[ord(character) for character in cleaned])


@dataclass(slots=True)
class ToolRegistry:
    """Register and resolve tool providers by name and version."""

    _providers: dict[str, dict[str, ToolProvider]] = field(default_factory=dict)
    _default_versions: dict[str, str] = field(default_factory=dict)

    def register(
        self,
        *,
        name: str,
        version: str,
        provider: ToolProvider,
        default: bool = False,
        override: bool = False,
    ) -> None:
        """Register a tool provider under a stable name and version."""
        if not name.strip():
            raise ValueError("Tool name is required")
        if not version.strip():
            raise ValueError("Tool version is required")
        versions = self._providers.setdefault(name, {})
        if not override and version in versions:
            raise ValueError(f"Tool '{name}' version '{version}' is already registered")
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
    ) -> tuple[str, ToolProvider]:
        """Resolve a registered tool provider and its version."""
        versions = self._providers.get(name)
        if versions is None:
            raise LookupError(f"Tool '{name}' is not registered")

        resolved_version = version or self._default_versions.get(name)
        if resolved_version is None:
            resolved_version = max(versions, key=_version_key)
        try:
            return resolved_version, versions[resolved_version]
        except KeyError as exc:
            raise LookupError(
                f"Tool '{name}' version '{resolved_version}' is not registered"
            ) from exc

    def names(self) -> tuple[str, ...]:
        """Return registered tool names."""
        return tuple(self._providers)

    def versions(self, name: str) -> tuple[str, ...]:
        """Return registered versions for a given tool."""
        versions = self._providers.get(name)
        if versions is None:
            raise LookupError(f"Tool '{name}' is not registered")
        return tuple(sorted(versions, key=_version_key))
