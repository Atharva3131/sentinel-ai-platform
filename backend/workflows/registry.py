"""Workflow registry for versioned workflow definitions."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.workflows.definition import WorkflowDefinition


def _version_key(version: str) -> tuple[int, ...]:
    cleaned = version.strip().lower().removeprefix("v")
    parts = cleaned.split(".")
    if all(part.isdigit() for part in parts):
        return (1, *[int(part) for part in parts])
    return (0, *[ord(character) for character in cleaned])


@dataclass(slots=True)
class WorkflowRegistry:
    """Register and resolve workflow definitions by name and version."""

    _definitions: dict[str, dict[str, WorkflowDefinition]] = field(default_factory=dict)
    _aliases: dict[str, str] = field(default_factory=dict)
    _default_versions: dict[str, str] = field(default_factory=dict)

    def register(
        self,
        definition: WorkflowDefinition,
        *,
        default: bool = False,
        override: bool = False,
    ) -> None:
        """Register a workflow definition."""
        workflow_versions = self._definitions.setdefault(definition.workflow_id, {})
        if not override and definition.version in workflow_versions:
            raise ValueError(
                f"Workflow '{definition.workflow_id}' version '{definition.version}' already exists"
            )
        workflow_versions[definition.version] = definition

        alias = definition.name
        existing_alias = self._aliases.get(alias)
        if existing_alias is not None and existing_alias != definition.workflow_id and not override:
            raise ValueError(f"Workflow name '{alias}' is already registered for another workflow")
        self._aliases[alias] = definition.workflow_id

        if default or definition.workflow_id not in self._default_versions:
            self._default_versions[definition.workflow_id] = definition.version
        else:
            current_default = self._default_versions[definition.workflow_id]
            if _version_key(definition.version) >= _version_key(current_default):
                self._default_versions[definition.workflow_id] = definition.version

    def resolve(self, identifier: str, version: str | None = None) -> WorkflowDefinition:
        """Resolve a workflow by workflow ID or unique name."""
        workflow_id = self._aliases.get(identifier, identifier)
        versions = self._definitions.get(workflow_id)
        if versions is None:
            raise LookupError(f"Workflow '{identifier}' is not registered")

        resolved_version = version or self._default_versions.get(workflow_id)
        if resolved_version is None:
            resolved_version = max(versions, key=_version_key)
        try:
            return versions[resolved_version]
        except KeyError as exc:
            raise LookupError(
                f"Workflow '{workflow_id}' version '{resolved_version}' is not registered"
            ) from exc

    def names(self) -> tuple[str, ...]:
        """Return the registered workflow names."""
        return tuple(self._aliases)

    def versions(self, identifier: str) -> tuple[str, ...]:
        """Return the registered versions for a workflow."""
        workflow_id = self._aliases.get(identifier, identifier)
        versions = self._definitions.get(workflow_id)
        if versions is None:
            raise LookupError(f"Workflow '{identifier}' is not registered")
        return tuple(sorted(versions, key=_version_key))
