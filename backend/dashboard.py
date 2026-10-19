"""Read-only Gradio dashboard for the Sentinel API."""

from __future__ import annotations

import json
import os
from typing import Any

import gradio as gr
import httpx

from backend.configuration import AppSettings


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


def _incident_detail(items: list[dict[str, Any]]) -> tuple[str, str]:
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
        return None, f"{type(exc).__name__}: {exc}"


def build_dashboard(settings: AppSettings) -> gr.Blocks:
    """Build the read-only dashboard using server-side API calls."""
    sentinel_base_url = os.getenv("SENTINEL_DASHBOARD__BASE_URL", "http://127.0.0.1:8000")
    order_api_url = settings.demo1.base_url

    async def refresh() -> tuple[str, list[list[str]], str, str, str]:
        timeout = httpx.Timeout(5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            health, health_error = await _get_json(client, _url(sentinel_base_url, "/health"))
            incidents_payload, _incidents_error = await _get_json(
                client, _url(sentinel_base_url, "/api/v1/incidents?limit=50")
            )

            order_health: dict[str, Any] | None = None
            order_metrics: dict[str, Any] | None = None
            order_error: str | None = None
            if order_api_url:
                order_health, order_error = await _get_json(client, _url(order_api_url, "/health"))
                order_metrics, metrics_error = await _get_json(
                    client, _url(order_api_url, "/metrics/raw")
                )
                if metrics_error and not order_error:
                    order_error = f"metrics: {metrics_error}"
            else:
                order_error = "SENTINEL_DEMO1__BASE_URL is not configured"

        incidents = (incidents_payload or {}).get("items", [])
        if not isinstance(incidents, list):
            incidents = []
        sentinel_status = (health or {}).get("status", "UNAVAILABLE").upper()
        order_status = (order_health or {}).get("status", "UNAVAILABLE").upper()
        health_lines = [
            "## Current service health (live read-only probes)",
            f"- **Sentinel API:** `{sentinel_status}`"
            + (f" — {health_error}" if health_error else ""),
            f"- **Order API:** `{order_status}`"
            + (f" — {order_error}" if order_error else ""),
            "\nHistorical incident statuses below are persisted outcomes and do not "
            "represent current health.",
        ]
        metrics_text = _pretty(order_metrics) if order_metrics else (
            "Unavailable. " + (order_error or "No metrics payload returned.")
        )
        rca_text, plan_text = _incident_detail(incidents)
        return (
            "\n".join(health_lines),
            _incident_table(incidents),
            rca_text,
            plan_text,
            metrics_text,
        )

    dashboard: gr.Blocks = gr.Blocks(title="Sentinel AI Dashboard")
    with dashboard:
        gr.Markdown(
            "# Sentinel AI Dashboard\n"
            "Read-only operational view. Refreshes call existing Sentinel and Order "
            "API read endpoints."
        )
        refresh_button = gr.Button("Refresh dashboard", variant="primary")
        health_output = gr.Markdown()
        incidents_output = gr.Dataframe(
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
                rca_output = gr.Markdown()
            with gr.Column():
                gr.Markdown("### Remediation plan")
                plan_output = gr.Markdown()
        metrics_output = gr.Code(
            label="Order API metrics (raw read-only response)",
            language="json",
        )
        outputs = [health_output, incidents_output, rca_output, plan_output, metrics_output]
        refresh_button.click(refresh, outputs=outputs)
        dashboard.load(refresh, outputs=outputs)
    return dashboard


def mount_dashboard(app: Any, settings: AppSettings) -> Any:
    """Mount the dashboard without changing existing API routes."""
    return gr.mount_gradio_app(app, build_dashboard(settings), path="/dashboard")
