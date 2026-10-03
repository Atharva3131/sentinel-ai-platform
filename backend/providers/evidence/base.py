"""Shared HTTP base for evidence providers.

All three production evidence adapters (Prometheus, Elastic, OTLP) use the
same httpx lifecycle, retry loop, error mapping, and OTel helpers defined
here.  This avoids duplicating ~150 lines of infrastructure across three
files.

Error taxonomy (mirrors the LLM adapter):
  HTTP 401/403           → EvidenceAuthError
  HTTP 400               → EvidenceInvalidRequestError
  HTTP 5xx / connect err → EvidenceUnavailableError
  timeout                → EvidenceTimeoutError
  cancellation           → EvidenceCancelledError
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer("sentinel.evidence")


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class EvidenceProviderError(Exception):
    """Base for all evidence provider errors."""
    def __init__(self, msg: str, *, provider: str | None = None) -> None:
        super().__init__(msg)
        self.provider = provider


class EvidenceTimeoutError(EvidenceProviderError):
    def __init__(self, msg: str, *, provider: str | None = None,
                 timeout_seconds: float | None = None) -> None:
        super().__init__(msg, provider=provider)
        self.timeout_seconds = timeout_seconds


class EvidenceUnavailableError(EvidenceProviderError):
    pass


class EvidenceAuthError(EvidenceProviderError):
    pass


class EvidenceInvalidRequestError(EvidenceProviderError):
    pass


class EvidenceCancelledError(EvidenceProviderError):
    pass


# ---------------------------------------------------------------------------
# Shared HTTP client base
# ---------------------------------------------------------------------------


@dataclass
class EvidenceHttpBase:
    """Reusable HTTP base with retry, timeout, OTel and structured logging.

    Subclasses call ``_get`` / ``_post`` which handle the full lifecycle.
    """

    provider_name: str
    base_url: str
    timeout_seconds: float = 30.0
    max_retries: int = 3
    retry_min_wait: float = 0.5
    retry_max_wait: float = 5.0
    # Optional Bearer token (never logged)
    _api_key: str | None = field(default=None, init=False, repr=False)
    # Optional HTTP Basic auth (password never logged)
    _username: str | None = field(default=None, init=False, repr=False)
    _password: str | None = field(default=None, init=False, repr=False)
    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _configure_auth(
        self,
        *,
        api_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Called by concrete subclasses after dataclass init."""
        self._api_key = api_key
        self._username = username
        self._password = password

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            headers: dict[str, str] = {"Accept": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            auth = (
                httpx.BasicAuth(self._username, self._password or "")
                if self._username else None
            )
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                auth=auth,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        return self._http

    async def close(self) -> None:
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout_override: float | None = None,
        span_name: str | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET", path,
            params=params,
            timeout_override=timeout_override,
            span_name=span_name,
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout_override: float | None = None,
        span_name: str | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST", path,
            json=json,
            timeout_override=timeout_override,
            span_name=span_name,
            correlation_id=correlation_id,
            incident_id=incident_id,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout_override: float | None = None,
        span_name: str | None = None,
        correlation_id: str | None = None,
        incident_id: str | None = None,
    ) -> dict[str, Any]:
        effective_name = span_name or f"evidence.{self.provider_name}.{method.lower()}"

        with _tracer.start_as_current_span(
            effective_name, kind=trace.SpanKind.CLIENT
        ) as span:
            _set_span_attrs(
                span,
                provider=self.provider_name,
                path=path,
                correlation_id=correlation_id,
                incident_id=incident_id,
            )
            t0 = time.monotonic()
            try:
                result = await self._execute_with_retry(
                    method, path,
                    params=params,
                    json_body=json,
                    timeout_override=timeout_override,
                )
            except EvidenceProviderError:
                span.set_status(StatusCode.ERROR)
                raise
            except Exception as exc:
                span.set_status(StatusCode.ERROR, str(exc))
                raise EvidenceUnavailableError(
                    str(exc), provider=self.provider_name
                ) from exc

            latency_ms = (time.monotonic() - t0) * 1000
            span.set_attribute("evidence.latency_ms", round(latency_ms, 2))
            span.set_status(StatusCode.OK)

        log.debug(
            "evidence_request_complete",
            provider=self.provider_name,
            path=path,
            latency_ms=round(latency_ms, 2),
            correlation_id=correlation_id,
        )
        return result

    async def _execute_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        timeout_override: float | None,
    ) -> dict[str, Any]:
        attempt = 0
        wait = self.retry_min_wait

        while True:
            attempt += 1
            try:
                return await self._send_once(
                    method, path, params=params, json_body=json_body,
                    timeout_override=timeout_override,
                )
            except (EvidenceUnavailableError, EvidenceTimeoutError):
                if attempt > self.max_retries:
                    raise
                log.warning(
                    "evidence_request_retrying",
                    provider=self.provider_name,
                    attempt=attempt,
                    wait=wait,
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2.0, self.retry_max_wait)
            except (EvidenceAuthError, EvidenceInvalidRequestError,
                    EvidenceCancelledError):
                raise

    async def _send_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json_body: dict[str, Any] | None,
        timeout_override: float | None,
    ) -> dict[str, Any]:
        effective_timeout = (
            min(timeout_override, self.timeout_seconds)
            if timeout_override is not None
            else self.timeout_seconds
        )
        try:
            response = await asyncio.wait_for(
                self._client().request(
                    method, path,
                    params=params,
                    json=json_body,
                ),
                timeout=effective_timeout,
            )
        except TimeoutError:
            raise EvidenceTimeoutError(
                f"{self.provider_name} timed out after {effective_timeout}s",
                provider=self.provider_name,
                timeout_seconds=effective_timeout,
            ) from None
        except httpx.ConnectError as exc:
            raise EvidenceUnavailableError(
                f"{self.provider_name} unreachable: {exc}",
                provider=self.provider_name,
            ) from exc
        except httpx.HTTPError as exc:
            raise EvidenceUnavailableError(
                f"{self.provider_name} HTTP error: {exc}",
                provider=self.provider_name,
            ) from exc

        return self._check_status(response)

    def _check_status(self, response: httpx.Response) -> dict[str, Any]:
        status = response.status_code
        if status in (401, 403):
            raise EvidenceAuthError(
                f"{self.provider_name} auth error ({status})",
                provider=self.provider_name,
            )
        if status == 400:
            raise EvidenceInvalidRequestError(
                f"{self.provider_name} bad request (400)",
                provider=self.provider_name,
            )
        if status >= 500:
            raise EvidenceUnavailableError(
                f"{self.provider_name} server error ({status})",
                provider=self.provider_name,
            )
        if status >= 400:
            raise EvidenceInvalidRequestError(
                f"{self.provider_name} client error ({status})",
                provider=self.provider_name,
            )
        try:
            return response.json()  # type: ignore[no-any-return]
        except Exception as exc:
            raise EvidenceUnavailableError(
                f"{self.provider_name} non-JSON response",
                provider=self.provider_name,
            ) from exc


def _set_span_attrs(
    span: Any,
    *,
    provider: str,
    path: str,
    correlation_id: str | None,
    incident_id: str | None,
) -> None:
    from opentelemetry.trace import NonRecordingSpan
    if isinstance(span, NonRecordingSpan):
        return
    span.set_attribute("evidence.provider", provider)
    span.set_attribute("evidence.path", path)
    if correlation_id:
        span.set_attribute("correlation.id", correlation_id)
    if incident_id:
        span.set_attribute("incident.id", incident_id)
