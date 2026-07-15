"""Runtime registry for named runtime implementations."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.runtime.contracts import WorkflowRuntime


@dataclass(slots=True)
class RuntimeRegistry:
    """Register and resolve multiple runtime implementations by name."""

    runtimes: dict[str, WorkflowRuntime] = field(default_factory=dict)
    default_name: str | None = None

    def register(
        self,
        name: str,
        runtime: WorkflowRuntime,
        *,
        default: bool = False,
        override: bool = False,
    ) -> None:
        """Register a runtime under a stable name."""
        if not override and name in self.runtimes:
            raise ValueError(f"Runtime '{name}' is already registered")
        self.runtimes[name] = runtime
        if default or self.default_name is None:
            self.default_name = name

    def resolve(self, name: str | None = None) -> WorkflowRuntime:
        """Return a named runtime or the configured default runtime."""
        resolved_name = name or self.default_name
        if resolved_name is None:
            raise LookupError("No runtime has been registered")
        try:
            return self.runtimes[resolved_name]
        except KeyError as exc:
            raise LookupError(f"Runtime '{resolved_name}' is not registered") from exc

    def names(self) -> tuple[str, ...]:
        """Return registered runtime names in insertion order."""
        return tuple(self.runtimes)
