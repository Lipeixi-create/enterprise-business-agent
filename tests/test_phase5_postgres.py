from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect

from enterprise_business_agent.api.run_store import create_run_store_schema
from enterprise_business_agent.evidence import (
    EvidenceLedger,
    EvidenceRecord,
    FindingEngine,
    FindingStore,
    ReportGenerator,
    ReportStore,
    create_schema,
    drop_schema,
)

URL = os.getenv("EBA_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="EBA_TEST_POSTGRES_URL is not set"),
]


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL, future=True)
    drop_schema(eng)
    create_schema(eng)
    create_run_store_schema(eng)
    yield eng
    drop_schema(eng)
    eng.dispose()


def _record(run_id: str) -> EvidenceRecord:
    from datetime import datetime, timezone

    import hashlib

    return EvidenceRecord(
        evidence_id=f"{run_id}-e1",
        run_id=run_id,
        tool_call_id=f"{run_id}-c1",
        evidence_type="comparison",
        domain="Sales",
        metric_code="sales_amount",
        metric_definition_version="2.0.0",
        time_range_start="2025-12-01",
        time_range_end="2025-12-01",
        grain="month",
        filters=[{"dimension": "region", "operator": "eq", "value": "EC"}],
        dimensions={"region": "EC"},
        query_parameters={"metric": "sales_amount"},
        result={
            "metric": "sales_amount",
            "label": "销售额",
            "unit": "CNY",
            "items": [
                {
                    "current_period": "2025-12-01",
                    "baseline_period": "2025-11-01",
                    "current_value": "1000.00",
                    "baseline_value": "1250.00",
                    "absolute_change": "-250.00",
                    "change_rate": "-0.2",
                }
            ],
            "explain": {"source_table": "sales_fact"},
        },
        result_summary="销售额 1000.00",
        unit="CNY",
        source_tables=["sales_fact"],
        status="valid",
        traceable=True,
        fingerprint=hashlib.sha256(run_id.encode()).hexdigest(),
        created_at=datetime.now(timezone.utc),
    )


def test_postgres_schema_is_created_and_idempotent(engine):
    create_schema(engine)
    names = set(inspect(engine).get_table_names())
    assert {"agent_evidence", "agent_findings", "agent_reports"} <= names


def test_postgres_evidence_roundtrip_and_dedupe(engine):
    ledger = EvidenceLedger(engine)
    run_id = "pg-run-1"
    stored, inserted = ledger.record(_record(run_id))
    duplicate, inserted_again = ledger.record(_record(run_id))

    assert inserted is True
    assert inserted_again is False
    assert duplicate.evidence_id == stored.evidence_id
    assert ledger.count(run_id) == 1

    fetched = ledger.get(run_id, stored.evidence_id)
    assert fetched is not None
    assert fetched.result == stored.result
    assert fetched.metric_definition_version == "2.0.0"


def test_postgres_findings_and_report_persist(engine):
    ledger = EvidenceLedger(engine)
    run_id = "pg-run-2"
    evidence = ledger.record(_record(run_id))[0]
    findings = FindingEngine().build(run_id, [evidence])
    stored_findings = FindingStore(engine).save_many(findings)
    FindingStore(engine).save_many(findings)
    assert len(FindingStore(engine).list(run_id)) == len(stored_findings)

    reports = ReportStore(engine)
    generator = ReportGenerator(reports, narrative_enabled=False)
    first = generator.generate(
        run_id=run_id,
        question="PostgreSQL 证据链测试",
        principal_id="key:test",
        role="analyst",
        evidences=[evidence],
        findings=stored_findings,
    )
    second = generator.generate(
        run_id=run_id,
        question="PostgreSQL 证据链测试",
        principal_id="key:test",
        role="analyst",
        evidences=[evidence],
        findings=stored_findings,
    )
    assert first.status == "validated"
    assert second.version == 2
    assert [item.version for item in reports.versions(run_id)] == [1, 2]


def test_postgres_run_store_roundtrip(engine):
    from enterprise_business_agent.api.run_store import PersistentRunStore

    store = PersistentRunStore(
        engine.url.render_as_string(hide_password=False)
    )
    try:
        run_id = "pg-run-store-1"
        store.create(
            run_id=run_id,
            request_id="pg-request-1",
            owner="key:pg-owner",
            status="queued",
            question="PostgreSQL run store 测试",
            payload={"run_id": run_id, "status": "queued"},
        )
        assert store.get(run_id)["run_id"] == run_id
        assert store.owner_of(run_id) == "key:pg-owner"
        assert store.get(run_id, owner="key:other") is None

        event_id = store.append_event(
            event_type="run_created",
            run_id=run_id,
            principal_id="key:pg-owner",
            detail={"source": "test"},
        )
        assert [item["event_id"] for item in store.list_events(run_id)] == [event_id]
        assert store.list_events(run_id, owner="key:other") == []
        assert store.update_status(run_id, "completed", owner="key:pg-owner") is True
        assert store.get_status(run_id) == "completed"
    finally:
        store.close()

