from __future__ import annotations

import copy

import pytest
from sqlalchemy import create_engine

from enterprise_business_agent.agent.tools import ToolErrorInfo, ToolResult
from enterprise_business_agent.evidence import EvidenceCollector, EvidenceLedger


@pytest.fixture()
def collector(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'run_store.db'}", future=True)
    ledger = EvidenceLedger(engine)
    try:
        yield EvidenceCollector(ledger), ledger
    finally:
        engine.dispose()


def _result(
    *,
    status: str = "ok",
    data: dict | None = None,
    summary: str | None = "华东销售额 1200.00",
    error: ToolErrorInfo | None = None,
) -> ToolResult:
    return ToolResult(
        status=status,
        summary=summary,
        data=data
        if data is not None
        else {
            "metric": "sales_amount",
            "label": "销售额",
            "unit": "CNY",
            "points": [{"period": "2025-12-01", "value": "1200.00"}],
            "explain": {"source_table": "sales_fact"},
        },
        error=error,
    )


def test_collects_metric_query_with_definition_version_and_source(collector):
    sc, ledger = collector
    records = sc.collect(
        run_id="run-1",
        tool_call_id="call-1",
        tool="query_metric",
        arguments={
            "metric": "sales_amount",
            "start": "2025-12-01",
            "end": "2025-12-01",
            "dimensions": [],
            "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
        },
        result=_result(),
    )

    assert len(records) == 1
    record = records[0]
    assert record.run_id == "run-1"
    assert record.tool_call_id == "call-1"
    assert record.evidence_type == "metric_value"
    assert record.domain == "Sales"
    assert record.metric_code == "sales_amount"
    assert record.metric_definition_version == "2.0.0"
    assert record.time_range_start == "2025-12-01"
    assert record.time_range_end == "2025-12-01"
    assert record.grain == "month"
    assert record.unit == "CNY"
    assert record.source_tables == ["sales_fact"]
    assert record.traceable is True
    assert record.status == "valid"

    stored = ledger.get("run-1", record.evidence_id)
    assert stored is not None
    assert stored.query_parameters["metric"] == "sales_amount"


def test_result_payload_is_stored_verbatim(collector):
    sc, ledger = collector
    payload = {
        "metric": "sales_amount",
        "label": "销售额",
        "unit": "CNY",
        "points": [{"period": "2025-12-01", "value": "1200.00", "dimensions": {"region": "EC"}}],
        "explain": {"source_table": "sales_fact", "sql": "SELECT 1", "params": {}},
    }
    before = copy.deepcopy(payload)
    sc.collect(
        run_id="run-1",
        tool_call_id="call-1",
        tool="query_metric",
        arguments={"metric": "sales_amount", "start": "2025-12-01", "end": "2025-12-01"},
        result=_result(data=payload),
    )
    assert payload == before, "collector must not mutate tool output"
    stored = ledger.get("run-1", "run-1-call-1-e1")
    assert stored.result == before


@pytest.mark.parametrize(
    ("tool", "arguments", "expected_type", "expected_start", "expected_end", "grain"),
    [
        (
            "compare_periods",
            {"metric": "sales_amount", "kind": "mom", "period": "2025-12-01"},
            "comparison",
            "2025-12-01",
            "2025-12-01",
            "month",
        ),
        (
            "contribution",
            {
                "metric": "sales_amount",
                "dimension": "region",
                "current_start": "2025-12-01",
                "current_end": "2025-12-01",
                "baseline_start": "2025-11-01",
                "baseline_end": "2025-11-01",
            },
            "contribution",
            "2025-12-01",
            "2025-12-01",
            "month",
        ),
        (
            "top_n",
            {
                "metric": "sales_amount",
                "dimension": "region",
                "start": "2025-12-01",
                "end": "2025-12-01",
                "n": 3,
            },
            "ranking",
            "2025-12-01",
            "2025-12-01",
            "month",
        ),
        (
            "trend",
            {
                "metric": "sales_amount",
                "start": "2025-01-01",
                "end": "2025-12-01",
                "grain": "month",
            },
            "trend",
            "2025-01-01",
            "2025-12-01",
            "month",
        ),
        (
            "anomaly_scan",
            {"metric": "sales_amount", "start": "2025-01-01", "end": "2025-12-01"},
            "anomaly",
            "2025-01-01",
            "2025-12-01",
            "month",
        ),
        (
            "describe_metric",
            {"metric": "sales_amount"},
            "metric_definition",
            None,
            None,
            None,
        ),
    ],
)
def test_tool_to_evidence_type_and_range_mapping(
    collector, tool, arguments, expected_type, expected_start, expected_end, grain
):
    sc, _ = collector
    records = sc.collect(
        run_id="run-1",
        tool_call_id=f"call-{tool}",
        tool=tool,
        arguments=arguments,
        result=_result(),
    )
    record = records[0]
    assert record.evidence_type == expected_type
    assert record.time_range_start == expected_start
    assert record.time_range_end == expected_end
    assert record.grain == grain


