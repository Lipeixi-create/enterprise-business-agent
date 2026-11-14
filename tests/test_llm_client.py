from __future__ import annotations

import httpx
import pytest

from enterprise_business_agent.config import Settings
from enterprise_business_agent.llm import (
    AnthropicClient,
    LLMInvalidResponse,
    LLMTimeout,
    LLMTruncated,
    LLMUnavailable,
    build_llm_client,
)
from enterprise_business_agent.llm.client import _to_anthropic_messages


def _settings(**overrides):
    values = {
        "llm_api_key": "sk-test-secret",
        "llm_model": "fake-model",
        "llm_max_retries": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _response(status_code: int, payload: dict | None = None, text: str | None = None):
    request = httpx.Request("POST", "https://example.invalid/v1/messages")
    if text is not None:
        return httpx.Response(status_code, request=request, text=text)
    return httpx.Response(status_code, request=request, json=payload or {})


class FakeHttpClient:
    responses: list = []
    calls: list = []

    def __init__(self, timeout):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        FakeHttpClient.calls.append({"url": url, "headers": headers, "json": json})
        item = FakeHttpClient.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def fake_transport(monkeypatch):
    FakeHttpClient.responses = []
    FakeHttpClient.calls = []
    monkeypatch.setattr("enterprise_business_agent.llm.client.httpx.Client", FakeHttpClient)
    monkeypatch.setattr("enterprise_business_agent.llm.client.time.sleep", lambda _seconds: None)


def test_build_llm_client_returns_none_without_key():
    assert build_llm_client(Settings(llm_api_key=None)) is None


def test_client_parses_fenced_json_and_usage():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "fake-model-2026",
                "content": [{"type": "text", "text": '```json\n{"plan": {"metric": "sales_amount"}}\n```'}],
                "usage": {"input_tokens": 12, "output_tokens": 7},
            },
        )
    )
    result = AnthropicClient(_settings()).complete_json(system="s", prompt="p")
    assert result.data == {"plan": {"metric": "sales_amount"}}
    assert result.model == "fake-model-2026"
    assert (result.input_tokens, result.output_tokens) == (12, 7)


def test_client_redacts_api_key_in_repr():
    client = AnthropicClient(_settings())
    assert "sk-test-secret" not in repr(client)
    assert "redacted" in repr(client)


def test_authentication_failure_is_not_retried():
    FakeHttpClient.responses.append(_response(401, {"error": "invalid key"}))
    with pytest.raises(LLMUnavailable):
        AnthropicClient(_settings()).complete_json(system="s", prompt="p")
    assert len(FakeHttpClient.calls) == 1


def test_server_error_is_retried_then_succeeds():
    FakeHttpClient.responses.extend(
        [
            _response(500, {}),
            _response(200, {"content": [{"type": "text", "text": '{"ok": true}'}]}),
        ]
    )
    result = AnthropicClient(_settings()).complete_json(system="s", prompt="p")
    assert result.data == {"ok": True}
    assert len(FakeHttpClient.calls) == 2


def test_timeouts_are_bounded_by_max_retries():
    FakeHttpClient.responses.extend(
        [httpx.TimeoutException("timeout"), httpx.TimeoutException("timeout"), httpx.TimeoutException("timeout")]
    )
    with pytest.raises(LLMTimeout):
        AnthropicClient(_settings(llm_max_retries=2)).complete_json(system="s", prompt="p")
    assert len(FakeHttpClient.calls) == 3


def test_invalid_json_raises_structured_error():
    FakeHttpClient.responses.append(_response(200, {"content": [{"type": "text", "text": "not json"}]}))
    with pytest.raises(LLMInvalidResponse):
        AnthropicClient(_settings()).complete_json(system="s", prompt="p")


def test_prompt_is_not_logged(caplog):
    FakeHttpClient.responses.append(
        _response(200, {"content": [{"type": "text", "text": '{"ok": true}'}]})
    )
    AnthropicClient(_settings()).complete_json(system="s", prompt="secret prompt")
    assert "secret prompt" not in caplog.text
    assert "sk-test-secret" not in caplog.text


def test_complete_with_tools_parses_text_and_tool_use():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "fake-model-2026",
                "content": [
                    {"type": "text", "text": "先查询销售额"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "query_metric",
                        "input": {"metric": "sales_amount"},
                    },
                ],
                "usage": {"input_tokens": 20, "output_tokens": 9},
            },
        )
    )
    result = AnthropicClient(_settings()).complete_with_tools(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[{"name": "query_metric", "description": "d", "input_schema": {}}],
    )
    assert result.text == "先查询销售额"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].call_id == "toolu_1"
    assert result.tool_calls[0].arguments == {"metric": "sales_amount"}
    assert (result.input_tokens, result.output_tokens) == (20, 9)
    payload = FakeHttpClient.calls[0]["json"]
    assert payload["tool_choice"] == {"type": "auto"}
    assert payload["tools"][0]["name"] == "query_metric"


