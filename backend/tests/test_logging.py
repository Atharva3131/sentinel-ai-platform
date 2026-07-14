"""Structured logging tests."""

from __future__ import annotations

import io
import json
from collections.abc import Generator
from contextlib import redirect_stdout

import httpx
import pytest
import structlog
from fastapi import FastAPI
from structlog.contextvars import bind_contextvars, clear_contextvars

from backend.application.factory import create_application
from backend.configuration.settings import (
    AppSettings,
    LoggingSettings,
    OpenTelemetrySettings,
)
from backend.logging import configure_logging
from backend.logging.context import add_opentelemetry_context
from backend.middleware import ExceptionLoggingMiddleware, RequestContextMiddleware


@pytest.fixture(autouse=True)
def reset_contextvars() -> Generator[None, None, None]:
    """Prevent cross-test leakage of structlog context variables."""
    clear_contextvars()
    yield
    clear_contextvars()


def test_pretty_logs_include_request_context_and_headers() -> None:
    """Development logging should emit readable output with propagated IDs."""
    settings = AppSettings(
        environment="development",
        logging=LoggingSettings(level="INFO", json_output=False),
        opentelemetry=OpenTelemetrySettings(enabled=False),
        neo4j={"enabled": False},
    )
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        app = create_application(settings)
        async def run() -> None:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as client:
                    response = await client.get(
                        "/health/live",
                        headers={
                            "X-Correlation-ID": "corr-123",
                            "X-Workflow-ID": "workflow-456",
                            "X-Execution-ID": "execution-789",
                        },
                    )

            assert response.status_code == 200
            assert response.headers["X-Correlation-ID"] == "corr-123"
            assert response.headers["X-Workflow-ID"] == "workflow-456"
            assert response.headers["X-Execution-ID"] == "execution-789"

        import asyncio

        asyncio.run(run())

    output = buffer.getvalue()
    assert "http_request_completed" in output
    assert "corr-123" in output
    assert "workflow-456" in output
    assert "execution-789" in output
    assert not output.lstrip().startswith("{")


def test_json_logs_include_otel_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production logging should emit JSON with OpenTelemetry span identifiers."""
    class _SpanContext:
        trace_id = 0x1234
        span_id = 0x5678
        trace_flags = type("TraceFlags", (), {"sampled": True})()

        @property
        def is_valid(self) -> bool:
            return True

    class _Span:
        def get_span_context(self) -> _SpanContext:
            return _SpanContext()

    monkeypatch.setattr("backend.logging.context.trace.get_current_span", lambda: _Span())
    bind_contextvars(correlation_id="corr-json", workflow_id="wf-json", execution_id="ex-json")
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        configure_logging(LoggingSettings(level="INFO", json_output=True))
        logger = structlog.get_logger("test.logging")
        logger.info("structured_event", payload="ok")

    record = json.loads(buffer.getvalue().splitlines()[-1])
    assert record["event"] == "structured_event"
    assert record["payload"] == "ok"
    assert record["correlation_id"] == "corr-json"
    assert record["workflow_id"] == "wf-json"
    assert record["execution_id"] == "ex-json"
    assert record["trace_id"] == "00000000000000000000000000001234"
    assert record["span_id"] == "0000000000005678"
    assert record["trace_sampled"] is True


def test_exception_middleware_logs_failures_with_context() -> None:
    """Unhandled exceptions should be logged once with request context."""
    app = FastAPI()
    app.add_middleware(ExceptionLoggingMiddleware)
    app.add_middleware(RequestContextMiddleware)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        configure_logging(LoggingSettings(level="INFO", json_output=True))
        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.get(
                    "/boom",
                    headers={
                        "X-Correlation-ID": "corr-exc",
                        "X-Workflow-ID": "workflow-exc",
                        "X-Execution-ID": "execution-exc",
                    },
                )

        import asyncio

        response = asyncio.run(run())

    assert response.status_code == 500
    output = buffer.getvalue()
    assert "http_request_failed" in output
    assert "corr-exc" in output
    assert "workflow-exc" in output
    assert "execution-exc" in output


def test_otel_processor_omits_fields_when_no_span_is_active() -> None:
    """The trace processor should be a no-op when no span is active."""
    event = add_opentelemetry_context(structlog.get_logger(), "info", {"event": "test"})
    assert event == {"event": "test"}
