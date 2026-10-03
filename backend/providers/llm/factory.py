"""LLM provider factory — builds the right provider from configuration.

This is the only place that reads ``LLMSettings`` and decides which
concrete adapter to construct.  The domain layer never calls this directly.

Provider selection (``settings.llm.primary.name``):
  "sarvam"  → SarvamLLMAdapter (primary) + optional MistralLLMAdapter (fallback)
  "mistral" → MistralLLMAdapter (primary) + optional SarvamLLMAdapter (fallback)
  "fake"    → FakeLLMProvider (testing; no network I/O)

Fallback is wired when:
  1. ``settings.llm.enable_fallback`` is True, AND
  2. ``settings.llm.fallback`` is configured, AND
  3. ``settings.llm.fallback.name`` differs from the primary.
"""

from __future__ import annotations

from backend.configuration.settings import LLMProviderName, LLMSettings
from backend.interfaces.fake_llm import FakeLLMProvider
from backend.interfaces.llm import LLMProvider
from backend.providers.llm.fallback import FallbackLLMProvider
from backend.providers.llm.mistral import MistralLLMAdapter
from backend.providers.llm.sarvam import SarvamLLMAdapter


def build_llm_provider(settings: LLMSettings) -> LLMProvider:
    """Construct and return the configured LLM provider.

    Returns a ``FallbackLLMProvider`` when both primary and fallback are
    configured and fallback is enabled; otherwise returns the primary
    provider directly.
    """
    primary = _build_single(settings.primary.name, settings)

    fallback: LLMProvider | None = None
    if (
        settings.enable_fallback
        and settings.fallback is not None
        and settings.fallback.name != settings.primary.name
    ):
        fallback = _build_single(settings.fallback.name, settings, is_fallback=True)

    if fallback is not None:
        return FallbackLLMProvider(primary=primary, fallback=fallback)
    return primary


def _build_single(
    name: LLMProviderName,
    settings: LLMSettings,
    *,
    is_fallback: bool = False,
) -> LLMProvider:
    from backend.configuration.settings import LLMProviderSettings
    provider_settings: LLMProviderSettings = (
        settings.fallback if (is_fallback and settings.fallback is not None) else settings.primary
    )

    if name == LLMProviderName.SARVAM:
        return SarvamLLMAdapter(provider_settings)
    if name == LLMProviderName.MISTRAL:
        return MistralLLMAdapter(provider_settings)
    if name == LLMProviderName.FAKE:
        return FakeLLMProvider()
    raise ValueError(f"Unknown LLM provider: {name!r}")
