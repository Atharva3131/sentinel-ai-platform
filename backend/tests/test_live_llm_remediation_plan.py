"""Opt-in validation of the configured LLM RCA and remediation planner."""

from __future__ import annotations

import os

import pytest

from backend.diagnostics.llm_remediation_probe import run_probe


@pytest.mark.live_llm
@pytest.mark.asyncio
async def test_configured_llm_produces_structured_rca_and_plan() -> None:
    """Verify real LLM output can be consumed by the planner without side effects."""
    if os.getenv("SENTINEL_RUN_LIVE_LLM_TESTS") != "1":
        pytest.skip("Set SENTINEL_RUN_LIVE_LLM_TESTS=1 to call the configured LLM")

    result = await run_probe()

    assert result["rca"]["rca_id"]
    assert result["rca"]["root_cause_title"]
    assert result["rca"]["root_cause_description"]
    assert 0.0 <= result["rca"]["confidence"] <= 1.0
    assert result["remediation_plan"]["plan_id"]
    assert result["remediation_plan"]["rca_id"] == result["rca"]["rca_id"]
    assert result["remediation_plan"]["action_count"] > 0
    assert result["deployment_executed"] is False
