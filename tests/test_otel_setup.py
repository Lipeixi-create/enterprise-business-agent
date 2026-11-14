"""Tracing is optional: it must report honestly and never affect business paths."""

from __future__ import annotations

import sys

import pytest

from enterprise_business_agent.config import Settings
from enterprise_business_agent.observability import SpanSink
from enterprise_business_agent.observability.setup import (
    clear_run_context,
    configure_tracing,
    reset_tracing,
    set_run_context,
    shutdown_tracing,
    span,
    tracing_status,
)

_ENV_NAMES = (
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "EBA_OTEL_EXPORTER_ENDPOINT",
)


@pytest.fixture(autouse=True)
def clean_tracing(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    reset_tracing()
    yield
    clear_run_context()
    reset_tracing()


class _RecordingExporter:
    def __init__(self, succeed: bool = True):
        from opentelemetry.sdk.trace.export import SpanExportResult

        self._result = SpanExportResult.SUCCESS if succeed else SpanExportResult.FAILURE
        self.spans: list = []

    def export(self, spans):
        self.spans.extend(spans)
        return self._result

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


def test_disabled_tracing_creates_no_span():
    status = configure_tracing(Settings())
    assert status.enabled is False
    assert status.initialized is False
    assert status.exported_spans == 0
    assert "EBA_OTEL_ENABLED is false" in status.note

    with span("db.query", db__system="sqlite") as current:
        assert current is None

    assert tracing_status().exported_spans == 0


def test_enabled_without_endpoint_is_reported_not_raised():
    pytest.importorskip("opentelemetry.sdk.trace")
    status = configure_tracing(Settings(otel_enabled=True))
    assert status.enabled is True
    assert status.sdk_available is True
    assert status.exporter_configured is False
    assert status.initialized is False
    assert status.init_error == "no OTLP endpoint configured"


def test_missing_sdk_is_reported_without_raising(monkeypatch):
    for name in (
        "opentelemetry.sdk",
        "opentelemetry.sdk.resources",
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.trace.export",
    ):
        monkeypatch.setitem(sys.modules, name, None)

    status = configure_tracing(
        Settings(
            otel_enabled=True,
            otel_exporter_endpoint="http://127.0.0.1:4318/v1/traces",
        )
    )
    assert status.enabled is True
    assert status.sdk_available is False
    assert status.initialized is False
    assert "not installed" in status.note

    with span("db.query") as current:
        assert current is None


def test_successful_export_is_counted_only_after_the_export_call():
    pytest.importorskip("opentelemetry.sdk.trace")
    exporter = _RecordingExporter(succeed=True)
    status = configure_tracing(Settings(otel_enabled=True), exporter=exporter)
    assert status.initialized is True
    assert status.exporter_kind == "injected"
    assert status.exported_spans == 0

    with span("db.query", db__system="sqlite", sql__hash="abc123") as current:
        assert current is not None
    assert tracing_status().exported_spans == 0

    shutdown_tracing()
    assert tracing_status().exported_spans == 1
    assert tracing_status().dropped_spans == 0
    assert [item.name for item in exporter.spans] == ["db.query"]
    assert exporter.spans[0].attributes["db.system"] == "sqlite"
    assert exporter.spans[0].attributes["sql.hash"] == "abc123"


def test_failed_export_is_counted_as_dropped_not_exported():
    pytest.importorskip("opentelemetry.sdk.trace")
    exporter = _RecordingExporter(succeed=False)
    configure_tracing(Settings(otel_enabled=True), exporter=exporter)

    with span("db.query", db__system="sqlite") as current:
        assert current is not None
    shutdown_tracing()

    status = tracing_status()
    assert status.exported_spans == 0
    assert status.dropped_spans == 1


def test_tool_and_database_spans_share_one_trace_and_carry_run_context():
    pytest.importorskip("opentelemetry.sdk.trace")
    exporter = _RecordingExporter()
    configure_tracing(Settings(otel_enabled=True), exporter=exporter)

    sink = SpanSink(run_id="run-1", service_name="test")
    sink({"event_type": "decision_started", "round": 1})
    sink(
        {
            "event_type": "tool_call_started",
            "tool": "query_metric",
            "call_id": "c1",
            "arguments": {"metric": "revenue"},
        }
    )
    set_run_context("run-1", "c1")
    try:
        with span("db.query", db__system="sqlite") as db_span:
            assert db_span is not None
    finally:
        clear_run_context()
    sink(
        {
            "event_type": "tool_call_finished",
            "tool": "query_metric",
            "call_id": "c1",
            "status": "ok",
        }
    )
    sink({"event_type": "run_finished", "status": "completed"})
    shutdown_tracing()

    by_name = {item.name: item for item in exporter.spans}
    assert {"agent.run", "agent.decision", "tool.call", "db.query"} <= set(by_name)
    assert (
        by_name["db.query"].context.trace_id
        == by_name["tool.call"].context.trace_id
        == by_name["agent.run"].context.trace_id
    )
    assert by_name["db.query"].attributes["agent.run_id"] == "run-1"
    assert by_name["db.query"].attributes["tool.call_id"] == "c1"


def test_checkpoint_restore_span_records_missing_checkpoint_as_error():
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.trace import StatusCode

    exporter = _RecordingExporter()
    configure_tracing(Settings(otel_enabled=True), exporter=exporter)

    sink = SpanSink(run_id="run-2", service_name="test")
    sink.checkpoint_restored(thread_id="run-2", found=False, restore_ms=1.5)
    sink({"event_type": "run_finished", "status": "failed"})
    shutdown_tracing()

    restore = next(item for item in exporter.spans if item.name == "checkpoint.restore")
    assert restore.attributes["checkpoint.found"] is False
    assert restore.attributes["checkpoint.thread_id"] == "run-2"
    assert restore.status.status_code == StatusCode.ERROR
