"""FallbackLLMProvider — tries primary, falls back to secondary on failure.

The fallback provider is transparent to callers: it satisfies the same
``LLMProvider`` protocol and delegates to its primary provider first.  On
any ``LLMUnavailableError`` or ``LLMTimeoutError`` it retries once with the
fallback provider.

``LLMRateLimitError``, ``LLMInvalidRequestError``, and
``LLMCancelledError`` are **not** routed to the fallback — they indicate
caller-side problems that the fallback cannot fix.
"""

from __future__ import annotations

import structlog

from backend.interfaces.llm import (
    LLMCancelledError,
    LLMError,
    LLMInvalidRequestError,
    LLMProvider,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    LLMTimeoutError,
    LLMUnavailableError,
)

log = structlog.get_logger(__name__)

# Errors that should be retried with the fallback provider
_FALLBACK_ON: tuple[type[LLMError], ...] = (LLMUnavailableError, LLMTimeoutError)

# Errors that should NOT be routed to fallback
_NO_FALLBACK_ON: tuple[type[LLMError], ...] = (
    LLMRateLimitError,
    LLMInvalidRequestError,
    LLMCancelledError,
)


class FallbackLLMProvider:
    """Delegates to primary; falls back to secondary on unavailability/timeout.

    Satisfies the ``LLMProvider`` protocol structurally.

    Args:
        primary:   Primary LLMProvider (e.g. SarvamLLMAdapter).
        fallback:  Fallback LLMProvider (e.g. MistralLLMAdapter).  When
                   ``None``, errors from the primary are propagated directly.
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    @property
    def name(self) -> str:
        fallback_name = self._fallback.name if self._fallback else "none"
        return f"fallback({self._primary.name}->{fallback_name})"

    async def generate(self, request: LLMRequest) -> LLMResponse:
        """Try primary; on eligible errors try fallback if configured."""
        try:
            return await self._primary.generate(request)
        except _NO_FALLBACK_ON:
            raise
        except _FALLBACK_ON as primary_exc:
            if self._fallback is None:
                raise

            log.warning(
                "llm_primary_failed_using_fallback",
                primary=self._primary.name,
                fallback=self._fallback.name,
                error=str(primary_exc),
                error_type=type(primary_exc).__name__,
            )
            return await self._fallback.generate(request)
