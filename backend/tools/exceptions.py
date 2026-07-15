"""Tool framework exceptions."""

from __future__ import annotations

from typing import Any

from backend.exceptions import SentinelError


class ToolException(SentinelError):
    """Base exception raised by tool implementations and the tool runtime."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        tool_version: str | None = None,
        workflow_id: str | None = None,
        execution_id: str | None = None,
        attempt: int | None = None,
        retryable: bool = False,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.tool_version = tool_version
        self.workflow_id = workflow_id
        self.execution_id = execution_id
        self.attempt = attempt
        self.retryable = retryable
        self.timeout_seconds = timeout_seconds
        self.metadata = metadata or {}
