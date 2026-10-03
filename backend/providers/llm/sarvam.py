"""Sarvam LLM adapter.

Thin wrapper around ``OpenAICompatibleLLMAdapter`` pre-configured for
the Sarvam API endpoint and default model.

Sarvam endpoint:  https://api.sarvam.ai/v1
Primary model:    sarvam-105b  (128K context, best for reasoning/agentic tasks)
Fallback model:   sarvam-30b

Authentication:   Authorization: Bearer <SENTINEL_LLM__PRIMARY__API_KEY>

No Sarvam SDK is imported — the adapter speaks the OpenAI wire format
directly over httpx.
"""

from __future__ import annotations

from backend.configuration.settings import LLMProviderSettings
from backend.providers.llm.openai_compat import OpenAICompatibleLLMAdapter

_SARVAM_BASE_URL = "https://api.sarvam.ai/v1"
_SARVAM_DEFAULT_MODEL = "sarvam-105b"


def SarvamLLMAdapter(settings: LLMProviderSettings) -> OpenAICompatibleLLMAdapter:
    """Factory: return an OpenAICompatibleLLMAdapter configured for Sarvam.

    Args:
        settings: LLMProviderSettings with api_key and optional model/timeout overrides.

    Returns:
        A fully configured adapter that satisfies the LLMProvider protocol.
    """
    return OpenAICompatibleLLMAdapter(
        provider_name="sarvam",
        base_url=settings.base_url or _SARVAM_BASE_URL,
        api_key=settings.api_key.get_secret_value() if settings.api_key else "",
        default_model=settings.model or _SARVAM_DEFAULT_MODEL,
        timeout_seconds=settings.timeout_seconds,
        max_retries=settings.max_retries,
        retry_min_wait=settings.retry_min_wait_seconds,
        retry_max_wait=settings.retry_max_wait_seconds,
        temperature=settings.temperature,
        max_output_tokens=settings.max_output_tokens,
    )