def test_complete_with_tools_rejects_non_object_input():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "query_metric",
                        "input": ["not", "an", "object"],
                    }
                ]
            },
        )
    )
    with pytest.raises(LLMInvalidResponse):
        AnthropicClient(_settings()).complete_with_tools(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
        )


def test_tool_call_respects_agent_time_budget_before_http():
    with pytest.raises(LLMTimeout):
        AnthropicClient(_settings()).complete_with_tools(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            timeout_seconds=0.0,
        )
    assert FakeHttpClient.calls == []


def test_tool_results_and_assistant_tool_calls_are_converted():
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "name": "query_metric", "arguments": {"metric": "sales_amount"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": '{"status": "ok"}'},
    ]
    converted = _to_anthropic_messages(messages)
    assert converted[1]["role"] == "assistant"
    assert converted[1]["content"][0]["type"] == "tool_use"
    assert converted[1]["content"][0]["id"] == "t1"
    assert converted[2]["role"] == "user"
    assert converted[2]["content"][0]["type"] == "tool_result"
    assert converted[2]["content"][0]["tool_use_id"] == "t1"


def test_consecutive_tool_result_and_decision_prompt_are_merged():
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "t1", "name": "query_metric", "arguments": {"metric": "sales_amount"}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": '{"status": "ok"}'},
        {"role": "user", "content": "choose the next step"},
    ]
    converted = _to_anthropic_messages(messages)
    assert len(converted) == 3
    assert [block["type"] for block in converted[2]["content"]] == [
        "tool_result",
        "text",
    ]
    assert converted[2]["content"][1]["text"] == "choose the next step"


def test_truncated_json_is_reported_as_truncation_not_generic_error():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "fake-model",
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": '{"plan": {"metric": '}],
                "usage": {"input_tokens": 10, "output_tokens": 64},
            },
        )
    )
    with pytest.raises(LLMTruncated):
        AnthropicClient(_settings()).complete_json(
            system="s", prompt="p", max_tokens=64
        )
    assert FakeHttpClient.calls[0]["json"]["max_tokens"] == 64


def test_truncated_tool_response_is_flagged():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "fake-model",
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": "部分结论"}],
                "usage": {"input_tokens": 10, "output_tokens": 64},
            },
        )
    )
    result = AnthropicClient(_settings()).complete_with_tools(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[],
        max_tokens=64,
    )
    assert result.truncated is True
    assert result.stop_reason == "max_tokens"
    assert result.text == "部分结论"
    assert FakeHttpClient.calls[0]["json"]["max_tokens"] == 64


def test_connection_failure_becomes_structured_unavailable_after_retries():
    FakeHttpClient.responses.extend(
        [httpx.ConnectError("connection refused")] * 3
    )
    with pytest.raises(LLMUnavailable):
        AnthropicClient(_settings(llm_max_retries=2)).complete_json(
            system="s", prompt="p"
        )
    assert len(FakeHttpClient.calls) == 3


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_tool_call_reports_no_attempt_when_the_window_is_below_the_minimum():
    with pytest.raises(LLMTimeout) as excinfo:
        AnthropicClient(_settings()).complete_with_tools(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            timeout_seconds=4.0,
        )
    assert FakeHttpClient.calls == []
    assert excinfo.value.attempts == 0
    assert excinfo.value.budget_limited is True


def test_retry_is_not_sent_when_the_window_cannot_fit_another_attempt(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("enterprise_business_agent.llm.client.time.monotonic", clock)

    def timing_out_post(self, url, headers=None, json=None):
        FakeHttpClient.calls.append({"url": url, "headers": headers, "json": json})
        clock.advance(6.0)
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(FakeHttpClient, "post", timing_out_post)
    with pytest.raises(LLMTimeout) as excinfo:
        AnthropicClient(_settings(llm_max_retries=2)).complete_with_tools(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            timeout_seconds=8.0,
        )
    # 2s remained after the first attempt: neither the backoff nor a 5s
    # request window fits, so the case budget is preserved.
    assert len(FakeHttpClient.calls) == 1
    assert excinfo.value.attempts == 1
    assert excinfo.value.budget_limited is True


def test_tool_response_reports_attempts_and_clamps_the_request_timeout(monkeypatch):
    seen_timeouts: list[float] = []
    original_enter = FakeHttpClient.__enter__

    def enter(self):
        seen_timeouts.append(getattr(self.timeout, "read", self.timeout))
        return original_enter(self)

    monkeypatch.setattr(FakeHttpClient, "__enter__", enter)
    FakeHttpClient.responses.extend(
        [
            _response(500, {}),
            _response(
                200,
                {
                    "model": "fake-model",
                    "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn",
                },
            ),
        ]
    )
    result = AnthropicClient(_settings()).complete_with_tools(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[],
        timeout_seconds=12.0,
    )
    assert result.attempts == 2
    assert len(FakeHttpClient.calls) == 2
    # The configured per-request timeout is larger than the window; each
    # attempt must use what remains instead of the full configured value.
    assert seen_timeouts[0] == pytest.approx(12.0)
    assert seen_timeouts[1] <= 12.0
