from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.middleware import JsonFormatter, redact
from enterprise_business_agent.config import Settings
from enterprise_business_agent.db import make_engine
from enterprise_business_agent.evidence import ApprovalNotFound, ApprovalStore
from enterprise_business_agent.observability.alerts import evaluate_alerts
from enterprise_business_agent.observability.setup import (
    configure_tracing,
    force_flush_tracing,
    reset_tracing,
    shutdown_tracing,
    span,
)
from enterprise_business_agent.observability.spans import SpanSink


@pytest.fixture(autouse=True)
def clean_tracing():
    reset_tracing()
    yield
    shutdown_tracing()
    reset_tracing()


def test_file_exporter_writes_a_real_http_span_and_propagates_traceparent(tmp_path):
    trace_file = tmp_path / "spans.jsonl"
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        otel_enabled=True,
        otel_exporter="file",
        otel_trace_file=str(trace_file),
    )
    engine = make_engine("sqlite://")
    app = create_app(
        settings,
        engine=engine,
        llm_client=None,
        result_store=ResultStore(),
    )
    incoming_trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    with TestClient(app) as client:
        response = client.get(
            "/health",
            headers={
                "traceparent": f"00-{incoming_trace}-00f067aa0ba902b7-01",
            },
        )
        assert response.status_code == 200
        assert response.headers["X-Trace-ID"] == incoming_trace
        assert response.headers["traceparent"].startswith(f"00-{incoming_trace}-")
        # Flush while the lifespan (and therefore the tracer provider) is alive;
        # app shutdown flushes again and then drops the provider.
        assert force_flush_tracing() is True

    shutdown_tracing()
    engine.dispose()

    lines = [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    http_spans = [item for item in lines if item["name"] == "http.request"]
    assert http_spans, "the HTTP request span must be exported to the local file"
    exported = http_spans[0]
    assert exported["context"]["trace_id"].endswith(incoming_trace)
    assert exported["attributes"]["http.request.path"] == "/health"
    assert exported["attributes"]["http.response.status_code"] == 200


def test_run_span_can_continue_a_trace_captured_on_another_thread(tmp_path):
    trace_file = tmp_path / "spans.jsonl"
    configure_tracing(
        Settings(
            otel_enabled=True,
            otel_exporter="file",
            otel_trace_file=str(trace_file),
        )
    )
    with span("request.submit") as request_span:
        trace_id = request_span.get_span_context().trace_id
        # Simulate the async worker receiving only a context object.
        from opentelemetry import context as otel_context
        from opentelemetry import trace

        captured = otel_context.get_current()
        sink = SpanSink(run_id="run-async", parent_context=captured)
        sink({"event_type": "run_finished", "status": "completed"})
        assert trace.get_current_span().get_span_context().trace_id == trace_id

    force_flush_tracing()
    shutdown_tracing()
    lines = [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_name = {item["name"]: item for item in lines}
    assert by_name["agent.run"]["context"]["trace_id"] == by_name["request.submit"][
        "context"
    ]["trace_id"]


def test_redaction_masks_credentials_without_hiding_token_counts():
    payload = {
        "api_key": "sk-secret",
        "database_url": "postgresql://user:pass@db/prod",
        "authorization": "Bearer abc",
        "input_tokens": 1234,
        "message": "password=hunter2 request finished",
        "nested": {"EBA_API_KEYS": {"k": "analyst"}},
    }
    cleaned = redact(payload)
    assert cleaned["api_key"] == "***"
    assert cleaned["database_url"] == "***"
    assert cleaned["authorization"] == "***"
    assert cleaned["input_tokens"] == 1234
    assert "hunter2" not in cleaned["message"]
    assert cleaned["nested"]["EBA_API_KEYS"] == "***"


def test_json_log_formatter_includes_trace_and_redacts_extra():
    import logging

    formatter = JsonFormatter()
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "event", (), None
    )
    record.fields = {"api_key": "secret", "run_id": "r1"}
    payload = json.loads(formatter.format(record))
    assert payload["api_key"] == "***"
    assert payload["run_id"] == "r1"
    assert "trace_id" in payload


def test_alerts_flag_failure_timeout_and_missing_tracing():
    metrics = {
        "runs": {
            "terminal": 10,
            "failure_rate": 0.5,
            "p95_execution_seconds": 400,
        },
        "approvals": {"pending": 12},
        "performance": {"timeouts": {"llm": 2}},
        "database": {"connection_errors": 1},
        "observability": {
            "otel_enabled": True,
            "initialized": False,
            "init_error": "no OTLP endpoint configured",
        },
    }
    settings = Settings(
        alert_failure_rate=0.25,
        alert_p95_seconds=300,
        alert_pending_approvals=10,
        alert_min_runs=5,
    )
    result = evaluate_alerts(metrics, settings)
    codes = {item["code"] for item in result["alerts"]}
    assert result["status"] == "critical"
    assert {
        "run_failure_rate_high",
        "run_p95_latency_high",
        "approvals_backlog",
        "llm_timeout_observed",
        "database_connection_errors",
        "tracing_not_initialized",
    } <= codes


def test_alerts_stay_quiet_on_a_healthy_small_sample():
    settings = Settings(alert_min_runs=5)
    result = evaluate_alerts(
        {
            "runs": {"terminal": 2, "failure_rate": 0.5, "p95_execution_seconds": 10},
            "approvals": {"pending": 0},
            "performance": {"timeouts": {"llm": 0}},
            "database": {"connection_errors": 0},
            "observability": {"otel_enabled": False},
        },
        settings,
    )
    assert result["status"] == "ok"
    assert result["alerts"] == []


def test_approval_decision_path_emits_a_span_and_marks_missing_as_error(tmp_path):
    trace_file = tmp_path / "approval.jsonl"
    configure_tracing(
        Settings(
            otel_enabled=True,
            otel_exporter="file",
            otel_trace_file=str(trace_file),
        )
    )
    engine = make_engine("sqlite://")
    store = ApprovalStore(engine)
    with pytest.raises(ApprovalNotFound):
        store.decide(
            report_id="missing-report",
            version=1,
            action="approve",
            actor="approver-1",
            current_fingerprint="fingerprint",
        )
    force_flush_tracing()
    shutdown_tracing()
    engine.dispose()

    spans = [
        json.loads(line)
        for line in trace_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    decide = next(item for item in spans if item["name"] == "approval.decide")
    assert decide["attributes"]["approval.report_id"] == "missing-report"
    assert decide["attributes"]["approval.action"] == "approve"
    assert decide["status"]["status_code"] == "ERROR"
