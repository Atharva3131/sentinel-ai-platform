"""Fake LLM provider implementations for testing.

These fakes satisfy the ``LLMProvider`` protocol without making any
network calls.  They are configurable stubs — test suites inject the
responses they need.

FakeLLMProvider    — returns pre-configured responses in sequence
StructuredFakeLLM  — always returns a structured JSON response
ToolCallFakeLLM    — returns tool calls on first turn, then a final answer
FailingFakeLLM     — raises a configurable LLMError
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from backend.interfaces.common import ProviderUsage
from backend.interfaces.llm import (
    LLMCancelledError,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMToolCall,
)


class FakeLLMProvider:
    """Returns pre-configured responses in sequence, then repeats the last one."""

    name = "fake-llm"

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        model: str = "fake-model-1",
        usage: ProviderUsage | None = None,
    ) -> None:
        self._responses = responses or ["I am a fake LLM response."]
        self._model = model
        self._usage = usage or ProviderUsage(input_tokens=10, output_tokens=5, total_tokens=15)
        self._call_count = 0
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        # Respect cancellation
        if request.cancellation_token is not None and request.cancellation_token.is_set():
            raise LLMCancelledError("Request cancelled", provider=self.name)

        self.requests.append(request)
        idx = min(self._call_count, len(self._responses) - 1)
        content = self._responses[idx]
        self._call_count += 1

        structured: dict[str, Any] | None = None
        if request.structured_schema is not None:
            try:
                structured = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                structured = {"content": content}

        return LLMResponse(
            content=content,
            model=self._model,
            finish_reason="stop",
            usage=self._usage,
            structured_output=structured,
            context=request.context,
        )

    @property
    def call_count(self) -> int:
        return self._call_count


class StructuredFakeLLM:
    """Always returns a pre-configured dict as structured output."""

    name = "structured-fake-llm"

    def __init__(self, output: dict[str, Any]) -> None:
        self._output = output
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        content = json.dumps(self._output)
        return LLMResponse(
            content=content,
            model="structured-fake-model",
            finish_reason="stop",
            structured_output=self._output,
            usage=ProviderUsage(input_tokens=20, output_tokens=30, total_tokens=50),
            context=request.context,
        )


class ToolCallFakeLLM:
    """Returns tool calls on the first N turns, then a plain text answer.

    Useful for testing the iterative tool-call loop in the investigation agent.
    """

    name = "tool-call-fake-llm"

    def __init__(
        self,
        tool_calls: list[list[LLMToolCall]],
        final_response: str = "Investigation complete.",
        *,
        structured_final: dict[str, Any] | None = None,
    ) -> None:
        """
        Args:
            tool_calls: list of per-turn tool call lists.  Turn 0 returns
                        tool_calls[0], turn 1 returns tool_calls[1], etc.
            final_response: text returned when all tool call turns are exhausted.
            structured_final: optional structured output on the final turn.
        """
        self._tool_call_turns: Iterator[list[LLMToolCall]] = iter(tool_calls)
        self._final_response = final_response
        self._structured_final = structured_final
        self._call_count = 0
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        self._call_count += 1

        try:
            calls = next(self._tool_call_turns)
            return LLMResponse(
                content="",
                model="tool-fake-model",
                finish_reason="tool_calls",
                tool_calls=tuple(calls),
                usage=ProviderUsage(input_tokens=15, output_tokens=5, total_tokens=20),
                context=request.context,
            )
        except StopIteration:
            final_content = self._final_response
            structured: dict[str, Any] | None = self._structured_final
            if structured is None and request.structured_schema is not None:
                try:
                    structured = json.loads(final_content)
                except (json.JSONDecodeError, ValueError):
                    structured = {"content": final_content}
            return LLMResponse(
                content=final_content,
                model="tool-fake-model",
                finish_reason="stop",
                structured_output=structured,
                usage=ProviderUsage(input_tokens=20, output_tokens=40, total_tokens=60),
                context=request.context,
            )


class FailingFakeLLM:
    """Always raises a configurable LLMError."""

    name = "failing-fake-llm"

    def __init__(self, error: LLMError | None = None) -> None:
        from backend.interfaces.llm import LLMUnavailableError
        self._error = error or LLMUnavailableError(
            "Fake provider is unavailable", provider="failing-fake-llm"
        )
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        raise self._error
