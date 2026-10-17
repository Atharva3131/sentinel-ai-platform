"""Tests for the no-side-effect LLM remediation probe."""

from __future__ import annotations

import pytest

from backend.configuration.settings import (
    GitHubSettings,
    LLMProviderName,
    LLMProviderSettings,
    LLMSettings,
    TestingSettings,
)
from backend.diagnostics.llm_remediation_probe import _run_probe
from backend.interfaces.fake_llm import StructuredFakeLLM


def _settings() -> TestingSettings:
    return TestingSettings(
        _env_file=None,
        github=GitHubSettings(
            default_owner="test-owner",
            default_repository="test-repository",
        ),
        llm=LLMSettings(
            primary=LLMProviderSettings(
                name=LLMProviderName.FAKE,
                model="test-model",
            )
        ),
    )


def _llm() -> StructuredFakeLLM:
    return StructuredFakeLLM(
        {
            "root_cause_title": "Synthetic deployment regression",
            "root_cause_description": "The synthetic release increased errors.",
            "confidence": 0.95,
            "supporting_evidence_ids": [],
            "contradicting_evidence_ids": [],
            "reasoning_summary": "The deployment preceded the error increase.",
            "missing_evidence": [],
        }
    )


@pytest.mark.asyncio
async def test_dry_run_evaluates_policy_without_external_calls() -> None:
    result = await _run_probe(_settings(), _llm(), dry_run=True)

    assert result["dry_run"] is True
    assert result["deployment_executed"] is False
    assert result["deployment_provider_calls"] == 0
    assert result["azure_restart_attempted"] is False
    assert result["final_status"] == "failed"
    assert result["incident_status"] == "escalated"

    decisions = {decision["operation"]: decision for decision in result["policy_decisions"]}
    assert decisions["repository_read"]["allowed"] is True
    assert decisions["branch_create"]["allowed"] is True
    assert decisions["pull_request_create"]["allowed"] is False

    remediation = result["remediation_result"]
    assert remediation["actions_succeeded"] == 2
    assert len(remediation["policy_rejected_action_ids"]) == 1


@pytest.mark.asyncio
async def test_dry_run_requires_github_defaults_before_llm_execution() -> None:
    settings = _settings()
    settings.github.default_owner = ""

    with pytest.raises(RuntimeError, match="default owner/repository"):
        await _run_probe(settings, _llm(), dry_run=True)
