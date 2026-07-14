"""Unhandled exception logging middleware."""

from __future__ import annotations

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

_logger = structlog.get_logger(__name__)


class ExceptionLoggingMiddleware:
    """Log unhandled request exceptions with structured context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            _logger.exception(
                "http_request_failed",
                method=scope.get("method"),
                path=scope.get("path"),
                exception_type=type(exc).__name__,
            )
            raise
