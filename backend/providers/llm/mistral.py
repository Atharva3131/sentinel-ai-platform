"""Mistral LLM adapter.

Thin wrapper around ``OpenAICompatibleLLMAdapter`` pre-configured for
the Mistral API endpoint and default model.

Mistral endpoint:  https://api.mistral.ai/v1
Primary model:     mistral-large-latest

Authentication:    Authorization: Bearer <SENTINEL_LLM__PRIMARY__API_KEY>

Mistral's API is OpenAI-compatible for chat completions, tool calls, and
JSON mode.  No Mistral SDK is imported.
"""

from __future__ import annotations

from backend.configuration.settings import LLMProviderSettings
from backend.providers.llm.openai_compat import OpenAICompatibleLLMAdapter

_MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
_MISTRAL_DEFAULT_MODEL = "mistral-large-latest"


def MistralLLMAdapter(settings: LLMProviderSettings) -> OpenAICompatibleLLMAdapter:
    """Factory: return an OpenAICompatibleLLMAdapter configured for Mistral.

    Args:
        settings: LLMProviderSettings with api_key and optional model/timeout overrides.

    Returns:
        A fully configured adapter that satisfies the LLMProvider protocol.
    """
    return OpenAICompatibleLLMAdapter(
        provider_name="mistral",
        base_url=settings.base_url or _MISTRAL_BASE_URL,
        api_key=settings.api_key.get_secret_value() if settings.api_key else "",
        default_model=settings.model or _MISTRAL_DEFAULT_MODEL,
        timeout_seconds=settings.timeout_seconds,
        max_retries=settings.max_retries,
        retry_min_wait=settings.retry_min_wait_seconds,
        retry_max_wait=settings.retry_max_wait_seconds,
        temperature=settings.temperature,
        max_output_tokens=settings.max_output_tokens,
    )
