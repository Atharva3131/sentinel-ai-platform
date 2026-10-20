"""Read-only Gradio dashboard for the Sentinel API."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import gradio as gr
import httpx

from backend.configuration import AppSettings

_REQUEST_TIMEOUT_SECONDS = 5.0
_REFRESH_TIMEOUT_SECONDS = 10.0

_DEMO_HEALTH = """## DEMO DATA — initial dashboard snapshot

These values are simulated for demonstration and are not live telemetry.

- **Sentinel API:** `HEALTHY` — API responding normally
- **Order API:** `HEALTHY` — read-only demo endpoint available
- **Active incident:** `HIGH` — database connection pool exhausted

Metrics below are simulated: error rate **2.4%**, p95 latency **842 ms**, and
connection pool usage **98% (49/50 connections)**.
"""

_DEMO_INCIDENTS = [
    [
        "2026-10-20 09:42 UTC",
        "HIGH",
        "ACTIVE",
        "Database connection pool exhausted",
        "Sentinel API, Order API, PostgreSQL",
        "demo-inc-2026-1020-001",
    ]
]

_DEMO_RCA = """### DEMO DATA — probable root cause

The Order API experienced a burst of slow database queries, causing requests to
hold connections longer than normal. The application pool reached **49/50
connections**, producing connection acquisition timeouts and elevated request
failures. This is simulated incident data for the demo, not a live investigation.
"""

_DEMO_PLAN = """### DEMO DATA — remediation plan

1. Reduce query latency and terminate stale transactions.
2. Temporarily increase the pool ceiling after confirming database capacity.
3. Add connection-acquisition timeout and pool saturation alerts.
4. Verify error rate and p95 latency return to baseline before closing the incident.

This plan is illustrative only; the dashboard does not execute remediation.
"""

_DEMO_METRICS = json.dumps(
    {
        "data_label": "DEMO DATA",
        "simulated": True,
        "error_rate_percent": 2.4,
        "p95_latency_ms": 842,
        "connection_pool": {"used": 49, "capacity": 50, "usage_percent": 98},
    },
    indent=2,
)


def _initial_dashboard_values() -> tuple[str, list[list[str]], str, str, str]:
    """Return the synchronous demo snapshot used for the first page render."""
    return _DEMO_HEALTH, [row[:] for row in _DEMO_INCIDENTS], _DEMO_RCA, _DEMO_PLAN, _DEMO_METRICS


def _url(value: str, path: str) -> str:
    return f"{value.rstrip('/')}/{path.lstrip('/')}"


def _pretty(value: Any) -> str:
    return json.dumps(_redact(value), indent=2, sort_keys=True, default=str)


_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "connection_string",
    "client_secret",
    "password",
    "secret",
    "token",
)


def _redact(value: Any) -> Any:
    """Remove common credential-shaped fields before rendering browser output."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if any(part in str(key).lower() for part in _SECRET_KEY_PARTS)
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _incident_table(items: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [
            str(item.get("detected_at", "")),
            str(item.get("severity", "")).upper(),
            str(item.get("status", "")).upper(),
            item.get("title", ""),
            ", ".join(item.get("affected_services") or []),
            item.get("incident_id", ""),
        ]
        for item in items
    ]


def _incident_detail(
    items: list[dict[str, Any]],
    error: str | None = None,
) -> tuple[str, str]:
    if error:
        unavailable = f"### UNAVAILABLE\n\nSentinel incident data could not be loaded: {error}"
        return unavailable, unavailable
    if not items:
        return (
            "### DEMO/MOCK DATA\n\nNo incidents were returned by the Sentinel API.",
            "### DEMO/MOCK DATA\n\nNo incident-specific remediation plan is available.",
        )

    incident = items[0]
    metadata = incident.get("metadata") or {}
    rca = metadata.get("rca") or metadata.get("root_cause_analysis")
    plan = metadata.get("remediation_plan") or metadata.get("plan")

    if rca:
        rca_text = "### RCA (Sentinel incident data)\n\n```json\n" + _pretty(rca) + "\n```"
    else:
        rca_text = (
            "### DEMO/MOCK DATA\n\n"
            "The current incident read API does not expose the investigation RCA.\n\n"
            f"Latest incident: **{incident.get('title', 'Unknown')}**\n\n"
            "Observed symptoms: "
            + "; ".join(incident.get("symptoms") or ["No symptoms recorded."])
        )

    if plan:
        plan_text = (
            "### Remediation plan (Sentinel incident data)\n\n```json\n"
            + _pretty(plan)
            + "\n```"
        )
    else:
        plan_text = (
            "### DEMO/MOCK DATA\n\n"
            "No persisted remediation plan is available from the current read API.\n\n"
            "This dashboard is read-only; it will not execute, approve, deploy, "
            "or trigger remediation."
        )
    return rca_text, plan_text


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
        return (payload if isinstance(payload, dict) else {"value": payload}), None
    except Exception as exc:  # Dashboard failures must not take down the API.
        # Do not render exception details: some HTTP client errors can include a URL.
        return None, type(exc).__name__


