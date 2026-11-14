from __future__ import annotations

from datetime import date

import pytest

from enterprise_business_agent.agent import AgentRuntime, AgentStatus
from enterprise_business_agent.config import Settings
from enterprise_business_agent.observability import spans as spans_module
from enterprise_business_agent.observability.spans import SpanSink
from enterprise_business_agent.security import Principal
from tests.agent_fakes import ScriptedLLM, finish_response


ANALYST = Principal("key:test", "analyst", frozenset({"metrics:read", "agent:run"}))


class FakeSpan:
    def __init__(self, name: str, attributes: dict | None, parent):
        self.name = name
        self.attributes = dict(attributes or {})
        self.parent = parent
        self.events: list[tuple[str, dict]] = []
        self.status = None
        self.ended = False

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        self.events.append((name, dict(attributes or {})))

    def set_status(self, status):
        self.status = status

    def end(self):
        self.ended = True


class FakeTraceModule:
    tracer: "FakeTracer | None" = None

    @staticmethod
    def set_span_in_context(span):
        return {"parent": span}

    @classmethod
    def get_tracer(cls, name):
        return cls.tracer


class FakeTracer:
    def __init__(self, fail: bool = False):
        self.spans: list[FakeSpan] = []
        self.fail = fail

    def start_span(self, name, context=None, attributes=None):
        if self.fail:
            raise RuntimeError("tracer backend down")
        parent = (context or {}).get("parent")
        span = FakeSpan(name, attributes, parent)
        self.spans.append(span)
        return span


def _use_tracer(monkeypatch, tracer):
    FakeTraceModule.tracer = tracer
    monkeypatch.setattr(
        spans_module, "_load_trace_module", lambda: FakeTraceModule
    )
    return tracer


def _events():
    return [
        {"event_type": "decision_started", "round": 1, "budget_left": 4, "tokens_used": 0},
        {
            "event_type": "model_response",
            "model": "fake-scripted-model",
            "latency_ms": 12.5,
            "input_tokens": 100,
            "output_tokens": 8,
            "tool_calls": [{"id": "c1", "name": "query_metric", "arguments": {}}],
            "has_text": True,
        },
        {
            "event_type": "tool_call_started",
            "tool": "query_metric",
            "call_id": "c1",
            "arguments": {"metric": "sales_amount"},
        },
        {
            "event_type": "tool_call_finished",
            "tool": "query_metric",
            "call_id": "c1",
            "status": "ok",
            "duration_ms": 3.2,
            "evidence_ids": ["e1"],
            "error_code": None,
        },
        {
            "event_type": "run_finished",
            "status": "completed",
            "stop_reason": "model_finished",
            "tool_calls_used": 1,
            "input_tokens": 100,
            "output_tokens": 8,
            "latency_ms": 12.5,
            "provider": "glm",
            "model": "fake-scripted-model",
            "prompt_version": "phase4.3-test",
        },
    ]


def test_span_sink_builds_nested_run_decision_llm_and_tool_spans(monkeypatch):
    tracer = _use_tracer(monkeypatch, FakeTracer())
    sink = SpanSink(
        run_id="run-1",
        provider="glm",
        prompt_version="phase4.3-test",
        tracer=tracer,
    )
    for event in _events():
        sink(event)

    names = [span.name for span in tracer.spans]
    assert names == ["agent.run", "agent.decision", "llm.complete", "tool.call"]
    root, decision, llm_span, tool_span = tracer.spans
    assert root.parent is None
    assert root.attributes["agent.run_id"] == "run-1"
    assert root.attributes["agent.status"] == "completed"
    assert root.attributes["llm.provider"] == "glm"
    assert root.ended
    assert decision.parent is root
    assert decision.ended
    assert llm_span.parent is decision
    assert llm_span.attributes["llm.tool_call_count"] == 1
    assert llm_span.ended
    assert tool_span.parent is decision
    assert tool_span.attributes["tool.status"] == "ok"
    assert tool_span.attributes["tool.evidence_count"] == 1
    assert tool_span.ended
    assert all(span.ended for span in tracer.spans)


