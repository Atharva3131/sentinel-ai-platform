"""Tool validation primitives (schemas, permissions, and invariants)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from backend.tools.context import ToolContext
from backend.tools.exceptions import ToolException
from backend.tools.models import ToolInput, ToolMetadata


class ToolValidationError(ToolException):
    """Raised when tool inputs or schemas are invalid."""


class ToolPermissionError(ToolException):
    """Raised when the caller lacks permissions required by a tool."""


@dataclass(slots=True)
class ToolValidator:
    """Validate tool metadata and requests.

    Schema validation uses a conservative JSON Schema subset (types + required fields) to avoid
    introducing new dependencies. Tool implementations may plug in stricter validation by replacing
    this validator with one backed by a full JSON Schema engine.
    """

    def validate_metadata(self, metadata: ToolMetadata) -> None:
        if not metadata.name.strip():
            raise ToolValidationError("Tool name is required", tool_name=metadata.name)

    def validate_permissions(self, metadata: ToolMetadata, context: ToolContext) -> None:
        required = set(metadata.permissions)
        if not required:
            return
        granted = set(context.permissions)
        missing = required - granted
        if missing:
            raise ToolPermissionError(
                "Tool permission denied",
                tool_name=metadata.name,
                tool_version=metadata.version,
                workflow_id=context.workflow_id,
                execution_id=context.execution_id,
                attempt=context.attempt,
                retryable=False,
                metadata={"missing_permissions": sorted(str(permission) for permission in missing)},
            )

    def validate_input(self, metadata: ToolMetadata, tool_input: ToolInput) -> None:
        schema = metadata.input_schema
        if schema is None:
            return
        self._validate_json_schema_subset(schema, tool_input.payload, path="$")

    def validate_output(self, metadata: ToolMetadata, output: Any) -> None:
        schema = metadata.output_schema
        if schema is None:
            return
        self._validate_json_schema_subset(schema, output, path="$")

    def _validate_json_schema_subset(
        self,
        schema: Mapping[str, Any],
        value: Any,
        *,
        path: str,
    ) -> None:
        schema_type = schema.get("type")
        if schema_type is not None:
            if not self._is_type(value, str(schema_type)):
                raise ToolValidationError(
                    f"Schema type mismatch at {path}",
                    metadata={
                        "path": path,
                        "expected": schema_type,
                        "actual": type(value).__name__,
                    },
                )

        if schema_type == "object" or "properties" in schema or "required" in schema:
            if not isinstance(value, dict):
                raise ToolValidationError(
                    f"Expected object at {path}",
                    metadata={"path": path, "actual": type(value).__name__},
                )
            required = schema.get("required") or []
            if isinstance(required, list):
                for key in required:
                    if key not in value:
                        raise ToolValidationError(
                            f"Missing required field at {path}",
                            metadata={"path": path, "missing": key},
                        )
            properties = schema.get("properties") or {}
            if isinstance(properties, dict):
                for prop, prop_schema in properties.items():
                    if prop not in value:
                        continue
                    if isinstance(prop_schema, Mapping):
                        self._validate_json_schema_subset(
                            cast_mapping(prop_schema),
                            value[prop],
                            path=f"{path}.{prop}",
                        )

    def _is_type(self, value: Any, schema_type: str) -> bool:
        if schema_type == "string":
            return isinstance(value, str)
        if schema_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if schema_type == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if schema_type == "boolean":
            return isinstance(value, bool)
        if schema_type == "object":
            return isinstance(value, dict)
        if schema_type == "array":
            return isinstance(value, list)
        if schema_type == "null":
            return value is None
        return True


def cast_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return value