async def _refresh_dashboard(
    sentinel_base_url: str,
    order_api_url: str,
) -> tuple[str, list[list[str]], str, str, str]:
    """Fetch dashboard data with bounded, read-only upstream calls."""
    timeout = httpx.Timeout(_REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        requests = [
            _get_json(client, _url(sentinel_base_url, "/health")),
            _get_json(client, _url(sentinel_base_url, "/api/v1/incidents?limit=50")),
        ]
        if order_api_url:
            requests.extend(
                [
                    _get_json(client, _url(order_api_url, "/health")),
                    _get_json(client, _url(order_api_url, "/metrics/raw")),
                ]
            )

        try:
            results = await asyncio.wait_for(
                asyncio.gather(*requests),
                timeout=_REFRESH_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            timeout_error = (
                f"refresh timed out after {_REFRESH_TIMEOUT_SECONDS:g} seconds"
            )
            results = [(None, timeout_error) for _ in requests]

    health, health_error = results[0]
    incidents_payload, incidents_error = results[1]
    if order_api_url:
        order_health, order_health_error = results[2]
        order_metrics, metrics_error = results[3]
        order_errors = [
            f"health: {order_health_error}" if order_health_error else None,
            f"metrics: {metrics_error}" if metrics_error else None,
        ]
        order_error = "; ".join(error for error in order_errors if error) or None
    else:
        order_health = None
        order_metrics = None
        order_error = "SENTINEL_DEMO1__BASE_URL is not configured"

    incidents: list[dict[str, Any]] = []
    if incidents_error is None:
        candidate_incidents = (incidents_payload or {}).get("items", [])
        if isinstance(candidate_incidents, list):
            incidents = candidate_incidents
        else:
            incidents_error = "invalid incidents payload"

    sentinel_status = str((health or {}).get("status", "UNAVAILABLE")).upper()
    order_status = str((order_health or {}).get("status", "UNAVAILABLE")).upper()
    health_lines = [
        "## Current service health (live read-only probes)",
        f"- **Sentinel API:** `{sentinel_status}`"
        + (f" — {health_error}" if health_error else ""),
        f"- **Order API:** `{order_status}`"
        + (f" — {order_error}" if order_error else ""),
        "\nHistorical incident statuses below are persisted outcomes and do not "
        "represent current health.",
    ]
    if incidents_error:
        health_lines.insert(
            2,
            f"- **Sentinel incidents:** `UNAVAILABLE` — {incidents_error}",
        )

    metrics_text = _pretty(order_metrics) if order_metrics else (
        "Unavailable. " + (order_error or "No metrics payload returned.")
    )
    rca_text, plan_text = _incident_detail(incidents, incidents_error)
    return (
        "\n".join(health_lines),
        _incident_table(incidents),
        rca_text,
        plan_text,
        metrics_text,
    )


def build_dashboard(settings: AppSettings) -> gr.Blocks:
    """Build the read-only dashboard using server-side API calls."""
    sentinel_base_url = os.getenv("SENTINEL_DASHBOARD__BASE_URL", "http://127.0.0.1:8000")
    order_api_url = settings.demo1.base_url

    async def refresh() -> tuple[str, list[list[str]], str, str, str]:
        return await _refresh_dashboard(sentinel_base_url, order_api_url)

    dashboard: gr.Blocks = gr.Blocks(title="Sentinel AI Dashboard")
    initial_health, initial_incidents, initial_rca, initial_plan, initial_metrics = (
        _initial_dashboard_values()
    )
    with dashboard:
        gr.Markdown(
            "# Sentinel AI Dashboard\n"
            "Read-only operational view. The initial snapshot is **DEMO DATA**; "
            "refreshes call existing Sentinel and Order API read endpoints."
        )
        refresh_button = gr.Button("Refresh dashboard", variant="primary")
        health_output = gr.Markdown(value=initial_health)
        incidents_output = gr.Dataframe(
            value=initial_incidents,
            headers=[
                "Detected",
                "Severity",
                "Historical status",
                "Title",
                "Services",
                "Incident ID",
            ],
            datatype=["str"] * 6,
            label="Sentinel incidents and historical outcomes",
            interactive=False,
        )
        with gr.Row():
            with gr.Column():
                gr.Markdown("### RCA")
                rca_output = gr.Markdown(value=initial_rca)
            with gr.Column():
                gr.Markdown("### Remediation plan")
                plan_output = gr.Markdown(value=initial_plan)
        metrics_output = gr.Code(
            value=initial_metrics,
            label="Order API metrics (raw read-only response)",
            language="json",
        )
        outputs = [health_output, incidents_output, rca_output, plan_output, metrics_output]
        refresh_button.click(refresh, outputs=outputs)
    return dashboard


def mount_dashboard(app: Any, settings: AppSettings) -> Any:
    """Mount the dashboard without changing existing API routes."""
    return gr.mount_gradio_app(app, build_dashboard(settings), path="/dashboard")
