"""Correlation context and structured HTTP access logging middleware."""

from __future__ import annotations

import re
import time
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.contextvars import bind_contextvars, clear_contextvars

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_logger = structlog.get_logger(__name__)


def _safe_identifier(value: str | None) -> str | None:
    return value if value and _SAFE_ID.fullmatch(value) else None


class RequestContextMiddleware:
    """Bind request identifiers to context variables and response headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        correlation_id = _safe_identifier(headers.get("x-correlation-id")) or str(uuid4())
        request_id = str(uuid4())
        workflow_id = _safe_identifier(headers.get("x-workflow-id"))
        execution_id = _safe_identifier(headers.get("x-execution-id"))
        clear_contextvars()
        bind_contextvars(
            correlation_id=correlation_id,
            request_id=request_id,
            workflow_id=workflow_id,
            execution_id=execution_id,
        )
        started = time.perf_counter()
        status_code = 500

        async def send_with_context(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Correlation-ID"] = correlation_id
                response_headers["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except Exception:
            _logger.exception(
                "http_request_failed", method=scope.get("method"), path=scope.get("path")
            )
            raise
        finally:
            _logger.info(
                "http_request_completed",
                method=scope.get("method"),
                path=scope.get("path"),
                status_code=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            clear_contextvars()
