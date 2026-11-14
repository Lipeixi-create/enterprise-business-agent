from __future__ import annotations

import json

import httpx
import pytest

from enterprise_business_agent.config import Settings
from enterprise_business_agent.llm import (
    GLMClient,
    LLMInvalidResponse,
    LLMTimeout,
    LLMTruncated,
    LLMUnavailable,
    build_llm_client,
)
from enterprise_business_agent.llm.glm import _glm_messages, _glm_tools


def _settings(**overrides):
    values = {
        "llm_provider": "glm",
        "llm_model": "glm-5.3-flash",
        "llm_base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "llm_api_key": "test-glm-secret",
        "llm_max_retries": 2,
    }
    values.update(overrides)
    return Settings(**values)


def _response(status_code: int, payload: dict | None = None):
    request = httpx.Request("POST", "https://open.bigmodel.cn/api/paas/v4/chat/completions")
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
    monkeypatch.setattr("enterprise_business_agent.llm.glm.httpx.Client", FakeHttpClient)
    monkeypatch.setattr("enterprise_business_agent.llm.glm.time.sleep", lambda _seconds: None)


def test_factory_selects_glm_and_anthropic():
    assert isinstance(build_llm_client(_settings()), GLMClient)
    assert build_llm_client(_settings(llm_api_key=None)) is None


def test_post_is_bounded_by_wall_clock_deadline(monkeypatch):
    import time as real_time

    class TricklingClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            import threading

            threading.Event().wait(0.5)
            raise AssertionError("response arrived after the deadline")

    monkeypatch.setattr(
        "enterprise_business_agent.llm.glm.httpx.Client", TricklingClient
    )
    client = GLMClient(_settings())
    started = real_time.monotonic()
    with pytest.raises(httpx.ReadTimeout):
        client._post({"model": "x"}, 0.05)
    assert real_time.monotonic() - started < 0.4


def test_glm_tool_schema_and_message_conversion():
    tools = [
        {
            "name": "query_metric",
            "description": "query",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    converted = _glm_tools(tools)
    assert converted[0]["type"] == "function"
    assert converted[0]["function"]["parameters"] == {"type": "object", "properties": {}}
    messages = _glm_messages(
        "system",
        [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "name": "query_metric", "arguments": {"metric": "sales_amount"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"status":"ok"}'},
        ],
    )
    assert messages[0] == {"role": "system", "content": "system"}
    assert messages[2]["tool_calls"][0]["function"]["arguments"] == '{"metric": "sales_amount"}'
    assert messages[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"status":"ok"}',
    }


def test_glm_parses_tool_calls_and_usage():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "glm-5.3-flash",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "query_metric",
                                        "arguments": '{"metric":"sales_amount"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 9},
            },
        )
    )
    result = GLMClient(_settings()).complete_with_tools(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[{"name": "query_metric", "description": "d", "input_schema": {}}],
    )
    assert result.text is None
    assert result.tool_calls[0].call_id == "call_1"
    assert result.tool_calls[0].arguments == {"metric": "sales_amount"}
    assert result.stop_reason == "tool_calls"
    assert (result.input_tokens, result.output_tokens) == (20, 9)
    payload = FakeHttpClient.calls[0]["json"]
    assert payload["tools"][0]["type"] == "function"
    assert payload["tool_choice"] == "auto"
    assert FakeHttpClient.calls[0]["headers"]["Authorization"].startswith("Bearer ")


def test_glm_rejects_invalid_json_arguments():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {"name": "query_metric", "arguments": "not-json"},
                                }
                            ]
                        }
                    }
                ]
            },
        )
    )
    with pytest.raises(LLMInvalidResponse):
        GLMClient(_settings()).complete_with_tools(
            system="s", messages=[{"role": "user", "content": "q"}], tools=[]
        )


def test_glm_401_is_not_retried_and_key_is_redacted():
    FakeHttpClient.responses.append(_response(401, {"error": {"message": "bad key"}}))
    client = GLMClient(_settings())
    assert "test-glm-secret" not in repr(client)
    with pytest.raises(LLMUnavailable):
        client.complete_with_tools(
            system="s", messages=[{"role": "user", "content": "q"}], tools=[]
        )
    assert len(FakeHttpClient.calls) == 1


