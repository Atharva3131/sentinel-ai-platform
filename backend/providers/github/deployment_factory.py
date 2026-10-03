"""Deployment provider factory — config-driven selection."""

from __future__ import annotations

from backend.configuration.settings import (
    AppSettings,
    DeploymentProviderName,
)
from backend.interfaces.deployment import DeploymentProvider
from backend.providers.github.deployment import GitHubActionsDeploymentProvider
from backend.providers.github.fake_deployment import FakeDeploymentProvider


def build_deployment_provider(settings: AppSettings) -> DeploymentProvider:
    """Return the configured deployment provider.

    Returns ``FakeDeploymentProvider`` (zero network calls) when:
    - ``deployment.enabled`` is False, or
    - ``deployment.provider`` is ``fake``.
    """
    d = settings.deployment
    if not d.enabled or d.provider == DeploymentProviderName.FAKE:
        return FakeDeploymentProvider()

    if d.provider == DeploymentProviderName.GITHUB_ACTIONS:
        return GitHubActionsDeploymentProvider.from_settings(
            settings.github, d
        )

    raise ValueError(f"Unknown deployment provider: {d.provider!r}")
