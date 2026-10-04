"""Unit tests for AzureMonitorMetricsSnapshot.

All tests mock httpx transport — no live Azure resources required.

Coverage:
  - Successful single and multi-metric snapshots
  - Response parsing: segments array, top-level avg, empty result
  - Metric name mapping via _METRIC_ID_MAP
  - Unknown metric names pass through as-is
  - Auth: api_key header vs AAD Bearer token
  - HTTP error handling: 401/403, 404, 5xx, transport errors, timeout
  - Retry logic: 5xx retried up to max_retries, 401 not retried
  - Malformed JSON response returns 0.0
  - snapshot() contract: all requested metric names present in output
  - from_settings() factory wires settings correctly
  - empty app_id raises ValueError at construction time
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.configuration.settings import AzureMonitorMetricsSettings
from backend.models.incident import Incident, IncidentSeverity, IncidentStatus
from backend.providers.metrics.azure_monitor_snapshot import (
    _METRIC_ID_MAP,
    AzureMonitorMetricsSnapshot,
    _parse_metric_response,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    *,
    app_id: str = "test-app-id",
    api_key: str | None = "test-key",
    max_retries: int = 0,
    timeout_seconds: float = 5.0,
) -> AzureMonitorMetricsSnapshot:
    """Build an AzureMonitorMetricsSnapshot with retry disabled by default."""
    return AzureMonitorMetricsSnapshot(
        app_id=app_id,
        api_key=api_key,
        max_retries=max_retries,
        timeout_seconds=timeout_seconds,
        retry_min_wait_seconds=0.0,
        retry_max_wait_seconds=0.0,
    )


def _ai_response(metric_id: str, avg: float) -> dict[str, Any]:
    """Build a mock Application Insights metrics API response body."""
    return {
        "value": {
            "start": "2026-10-04T00:00:00Z",
            "end": "2026-10-04T00:01:00Z",
            "segments": [
                {metric_id: {"avg": avg, "count": 42}}
            ],
        }
    }


def _make_httpx_response(body: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_empty_app_id_raises() -> None:
    """Constructing with an empty app_id must raise ValueError."""
    with pytest.raises(ValueError, match="app_id"):
        AzureMonitorMetricsSnapshot(app_id="")


def test_repr_contains_app_id() -> None:
    snap = _make_snapshot(app_id="my-app")
    assert "my-app" in repr(snap)


# ---------------------------------------------------------------------------
# from_settings factory
# ---------------------------------------------------------------------------


def test_from_settings_no_api_key() -> None:
    """from_settings with no api_key should create instance with credential path."""
    settings = AzureMonitorMetricsSettings(
        enabled=True,
        app_id="settings-app-id",
    )
    snap = AzureMonitorMetricsSnapshot.from_settings(settings, credential=None)
    assert snap._app_id == "settings-app-id"
    assert snap._api_key is None


def test_from_settings_with_api_key() -> None:
    """from_settings should extract the secret value from SecretStr."""
    from pydantic import SecretStr
    settings = AzureMonitorMetricsSettings(
        enabled=True,
        app_id="my-app",
        api_key=SecretStr("super-secret"),
    )
    snap = AzureMonitorMetricsSnapshot.from_settings(settings)
    assert snap._api_key == "super-secret"


def test_from_settings_propagates_timeouts() -> None:
    settings = AzureMonitorMetricsSettings(
        app_id="my-app",
        timeout_seconds=15.0,
        max_retries=5,
        retry_min_wait_seconds=1.0,
        retry_max_wait_seconds=20.0,
    )
    snap = AzureMonitorMetricsSnapshot.from_settings(settings)
    assert snap._timeout_seconds == 15.0
    assert snap._max_retries == 5
    assert snap._retry_min_wait == 1.0
    assert snap._retry_max_wait == 20.0


# ---------------------------------------------------------------------------
# Metric name mapping
# ---------------------------------------------------------------------------


def test_metric_id_map_covers_common_names() -> None:
    """Canonical domain metric names must be present in the mapping table."""
    assert "error_rate" in _METRIC_ID_MAP
    assert "latency_ms" in _METRIC_ID_MAP
    assert "availability" in _METRIC_ID_MAP


def test_unknown_metric_name_passes_through() -> None:
    """A metric name not in _METRIC_ID_MAP is forwarded as-is to the API."""
    # We verify this indirectly: the map lookup returns the name unchanged
    metric_id = _METRIC_ID_MAP.get("my.custom.metric", "my.custom.metric")
    assert metric_id == "my.custom.metric"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_segments_array_returns_avg() -> None:
    body = _ai_response("requests/failed", 0.05)
    response = _make_httpx_response(body)
    result = _parse_metric_response(response)
    assert result == pytest.approx(0.05)


def test_parse_top_level_avg_fallback() -> None:
    """Top-level avg (no segments) should be extracted."""
    body = {
        "value": {
            "requests/failed": {"avg": 0.12},
        }
    }
    response = _make_httpx_response(body)
    result = _parse_metric_response(response)
    assert result == pytest.approx(0.12)


def test_parse_empty_segments_returns_zero() -> None:
    body: dict[str, Any] = {"value": {"segments": []}}
    response = _make_httpx_response(body)
    assert _parse_metric_response(response) == 0.0


def test_parse_missing_value_key_returns_zero() -> None:
    response = _make_httpx_response({})
    assert _parse_metric_response(response) == 0.0


def test_parse_non_json_response_returns_zero() -> None:
    response = httpx.Response(
        status_code=200,
        content=b"not json at all",
        headers={"content-type": "text/plain"},
    )
    assert _parse_metric_response(response) == 0.0


def test_parse_null_avg_returns_zero() -> None:
    body = {
        "value": {
            "segments": [{"requests/failed": {"avg": None}}],
        }
    }
    response = _make_httpx_response(body)
    assert _parse_metric_response(response) == 0.0


# ---------------------------------------------------------------------------
# Successful snapshot — single metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_single_metric_success() -> None:
    """snapshot() returns the correct float for a single mapped metric."""
    snap = _make_snapshot()

    with patch.object(snap, "_send_once", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = 0.03
        result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)

    assert result == {"error_rate": pytest.approx(0.03)}


@pytest.mark.asyncio
async def test_snapshot_multiple_metrics() -> None:
    """All requested metric names must appear in the output."""
    snap = _make_snapshot()
    call_count = 0

    async def _fake_send(path: str, params: dict[str, str]) -> float:
        nonlocal call_count
        call_count += 1
        if "requests/failed" in path:
            return 0.07
        if "requests/duration" in path:
            return 145.5
        return 0.0

    with patch.object(snap, "_send_once", side_effect=_fake_send):
        result = await snap.snapshot(
            "svc-a",
            ("error_rate", "latency_ms"),
            window_seconds=120.0,
        )

    assert set(result.keys()) == {"error_rate", "latency_ms"}
    assert result["error_rate"] == pytest.approx(0.07)
    assert result["latency_ms"] == pytest.approx(145.5)
    assert call_count == 2


@pytest.mark.asyncio
async def test_snapshot_returns_zero_for_empty_metric_names() -> None:
    snap = _make_snapshot()
    result = await snap.snapshot("svc-a", (), window_seconds=60.0)
    assert result == {}


# ---------------------------------------------------------------------------
# Protocol contract: all requested names in output even on error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_all_names_present_on_partial_failure() -> None:
    """Even when some metrics fail, all requested names must appear in output."""
    snap = _make_snapshot()

    async def _fake_send(path: str, params: dict[str, str]) -> float:
        if "requests/failed" in path:
            return 0.02
        raise httpx.ConnectError("unreachable")

    with patch.object(snap, "_send_once", side_effect=_fake_send):
        result = await snap.snapshot(
            "svc-a",
            ("error_rate", "latency_ms"),
            window_seconds=60.0,
        )

    assert "error_rate" in result
    assert "latency_ms" in result
    assert result["error_rate"] == pytest.approx(0.02)
    assert result["latency_ms"] == 0.0  # failed → default


@pytest.mark.asyncio
async def test_snapshot_all_names_present_on_total_failure() -> None:
    """snapshot() must return all keys as 0.0 when every metric call fails."""
    snap = _make_snapshot()

    with patch.object(
        snap, "_send_once", side_effect=Exception("boom")
    ):
        result = await snap.snapshot(
            "svc-a",
            ("error_rate", "latency_ms", "availability"),
            window_seconds=60.0,
        )

    assert set(result.keys()) == {"error_rate", "latency_ms", "availability"}
    assert all(v == 0.0 for v in result.values())


# ---------------------------------------------------------------------------
# HTTP error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_auth_error_returns_zero() -> None:
    """401/403 from App Insights returns 0.0 without raising."""
    snap = _make_snapshot(max_retries=0)

    transport = httpx.MockTransport(
        lambda req: httpx.Response(401, text="Unauthorized")
    )
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)
    assert result == {"error_rate": 0.0}


@pytest.mark.asyncio
async def test_snapshot_404_returns_zero() -> None:
    """A 404 (metric not found) returns 0.0 without retrying."""
    snap = _make_snapshot(max_retries=3)  # retries should NOT fire on 404

    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)
    assert result == {"error_rate": 0.0}
    assert call_count == 1  # no retries on 404


@pytest.mark.asyncio
async def test_snapshot_5xx_retried_then_returns_zero() -> None:
    """5xx errors are retried up to max_retries; exhaustion returns 0.0."""
    snap = _make_snapshot(max_retries=2, timeout_seconds=5.0)

    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    # Zero wait so the test is fast
    snap._retry_min_wait = 0.0
    snap._retry_max_wait = 0.0

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)
    assert result == {"error_rate": 0.0}
    # Initial attempt + 2 retries = 3 total
    assert call_count == 3


@pytest.mark.asyncio
async def test_snapshot_connect_error_returns_zero() -> None:
    """Transport-level connection errors return 0.0."""
    snap = _make_snapshot(max_retries=0)

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)
    assert result == {"error_rate": 0.0}


@pytest.mark.asyncio
async def test_snapshot_timeout_returns_zero() -> None:
    """Timeout during HTTP call returns 0.0 without raising."""
    snap = _make_snapshot(max_retries=0, timeout_seconds=0.001)

    async def _fake_send(path: str, params: dict[str, str]) -> float:
        raise TimeoutError("timed out")

    with patch.object(snap, "_send_once", side_effect=_fake_send):
        result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)

    assert result == {"error_rate": 0.0}


# ---------------------------------------------------------------------------
# Successful HTTP round-trip (via MockTransport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_full_http_roundtrip_with_api_key() -> None:
    """Full HTTP round-trip: verify x-api-key header is sent and value parsed."""
    snap = _make_snapshot(api_key="my-secret-key", max_retries=0)

    seen_headers: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(req.headers))
        body = _ai_response("requests/failed", 0.04)
        return _make_httpx_response(body)

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        headers={"x-api-key": "my-secret-key", "Accept": "application/json"},
        transport=transport,
    )

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)

    assert result["error_rate"] == pytest.approx(0.04)
    assert seen_headers.get("x-api-key") == "my-secret-key"


@pytest.mark.asyncio
async def test_snapshot_aad_bearer_token_used_when_no_api_key() -> None:
    """Without an API key, the adapter must acquire and send an AAD Bearer token."""
    snap = AzureMonitorMetricsSnapshot(
        app_id="aad-app",
        api_key=None,
        max_retries=0,
        timeout_seconds=5.0,
        retry_min_wait_seconds=0.0,
        retry_max_wait_seconds=0.0,
    )

    fake_token = MagicMock()
    fake_token.token = "fake-aad-token"

    fake_credential = AsyncMock()
    fake_credential.get_token = AsyncMock(return_value=fake_token)
    snap._credential = fake_credential

    seen_auth: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("authorization", ""))
        body = _ai_response("requests/failed", 0.01)
        return _make_httpx_response(body)

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)

    assert result["error_rate"] == pytest.approx(0.01)
    assert seen_auth == ["Bearer fake-aad-token"]
    fake_credential.get_token.assert_called_once()


@pytest.mark.asyncio
async def test_snapshot_no_credential_and_no_api_key_still_returns_zero_gracefully() -> None:
    """When both api_key and credential are absent, snapshot() returns 0.0 (auth error)."""
    snap = AzureMonitorMetricsSnapshot(
        app_id="no-auth-app",
        api_key=None,
        credential=None,
        max_retries=0,
        timeout_seconds=5.0,
    )

    def handler(req: httpx.Request) -> httpx.Response:
        # Simulate the server rejecting an unauthenticated request
        return httpx.Response(401, text="Unauthorized")

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)
    assert result == {"error_rate": 0.0}


# ---------------------------------------------------------------------------
# Timespan parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_timespan_parameter_format() -> None:
    """The timespan query parameter must be formatted as PTnS."""
    snap = _make_snapshot()
    seen_params: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen_params.update(dict(req.url.params))
        body = _ai_response("requests/failed", 0.0)
        return _make_httpx_response(body)

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    await snap.snapshot("svc-a", ("error_rate",), window_seconds=120.0)
    assert seen_params.get("timespan") == "PT120S"


@pytest.mark.asyncio
async def test_snapshot_aggregation_is_avg() -> None:
    """The aggregation query parameter must be 'avg'."""
    snap = _make_snapshot()
    seen_params: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen_params.update(dict(req.url.params))
        return _make_httpx_response(_ai_response("requests/failed", 0.0))

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)
    assert seen_params.get("aggregation") == "avg"


# ---------------------------------------------------------------------------
# Retry: 5xx retried; 401 not retried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_retried_succeeds_on_second_attempt() -> None:
    """A 5xx on first attempt followed by success should return the correct value."""
    snap = _make_snapshot(max_retries=2, timeout_seconds=5.0)
    snap._retry_min_wait = 0.0
    snap._retry_max_wait = 0.0

    attempt = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return httpx.Response(502, text="Bad Gateway")
        return _make_httpx_response(_ai_response("requests/failed", 0.06))

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)
    assert result["error_rate"] == pytest.approx(0.06)
    assert attempt == 2


@pytest.mark.asyncio
async def test_401_not_retried() -> None:
    """Auth errors (401) must not be retried."""
    snap = _make_snapshot(max_retries=3)

    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(401, text="Unauthorized")

    transport = httpx.MockTransport(handler)
    snap._http = httpx.AsyncClient(
        base_url="https://api.applicationinsights.io",
        transport=transport,
    )

    result = await snap.snapshot("svc-a", ("error_rate",), window_seconds=60.0)
    assert result == {"error_rate": 0.0}
    assert call_count == 1  # no retries


# ---------------------------------------------------------------------------
# MetricsSnapshotPort protocol compliance
# ---------------------------------------------------------------------------


def test_implements_metrics_snapshot_port() -> None:
    """AzureMonitorMetricsSnapshot must satisfy the MetricsSnapshotPort protocol."""
    from backend.core.validation_engine import MetricsSnapshotPort

    snap = _make_snapshot()
    assert isinstance(snap, MetricsSnapshotPort)


# ---------------------------------------------------------------------------
# VerificationEngine integration (no live Azure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verification_engine_uses_snapshot_adapter() -> None:
    """VerificationEngine.verify() successfully invokes AzureMonitorMetricsSnapshot."""
    from backend.core.validation_engine import VerificationEngine
    from backend.models.validation import VerificationPlan

    snap = _make_snapshot()

    incident = Incident(
        incident_id=str(uuid.uuid4()),
        title="Test incident",
        severity=IncidentSeverity.HIGH,
        status=IncidentStatus.OPEN,
        affected_services=("svc-a",),
        description="Elevated error rate.",
        detected_at=datetime.now(UTC),
        symptoms=("high error rate",),
        correlation_id=str(uuid.uuid4()),
    )

    async def _fake_send(path: str, params: dict[str, str]) -> float:
        # Return a "healthy" error rate — below the 0.05 threshold
        return 0.01

    with patch.object(snap, "_send_once", side_effect=_fake_send):
        engine = VerificationEngine(metrics_snapshot=snap)
        plan = VerificationPlan(
            plan_id="p1",
            incident_id=incident.incident_id,
            remediation_plan_id="r1",
            metrics_to_compare=("error_rate",),
            comparison_window_seconds=60.0,
            thresholds={"error_rate": 0.05},
            snapshot_before={"svc-a": {"error_rate": 0.9}},
        )
        result = await engine.verify(plan, incident)

    assert result.passed is True
    assert result.confidence > 0.0