def test_glm_429_is_retried_then_succeeds():
    FakeHttpClient.responses.extend(
        [
            _response(429, {}),
            _response(
                200,
                {
                    "choices": [
                        {"message": {"content": "ok"}, "finish_reason": "stop"}
                    ]
                },
            ),
        ]
    )
    result = GLMClient(_settings()).complete_with_tools(
        system="s", messages=[{"role": "user", "content": "q"}], tools=[]
    )
    assert result.text == "ok"
    assert len(FakeHttpClient.calls) == 2


def test_glm_timeout_respects_budget_before_http():
    with pytest.raises(LLMTimeout):
        GLMClient(_settings()).complete_with_tools(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            timeout_seconds=0,
        )
    assert FakeHttpClient.calls == []


def test_glm_complete_json_parses_fenced_json():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "glm-5.3-flash",
                "choices": [
                    {
                        "message": {"content": "```json\n{\"ok\": true}\n```"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4},
            },
        )
    )
    result = GLMClient(_settings()).complete_json(system="s", prompt="p")
    assert result.data == {"ok": True}
    assert result.input_tokens == 8
    assert result.output_tokens == 4


def test_glm_truncated_json_is_reported_as_truncation():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "glm-5.3-flash",
                "choices": [
                    {
                        "message": {"content": '{"sections": [{"title": "a"'},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    with pytest.raises(LLMTruncated):
        GLMClient(_settings()).complete_json(
            system="s", prompt="p", max_tokens=128
        )
    assert FakeHttpClient.calls[0]["json"]["max_tokens"] == 128


def test_glm_non_truncated_invalid_json_stays_a_format_error():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "glm-5.3-flash",
                "choices": [
                    {"message": {"content": "not json"}, "finish_reason": "stop"}
                ],
            },
        )
    )
    with pytest.raises(LLMInvalidResponse) as excinfo:
        GLMClient(_settings()).complete_json(system="s", prompt="p")
    assert not isinstance(excinfo.value, LLMTruncated)


def test_glm_truncated_tool_response_is_flagged():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "model": "glm-5.3-flash",
                "choices": [
                    {
                        "message": {"content": "结论：销售额同比"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 64},
            },
        )
    )
    result = GLMClient(_settings()).complete_with_tools(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[],
        max_tokens=64,
    )
    assert result.truncated is True
    assert result.stop_reason == "length"
    assert result.text == "结论：销售额同比"
    assert FakeHttpClient.calls[0]["json"]["max_tokens"] == 64


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_glm_zero_attempts_are_reported_when_the_window_is_below_the_minimum():
    with pytest.raises(LLMTimeout) as excinfo:
        GLMClient(_settings()).complete_with_tools(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            timeout_seconds=3.0,
        )
    assert FakeHttpClient.calls == []
    assert excinfo.value.attempts == 0
    assert excinfo.value.budget_limited is True


def test_glm_retry_is_not_sent_when_the_window_cannot_fit_another_attempt(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr("enterprise_business_agent.llm.glm.time.monotonic", clock)

    def timing_out_post(self, url, headers=None, json=None):
        FakeHttpClient.calls.append({"url": url, "headers": headers, "json": json})
        clock.advance(7.0)
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(FakeHttpClient, "post", timing_out_post)
    with pytest.raises(LLMTimeout) as excinfo:
        GLMClient(_settings(llm_max_retries=2)).complete_with_tools(
            system="s",
            messages=[{"role": "user", "content": "q"}],
            tools=[],
            timeout_seconds=9.0,
        )
    assert len(FakeHttpClient.calls) == 1
    assert excinfo.value.attempts == 1
    assert excinfo.value.budget_limited is True


def test_glm_tool_response_reports_attempts_and_clamps_the_timeout(monkeypatch):
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
                {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
            ),
        ]
    )
    result = GLMClient(_settings()).complete_with_tools(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[],
        timeout_seconds=14.0,
    )
    assert result.attempts == 2
    assert seen_timeouts[0] == pytest.approx(14.0)
    assert seen_timeouts[1] <= 14.0


def test_temperature_and_seed_are_configurable_for_stability_runs():
    FakeHttpClient.responses.append(
        _response(
            200,
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "done"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    client = GLMClient(_settings(llm_temperature=0.8, llm_seed=42))
    client.complete_with_tools(
        system="s",
        messages=[{"role": "user", "content": "q"}],
        tools=[],
    )
    payload = FakeHttpClient.calls[0]["json"]
    assert payload["temperature"] == 0.8
    assert payload["seed"] == 42
