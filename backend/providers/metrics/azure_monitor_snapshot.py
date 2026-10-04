"""AzureMonitorMetricsSnapshot — production MetricsSnapshotPort implementation.

Queries Azure Monitor / Application Insights for metric values used by
``VerificationEngine`` to compare before/after service state after a
remediation deployment.

API used:
    GET https://api.applicationinsights.io/v1/apps/{app_id}/metrics/{metricId}
        ?timespan=PT{window}S
        &aggregation=avg

Reference:
    https://learn.microsoft.com/en-us/rest/api/application-insights/metrics/get

Authentication (in priority order):
  1. API key — ``x-api-key: <key>`` header (no AAD token needed).
  2. AAD Bearer token — obtained from the injected ``credential`` object via
     ``azure.identity.aio`` (Managed Identity or Service Principal).

Metric name mapping:
    ``VerificationEngine`` passes generic domain metric names (e.g. ``"error_rate"``,
    ``"latency_p99"``) from ``VerificationPlan.metrics_to_compare``.  This adapter
    maps them to Application Insights metric IDs and normalises the raw response
    values to float.  Unmapped or unavailable metrics return 0.0 so
    ``VerificationEngine`` always receives a complete dict.

Error handling:
  * Any HTTP error, timeout, malformed response, or missing metric silently
    returns 0.0 for that metric — the engine is designed to treat 0.0 as
    "unknown / no degradation detected".
  * Errors are logged with structlog at WARNING level so they are visible in
    production without surfacing as exceptions to the orchestrator.

Design principles:
  * ``VerificationEngine`` remains completely cloud-agnostic — it only calls
    ``snapshot()`` via the protocol.
  * No Prometheus, no fake objects in production.
  * Single-responsibility: this class only queries metrics; it does not
    collect Evidence items or interact with the incident model.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from backend.configuration.settings import AzureMonitorMetricsSettings

if TYPE_CHECKING:
    from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
    AzureCredential = DefaultAzureCredential | ClientSecretCredential

log = structlog.get_logger(__name__)
_tracer = trace.get_tracer("sentinel.azure_monitor")

_PROVIDER_NAME = "azure_monitor"

# ---------------------------------------------------------------------------
# Metric name → Application Insights metric ID mapping
# ---------------------------------------------------------------------------

# Callers pass domain-neutral names; this table translates them to the
# Application Insights REST metric IDs accepted by the /metrics endpoint.
# Any name absent from this table is forwarded as-is (allows callers to pass
# native AI metric IDs directly).
_METRIC_ID_MAP: dict[str, str] = {
    # Error / failure rates
    "error_rate":              "requests/failed",
    "failure_rate":            "requests/failed",
    # Request throughput
    "request_rate":            "requests/count",
    "requests_per_second":     "requests/count",
    # Latency
    "latency_ms":              "requests/duration",
    "latency_p99":             "requests/duration",
    "response_time_ms":        "requests/duration",
    # Availability
    "availability":            "availabilityResults/availabilityPercentage",
    # Exceptions
    "exception_rate":          "exceptions/count",
    # Dependencies
    "dependency_failure_rate": "dependencies/failed",
    "dependency_duration_ms":  "dependencies/duration",
    # Custom / performance counter names pass through unchanged
}

# Metrics whose raw value should be treated as a rate (divide by window) — not
# currently applied since the API returns the aggregated average over the
# window, which is already the right unit for threshold comparison.
_RATE_METRICS: frozenset[str] = frozenset()

# The AAD scope required to call the Application Insights REST API
_AI_API_SCOPE = "https://api.applicationinsights.io/.default"


# ---------------------------------------------------------------------------
# AzureMonitorMetricsSnapshot
# ---------------------------------------------------------------------------


class AzureMonitorMetricsSnapshot:
    """Production ``MetricsSnapshotPort`` backed by Application Insights.

    Instantiate via the classmethod factory:

        snapshot = AzureMonitorMetricsSnapshot.from_settings(settings, credential)

    ``credential`` is only consulted when no ``api_key`` is configured.

    Thread/async safety: the httpx client is created lazily on first use and
    reused across all calls within the same application lifetime.
    """

    def __init__(
        self,
        app_id: str,
        *,
        api_key: str | None = None,
        credential: AzureCredential | None = None,
        base_url: str = "https://api.applicationinsights.io",
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_min_wait_seconds: float = 0.5,
        retry_max_wait_seconds: float = 10.0,
    ) -> None:
        if not app_id:
            raise ValueError(
                "AzureMonitorMetricsSnapshot requires a non-empty app_id. "
                "Set SENTINEL_AZURE_MONITOR_METRICS__APP_ID."
            )
        self._app_id = app_id
        self._api_key = api_key
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_min_wait = retry_min_wait_seconds
        self._retry_max_wait = retry_max_wait_seconds
        self._http: httpx.AsyncClient | None = None

    # ── Factory ───────────────────────────────────────────────────────────

    @classmethod
    def from_settings(
        cls,
        settings: AzureMonitorMetricsSettings,
        credential: AzureCredential | None = None,
    ) -> AzureMonitorMetricsSnapshot:
        """Build an instance from ``AzureMonitorMetricsSettings``.

        Pass the shared Azure credential when no API key is configured.
        """
        return cls(
            app_id=settings.app_id,
            api_key=(
                settings.api_key.get_secret_value()
                if settings.api_key is not None else None
            ),
            credential=credential,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
            retry_min_wait_seconds=settings.retry_min_wait_seconds,
            retry_max_wait_seconds=settings.retry_max_wait_seconds,
        )

    # ── MetricsSnapshotPort ───────────────────────────────────────────────

    async def snapshot(
        self,
        service: str,
        metric_names: tuple[str, ...],
        *,
        window_seconds: float = 60.0,
    ) -> dict[str, float]:
        """Query Application Insights for *metric_names* and return a float dict.

        All requested names are present in the result — missing/failed metrics
        default to 0.0.  Never raises.
        """
        result: dict[str, float] = {name: 0.0 for name in metric_names}

        for name in metric_names:
            value = await self._fetch_metric(service, name, window_seconds)
            result[name] = value

        return result

    # ── HTTP helpers ──────────────────────────────────────────────────────

    def _get_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            headers: dict[str, str] = {"Accept": "application/json"}
            if self._api_key:
                headers["x-api-key"] = self._api_key
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=httpx.Timeout(self._timeout_seconds),
            )
        return self._http

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        if self._http and not self._http.is_closed:
            await self._http.aclose()

    async def _get_auth_headers(self) -> dict[str, str]:
        """Return auth headers — empty when api_key is configured (already in client)."""
        if self._api_key:
            return {}  # Already set as a permanent header on the client
        if self._credential is None:
            return {}
        try:
            token = await self._credential.get_token(_AI_API_SCOPE)
            return {"Authorization": f"Bearer {token.token}"}
        except Exception as exc:
            log.warning(
                "azure_monitor_token_acquisition_failed",
                error=str(exc),
            )
            return {}

    async def _fetch_metric(
        self,
        service: str,
        metric_name: str,
        window_seconds: float,
    ) -> float:
        """Fetch one metric from Application Insights, returning 0.0 on any error."""
        metric_id = _METRIC_ID_MAP.get(metric_name, metric_name)
        # Application Insights timespan format: ISO 8601 duration
        timespan = f"PT{int(window_seconds)}S"

        path = f"/v1/apps/{self._app_id}/metrics/{metric_id}"
        params: dict[str, str] = {
            "timespan": timespan,
            "aggregation": "avg",
        }

        with _tracer.start_as_current_span(
            "azure_monitor.metrics.get", kind=trace.SpanKind.CLIENT
        ) as span:
            span.set_attribute("azure_monitor.app_id", self._app_id)
            span.set_attribute("azure_monitor.metric_id", metric_id)
            span.set_attribute("azure_monitor.service", service)
            span.set_attribute("azure_monitor.window_seconds", window_seconds)

            t0 = time.monotonic()
            try:
                value = await self._fetch_with_retry(path, params)
                latency_ms = (time.monotonic() - t0) * 1000
                span.set_attribute("azure_monitor.latency_ms", round(latency_ms, 2))
                span.set_attribute("azure_monitor.value", value)
                span.set_status(StatusCode.OK)
                return value
            except Exception as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                span.set_status(StatusCode.ERROR, str(exc))
                log.warning(
                    "azure_monitor_metric_fetch_failed",
                    service=service,
                    metric_name=metric_name,
                    metric_id=metric_id,
                    error=str(exc),
                    latency_ms=round(latency_ms, 2),
                )
                return 0.0

    async def _fetch_with_retry(
        self,
        path: str,
        params: dict[str, str],
    ) -> float:
        """Execute the HTTP request with exponential backoff retry."""
        attempt = 0
        wait = self._retry_min_wait

        while True:
            attempt += 1
            try:
                return await self._send_once(path, params)
            except _RetryableError:
                if attempt > self._max_retries:
                    raise
                log.warning(
                    "azure_monitor_request_retrying",
                    path=path,
                    attempt=attempt,
                    wait_seconds=wait,
                )
                await asyncio.sleep(wait)
                wait = min(wait * 2.0, self._retry_max_wait)
            except _FatalError:
                raise

    async def _send_once(self, path: str, params: dict[str, str]) -> float:
        """Issue one HTTP GET and parse the Application Insights response."""
        auth_headers = await self._get_auth_headers()
        client = self._get_client()

        try:
            response = await asyncio.wait_for(
                client.get(path, params=params, headers=auth_headers),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise _RetryableError(
                f"Application Insights request timed out after {self._timeout_seconds}s"
            ) from exc
        except httpx.ConnectError as exc:
            raise _RetryableError(
                f"Application Insights unreachable: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            raise _RetryableError(
                f"Application Insights HTTP transport error: {exc}"
            ) from exc

        status = response.status_code
        if status in (401, 403):
            raise _FatalError(
                f"Application Insights auth error ({status}) — "
                "check app_id and api_key / AAD credential."
            )
        if status == 404:
            # Metric not found for this app — treat as 0.0 rather than error
            raise _MetricNotFoundError(f"Metric not found: {path}")
        if status >= 500:
            raise _RetryableError(
                f"Application Insights server error ({status})"
            )
        if status >= 400:
            raise _FatalError(
                f"Application Insights client error ({status}): {response.text[:200]}"
            )

        return _parse_metric_response(response)

    def __repr__(self) -> str:
        return (
            f"AzureMonitorMetricsSnapshot("
            f"app_id={self._app_id!r}, "
            f"base_url={self._base_url!r})"
        )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_metric_response(response: httpx.Response) -> float:
    """Extract the average metric value from an Application Insights response.

    The Application Insights metrics API returns:
    {
      "value": {
        "start": "...", "end": "...", "interval": "...",
        "segments": [{"avg": <float>}]   // OR top-level avg
      }
    }

    Full schema reference:
    https://learn.microsoft.com/en-us/rest/api/application-insights/metrics/get#metricsresult

    Returns 0.0 for empty results or unexpected shapes.
    """
    try:
        body: dict[str, Any] = response.json()
    except Exception:
        return 0.0

    value_obj = body.get("value", {})
    if not isinstance(value_obj, dict):
        return 0.0

    # Segments array: [{"<metricId>": {"avg": ...}}]
    segments = value_obj.get("segments", [])
    if segments:
        segment = segments[0]
        if isinstance(segment, dict):
            # Each segment has one key = the metric ID with sub-dict {"avg": float, ...}
            for seg_val in segment.values():
                if isinstance(seg_val, dict):
                    avg = seg_val.get("avg")
                    if avg is not None:
                        try:
                            return float(avg)
                        except (TypeError, ValueError):
                            return 0.0

    # Top-level avg (single-segment shorthand)
    for v in value_obj.values():
        if isinstance(v, dict):
            avg = v.get("avg")
            if avg is not None:
                try:
                    return float(avg)
                except (TypeError, ValueError):
                    return 0.0

    return 0.0


# ---------------------------------------------------------------------------
# Internal exception hierarchy (not exposed to callers)
# ---------------------------------------------------------------------------


class _AzureMonitorError(Exception):
    """Base for internal errors — never escapes snapshot()."""


class _RetryableError(_AzureMonitorError):
    """Transient error — the request should be retried."""


class _FatalError(_AzureMonitorError):
    """Non-retryable error — abandon and return 0.0."""


class _MetricNotFoundError(_FatalError):
    """The requested metric ID does not exist for this Application Insights app."""
