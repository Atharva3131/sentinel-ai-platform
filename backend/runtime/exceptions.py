"""Runtime exception hierarchy."""

from __future__ import annotations

from typing import Any

from backend.exceptions import SentinelError


class RuntimeException(SentinelError):
    """Base exception raised by runtime implementations and factories."""

    def __init__(
        self,
        message: str,
        *,
        runtime_name: str | None = None,
        workflow_id: str | None = None,
        execution_id: str | None = None,
        attempt: int | None = None,
        retryable: bool = False,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.runtime_name = runtime_name
        self.workflow_id = workflow_id
        self.execution_id = execution_id
        self.attempt = attempt
        self.retryable = retryable
        self.timeout_seconds = timeout_seconds
        self.metadata = metadata or {}