def test_llm_and_tool_spans_carry_measured_timing_and_tokens(monkeypatch):
    tracer = _use_tracer(monkeypatch, FakeTracer())
    sink = SpanSink(run_id="run-3", tracer=tracer)
    for event in _events():
        sink(event)

    llm_span = next(span for span in tracer.spans if span.name == "llm.complete")
    assert llm_span.attributes["llm.latency_ms"] == 12.5
    assert llm_span.attributes["llm.input_tokens"] == 100
    assert llm_span.attributes["llm.output_tokens"] == 8
    tool_span = next(span for span in tracer.spans if span.name == "tool.call")
    assert tool_span.attributes["tool.duration_ms"] == 3.2


def test_tool_duration_is_absent_when_not_measured(monkeypatch):
    tracer = _use_tracer(monkeypatch, FakeTracer())
    sink = SpanSink(run_id="run-4", tracer=tracer)
    sink({"event_type": "decision_started", "round": 1})
    sink(
        {
            "event_type": "tool_call_started",
            "tool": "query_metric",
            "call_id": "c9",
            "arguments": {},
        }
    )
    sink(
        {
            "event_type": "tool_call_finished",
            "tool": "query_metric",
            "call_id": "c9",
            "status": "ok",
        }
    )

    tool_span = tracer.spans[-1]
    # A duration that was never recorded stays absent instead of being inferred.
    assert "tool.duration_ms" not in tool_span.attributes


def test_span_sink_marks_denied_tool_and_incomplete_run_without_raising(monkeypatch):
    tracer = _use_tracer(monkeypatch, FakeTracer())
    sink = SpanSink(run_id="run-2", tracer=tracer)
    for event in _events():
        if event["event_type"] == "tool_call_finished":
            event = {
                **event,
                "status": "permission_denied",
                "error_code": "permission_denied",
                "evidence_ids": [],
            }
        if event["event_type"] == "run_finished":
            event = {**event, "status": "invalid_action"}
        sink(event)

    tool_span = tracer.spans[-1]
    assert tool_span.attributes["tool.error_code"] == "permission_denied"
    assert tool_span.status is not None
    assert tracer.spans[0].status is not None


def test_broken_tracer_never_breaks_a_run(monkeypatch):
    _use_tracer(monkeypatch, FakeTracer(fail=True))
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=1,
        agent_max_tool_calls=2,
        agent_max_seconds=5,
        otel_enabled=True,
    )
    registry = _StubRegistry()
    runtime = AgentRuntime(
        llm_client=ScriptedLLM([finish_response("今天没有可分析的数据。")]),
        registry=registry,
        settings=settings,
    )
    events: list[dict] = []
    response = runtime.run("随便看看", ANALYST, trace_sink=events.append)

    assert response.status == AgentStatus.NO_EVIDENCE
    assert [item["event_type"] for item in events] == [
        "decision_started",
        "model_response",
        "run_finished",
    ]


def test_enabled_tracing_records_spans_for_a_real_run(monkeypatch):
    tracer = _use_tracer(monkeypatch, FakeTracer())
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=1,
        agent_max_tool_calls=2,
        agent_max_seconds=5,
        otel_enabled=True,
        otel_service_name="eba-test",
    )
    runtime = AgentRuntime(
        llm_client=ScriptedLLM([finish_response("本次没有查询到数据。")]),
        registry=_StubRegistry(),
        settings=settings,
    )
    runtime.run("随便看看", ANALYST)

    names = [span.name for span in tracer.spans]
    assert names == ["agent.run", "agent.decision", "llm.complete"]
    assert all(span.ended for span in tracer.spans)


def test_tracing_is_off_by_default(monkeypatch):
    def explode():
        raise AssertionError("tracer must not be loaded when tracing is disabled")

    monkeypatch.setattr(spans_module, "_load_trace_module", explode)
    settings = Settings(data_start=date(2024, 1, 1), data_months=1)
    runtime = AgentRuntime(
        llm_client=ScriptedLLM([finish_response("本次没有查询到数据。")]),
        registry=_StubRegistry(),
        settings=settings,
    )
    response = runtime.run("随便看看", ANALYST)
    assert response.status == AgentStatus.NO_EVIDENCE


class _StubRegistry:
    def specs(self, principal):
        return []

    def context(self, principal):
        return ""

    def execute(self, *args, **kwargs):
        raise AssertionError("no tool call expected")
