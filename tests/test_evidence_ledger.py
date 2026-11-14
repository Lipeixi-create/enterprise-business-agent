from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text

from enterprise_business_agent.evidence import (
    EvidenceLedger,
    EvidenceRecord,
    create_schema,
    drop_schema,
)
from enterprise_business_agent.evidence.schema import (
    EVIDENCE_METADATA,
    EVIDENCE_TABLE,
    FINDING_TABLE,
    REPORT_TABLE,
)


def _record(
    *,
    run_id: str = "run-1",
    evidence_id: str = "run-1-call-1-e1",
    tool_call_id: str = "call-1",
    fingerprint: str | None = None,
    start: str = "2025-12-01",
    end: str = "2025-12-01",
    status: str = "valid",
    result: dict | None = None,
) -> EvidenceRecord:
    payload = result or {
        "metric": "sales_amount",
        "points": [{"period": "2025-12-01", "value": "1200.00"}],
        "explain": {"source_table": "sales_fact"},
    }
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        evidence_type="metric_value",
        domain="Sales",
        metric_code="sales_amount",
        metric_definition_version="2.0.0",
        time_range_start=start,
        time_range_end=end,
        grain="month",
        filters=[{"dimension": "region", "operator": "eq", "value": "EC"}],
        dimensions={"region": "EC"},
        query_parameters={"metric": "sales_amount", "start": start, "end": end},
        result=payload,
        result_summary="华东销售额 1200.00",
        unit="CNY",
        source_tables=["sales_fact"],
        calculation_method="deterministic metric aggregation",
        status=status,
        fingerprint=fingerprint
        or hashlib.sha256(json.dumps([start, end]).encode()).hexdigest(),
        created_at=datetime.now(timezone.utc),
        traceable=True,
    )


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'run_store.db'}", future=True)
    yield eng
    eng.dispose()


def test_schema_creation_is_idempotent_and_does_not_touch_other_tables(engine):
    create_schema(engine)
    create_schema(engine)
    names = set(inspect(engine).get_table_names())
    assert {"agent_evidence", "agent_findings", "agent_reports"} <= names
    # Business tables are never created by the Phase 5 schema helper.
    assert "sales_fact" not in names


def test_record_roundtrip_preserves_all_mandated_fields(engine):
    ledger = EvidenceLedger(engine)
    record = _record()
    stored, inserted = ledger.record(record)

    assert inserted is True
    assert stored.evidence_id == record.evidence_id
    assert stored.run_id == record.run_id
    assert stored.tool_call_id == record.tool_call_id
    assert stored.metric_definition_version == "2.0.0"
    assert stored.time_range_start == "2025-12-01"
    assert stored.filters == record.filters
    assert stored.query_parameters == record.query_parameters
    assert stored.result == record.result, "raw tool output must be stored verbatim"
    assert stored.status == "valid"
    assert stored.traceable is True

    fetched = ledger.get(record.run_id, record.evidence_id)
    assert fetched is not None
    assert fetched.result == record.result
    assert ledger.count(record.run_id) == 1
    assert [item.evidence_id for item in ledger.list(record.run_id)] == [
        record.evidence_id
    ]


def test_same_fingerprint_is_idempotent_within_a_run(engine):
    ledger = EvidenceLedger(engine)
    first, inserted_first = ledger.record(_record())
    second, inserted_second = ledger.record(_record(evidence_id="duplicate-id"))

    assert inserted_first is True
    assert inserted_second is False
    assert second.evidence_id == first.evidence_id
    assert ledger.count("run-1") == 1


def test_same_fingerprint_in_another_run_is_distinct(engine):
    ledger = EvidenceLedger(engine)
    ledger.record(_record(run_id="run-1", evidence_id="e-1", fingerprint="fp"))
    _, inserted = ledger.record(
        _record(run_id="run-2", evidence_id="e-2", fingerprint="fp")
    )
    assert inserted is True
    assert ledger.count("run-2") == 1


def test_different_time_range_or_filters_are_not_merged(engine):
    ledger = EvidenceLedger(engine)
    base = _record(fingerprint="fp-a")
    other_month = _record(
        evidence_id="e-2",
        fingerprint="fp-b",
        start="2025-11-01",
        end="2025-11-01",
    )
    other_filters = _record(evidence_id="e-3", fingerprint="fp-c")
    other_filters.filters = [
        {"dimension": "region", "operator": "eq", "value": "NC"}
    ]
    ledger.record_many([base, other_month, other_filters])
    assert ledger.count("run-1") == 3


def test_concurrent_duplicate_writes_leave_one_row(engine):
    create_schema(engine)
    ledger = EvidenceLedger(engine, auto_create=False)
    errors: list[BaseException] = []

    def worker():
        try:
            EvidenceLedger(engine, auto_create=False).record(_record())
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert ledger.count("run-1") == 1


def test_failed_evidence_keeps_error_and_is_not_traceable(engine):
    ledger = EvidenceLedger(engine)
    failed = _record(status="failed", fingerprint="fp-failed")
    failed.error_code = "timeout"
    failed.error_message = "查询超时"
    failed.traceable = False
    ledger.record(failed)
    fetched = ledger.get("run-1", failed.evidence_id)
    assert fetched.status == "failed"
    assert fetched.error_code == "timeout"
    assert fetched.error_message == "查询超时"
    assert fetched.traceable is False
    assert fetched.is_valid is False


def test_query_blocks_are_isolated_between_runs(engine):
    ledger = EvidenceLedger(engine)
    ledger.record(_record(run_id="run-a", evidence_id="e-a", fingerprint="fp-a"))
    ledger.record(_record(run_id="run-b", evidence_id="e-b", fingerprint="fp-b"))
    assert [item.evidence_id for item in ledger.list("run-a")] == ["e-a"]
    assert ledger.get("run-a", "e-b") is None


def test_drop_schema_removes_only_phase5_tables(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'both.db'}", future=True)
    with eng.begin() as connection:
        connection.execute(text("CREATE TABLE business_keep (id INTEGER)"))
    create_schema(eng)
    drop_schema(eng)
    names = set(inspect(eng).get_table_names())
    assert "business_keep" in names
    assert not ({"agent_evidence", "agent_findings", "agent_reports"} & names)
    eng.dispose()


def test_metadata_table_names_are_stable():
    assert {
        EVIDENCE_TABLE.name,
        FINDING_TABLE.name,
        REPORT_TABLE.name,
    } == {"agent_evidence", "agent_findings", "agent_reports"}
    assert EVIDENCE_METADATA.tables[EVIDENCE_TABLE.name] is EVIDENCE_TABLE
