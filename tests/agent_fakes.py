"""TEST-ONLY scripted LLM doubles.

These are not a model-quality evaluation. They prove the control loop,
tool feedback, and failure handling without claiming real LLM behavior.
"""

from __future__ import annotations

from typing import Any, Callable

from enterprise_business_agent.llm import LLMToolCall, LLMToolResponse


Response = LLMToolResponse | Exception | Callable[["ScriptedLLM"], LLMToolResponse]


class ScriptedLLM:
    model = "fake-scripted-model"

    def __init__(self, responses: list[Response]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMToolResponse:
        self.calls.append(
            {
                "system": system,
                "messages": messages,
                "tools": tools,
                "timeout_seconds": timeout_seconds,
                "max_tokens": max_tokens,
            }
        )
        if not self.responses:
            raise AssertionError("fake LLM has no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(self)
        return item


def tool_response(
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    text: str | None = None,
) -> LLMToolResponse:
    return LLMToolResponse(
        text=text,
        tool_calls=[LLMToolCall(call_id=call_id, name=name, arguments=arguments)],
        stop_reason="tool_use",
        model="fake-scripted-model",
    )


def finish_response(text: str) -> LLMToolResponse:
    return LLMToolResponse(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        model="fake-scripted-model",
    )


def truncated_response(text: str) -> LLMToolResponse:
    """A final answer cut off by the provider's output token cap."""

    return LLMToolResponse(
        text=text,
        tool_calls=[],
        stop_reason="length",
        model="fake-scripted-model",
        truncated=True,
    )
