"""Focused tests for the read-only dashboard refresh path."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend import dashboard
from backend.configuration import AppSettings


def test_dashboard_initial_snapshot_is_demo_data_without_network_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("initial dashboard render must not call the network")

    monkeypatch.setattr(dashboard, "_refresh_dashboard", fail_if_called)

    built = dashboard.build_dashboard(AppSettings())
    config = built.get_config_file()
    component_values = [
        component.get("props", {}).get("value")
        for component in config["components"]
    ]

    assert any("DEMO DATA" in str(value) for value in component_values)
    assert any("Database connection pool exhausted" in str(value) for value in component_values)
    assert any("842" in str(value) and "98" in str(value) for value in component_values)
    assert not any(
        any(target[1] == "load" for target in dependency.get("targets", []))
        for dependency in config.get("dependencies", [])
    )


@pytest.mark.asyncio
async def test_refresh_preserves_all_read_only_payloads_and_configured_order_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_get_json(_client: Any, url: str) -> tuple[dict[str, Any], None]:
        calls.append(url)
        if url.endswith("/health") and "order-api" not in url:
            return {"status": "healthy"}, None
        if url.endswith("/incidents?limit=50"):
            return {"items": [{"title": "Pool exhausted"}]}, None
        if url.endswith("/health"):
            return {"status": "degraded"}, None
        return {"pool": {"free": 2}}, None

    monkeypatch.setattr(dashboard, "_get_json", fake_get_json)

    result = await dashboard._refresh_dashboard(
        "http://sentinel-api",
        "http://order-api-from-settings",
    )

    assert len(calls) == 4
    assert "http://order-api-from-settings/health" in calls
    assert "http://order-api-from-settings/metrics/raw" in calls
    assert "Pool exhausted" in result[1][0]
    assert '"free": 2' in result[4]
    assert "HEALTHY" in result[0]
    assert "DEGRADED" in result[0]


@pytest.mark.asyncio
async def test_refresh_surfaces_incident_upstream_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_json(_client: Any, url: str) -> tuple[dict[str, Any] | None, str | None]:
        if url.endswith("/incidents?limit=50"):
            return None, "ReadTimeout"
        return {"status": "healthy"}, None

    monkeypatch.setattr(dashboard, "_get_json", fake_get_json)

    result = await dashboard._refresh_dashboard("http://sentinel-api", "")

    assert result[1] == []
    assert "Sentinel incidents" in result[0]
    assert "UNAVAILABLE" in result[0]
    assert "ReadTimeout" in result[0]
    assert "could not be loaded" in result[2]


@pytest.mark.asyncio
async def test_refresh_returns_visible_timeout_instead_of_waiting_indefinitely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_get_json(_client: Any, _url: str) -> tuple[None, None]:
        await asyncio.sleep(1)
        return None, None

    monkeypatch.setattr(dashboard, "_get_json", slow_get_json)
    monkeypatch.setattr(dashboard, "_REFRESH_TIMEOUT_SECONDS", 0.01)

    result = await dashboard._refresh_dashboard("http://sentinel-api", "http://order-api")

    assert "timed out after 0.01 seconds" in result[0]
    assert "UNAVAILABLE" in result[0]
    assert "timed out" in result[2]
    assert "timed out" in result[4]