def test_comparison_records_baseline_window(collector):
    sc, _ = collector
    record = sc.collect(
        run_id="run-1",
        tool_call_id="call-1",
        tool="compare_periods",
        arguments={"metric": "sales_amount", "kind": "yoy", "period": "2025-12-01"},
        result=_result(),
    )[0]
    assert record.baseline_time_range_start == "2024-12-01"
    assert record.baseline_time_range_end == "2024-12-01"
    assert record.baseline_period == "2024-12-01"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ok", "valid"),
        ("no_data", "insufficient_data"),
        ("insufficient_data", "insufficient_data"),
        ("not_calculable", "not_calculable"),
        ("timeout", "failed"),
        ("execution_error", "failed"),
        ("invalid_request", "rejected"),
        ("permission_denied", "rejected"),
    ],
)
def test_tool_status_maps_to_evidence_status(collector, status, expected):
    sc, _ = collector
    record = sc.collect(
        run_id="run-1",
        tool_call_id="call-1",
        tool="query_metric",
        arguments={"metric": "sales_amount", "start": "2025-12-01", "end": "2025-12-01"},
        result=_result(status=status),
    )[0]
    assert record.status == expected
    if expected != "valid":
        assert record.is_valid is False
        assert record.traceable is False


def test_failed_call_keeps_error_reason_and_is_not_evidence(collector):
    sc, ledger = collector
    result = _result(
        status="timeout",
        data={},
        summary=None,
        error=ToolErrorInfo(code="timeout", message="查询超过 15 秒", retryable=True),
    )
    record = sc.collect(
        run_id="run-1",
        tool_call_id="call-timeout",
        tool="query_metric",
        arguments={"metric": "sales_amount", "start": "2025-12-01", "end": "2025-12-01"},
        result=result,
    )[0]
    assert record.status == "failed"
    assert record.error_code == "timeout"
    assert record.error_message == "查询超过 15 秒"
    assert record.result == {}
    assert ledger.count("run-1") == 1


def test_no_data_call_is_insufficient_not_fabricated(collector):
    sc, _ = collector
    record = sc.collect(
        run_id="run-1",
        tool_call_id="call-empty",
        tool="query_metric",
        arguments={"metric": "sales_amount", "start": "2025-12-01", "end": "2025-12-01"},
        result=_result(status="no_data", data={}, summary="没有数据"),
    )[0]
    assert record.status == "insufficient_data"
    assert record.result == {}


def test_list_metrics_creates_no_evidence(collector):
    sc, ledger = collector
    records = sc.collect(
        run_id="run-1",
        tool_call_id="call-list",
        tool="list_metrics",
        arguments={},
        result=_result(data={"metrics": []}),
    )
    assert records == []
    assert ledger.count("run-1") == 0


def test_repeated_identical_call_is_idempotent(collector):
    sc, ledger = collector
    kwargs = dict(
        run_id="run-1",
        tool="query_metric",
        arguments={"metric": "sales_amount", "start": "2025-12-01", "end": "2025-12-01"},
    )
    first = sc.collect(tool_call_id="call-1", result=_result(), **kwargs)[0]
    second = sc.collect(tool_call_id="call-2", result=_result(), **kwargs)[0]
    assert first.evidence_id == second.evidence_id
    assert ledger.count("run-1") == 1


def test_same_tool_different_filters_creates_two_records(collector):
    sc, ledger = collector
    base = {
        "metric": "sales_amount",
        "start": "2025-12-01",
        "end": "2025-12-01",
    }
    sc.collect(
        run_id="run-1",
        tool_call_id="call-ec",
        tool="query_metric",
        arguments={
            **base,
            "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
        },
        result=_result(),
    )
    sc.collect(
        run_id="run-1",
        tool_call_id="call-nc",
        tool="query_metric",
        arguments={
            **base,
            "filters": [{"dimension": "region", "operator": "eq", "value": "NC"}],
        },
        result=_result(),
    )
    assert ledger.count("run-1") == 2


def test_dimensions_include_filter_values(collector):
    sc, _ = collector
    record = sc.collect(
        run_id="run-1",
        tool_call_id="call-1",
        tool="query_metric",
        arguments={
            "metric": "sales_amount",
            "start": "2025-12-01",
            "end": "2025-12-01",
            "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
        },
        result=_result(),
    )[0]
    assert record.dimensions == {"region": "EC"}
