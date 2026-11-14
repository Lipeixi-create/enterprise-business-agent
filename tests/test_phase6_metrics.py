"""Metrics endpoint tests against real SQLite runtime tables."""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from enterprise_business_agent.agent.checkpoint import CHECKPOINT_METADATA
from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_metrics import metrics_snapshot
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.api.runtime_schema import create_runtime_schema
from enterprise_business_agent.config import Settings
from enterprise_business_agent.db import make_engine
from enterprise_business_agent.evidence.approval import APPROVAL_METADATA
from enterprise_business_agent.evidence.schema import EVIDENCE_METADATA


@pytest.fixture()
def store(tmp_path):
    run_store = PersistentRunStore(f"sqlite:///{tmp_path / 'metrics.db'}")
    create_runtime_schema(run_store.engine)
    yield run_store
    run_store.close()


def _seed(store: PersistentRunStore) -> None:
    for run_id, status in (
        ("run-ok", "completed"),
        ("run-warn", "completed_with_warnings"),
        ("run-fail", "failed"),
        ("run-running", "running"),
    ):
        store.create(
            run_id=run_id,
            request_id=f"req-{run_id}",
            owner="key:owner",
            status=status,
            question="指标测试",
            payload={
                "run_id": run_id,
                "status": status,
                "llm_tokens": {"input": 100, "output": 50},
            },
        )
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_runs SET execution_seconds = 10.0 WHERE run_id = 'run-ok'"
            )
        )
        connection.execute(
            text(
                "UPDATE agent_runs SET execution_seconds = 30.0, resume_count = 1 "
                "WHERE run_id = 'run-warn'"
            )
        )
        connection.execute(
            text(
                "UPDATE agent_runs SET execution_seconds = 20.0, resume_count = 1 "
                "WHERE run_id = 'run-fail'"
            )
        )
    store.put_tool_result(
        run_id="run-ok",
        call_key="a" * 64,
        tool_name="query_metric",
        call_id="call-1",
        status="ok",
        result={"ok": True},
    )
    store.put_tool_result(
        run_id="run-warn",
        call_key="b" * 64,
        tool_name="query_metric",
        call_id="call-2",
        status="error",
        result={"ok": False},
    )
    store.append_event(event_type="model_response", run_id="run-ok")
    store.append_event(event_type="model_response", run_id="run-warn")
    store.append_event(event_type="llm_retry", run_id="run-warn")
    store.append_event(event_type="llm_error", run_id="run-fail")

    checkpoints = CHECKPOINT_METADATA.tables["agent_checkpoints"]
    with store.engine.begin() as connection:
        connection.execute(
            checkpoints.insert().values(
                thread_id="run-ok",
                checkpoint_ns="",
                checkpoint_id="ckpt-1",
                parent_checkpoint_id=None,
                checkpoint_type="msgpack",
                checkpoint_value=b"x" * 40,
                metadata_type="msgpack",
                metadata_value=b"{}",
                created_at="2026-10-31T00:00:00+00:00",
            )
        )

    evidence = EVIDENCE_METADATA.tables["agent_evidence"]
    findings = EVIDENCE_METADATA.tables["agent_findings"]
    reports = EVIDENCE_METADATA.tables["agent_reports"]
    approvals = APPROVAL_METADATA.tables["agent_report_approvals"]
    with store.engine.begin() as connection:
        connection.execute(
            evidence.insert().values(
                evidence_id="ev-1",
                run_id="run-ok",
                tool_call_id="call-1",
                evidence_type="metric",
                filters="[]",
                dimensions="[]",
                query_parameters="{}",
                result="{}",
                source_tables="[]",
                status="ok",
                traceable=True,
                fingerprint="c" * 64,
                created_at="2026-10-31T00:00:00+00:00",
            )
        )
        connection.execute(
            findings.insert().values(
                finding_id="f-1",
                run_id="run-ok",
                title="测试",
                finding_type="observation",
                severity="low",
                statement="测试",
                evidence_ids="[]",
                confidence=0.6,
                confidence_basis="test",
                limitations="[]",
                suggested_validation="[]",
                created_at="2026-10-31T00:00:00+00:00",
            )
        )
        connection.execute(
            reports.insert().values(
                report_id="report-ok",
                run_id="run-ok",
                report_version=1,
                status="validated",
                report_json="{}",
                validation_json='{"issues": [{"code": "unsourced_number"}], '
                '"unverified_numbers": ["9.9"]}',
                created_at="2026-10-31T00:00:00+00:00",
            )
        )
        connection.execute(
            reports.insert().values(
                report_id="report-bad",
                run_id="run-warn",
                report_version=1,
                status="rejected",
                report_json="{}",
                validation_json='{"issues": [], "unverified_numbers": []}',
                created_at="2026-10-31T00:00:00+00:00",
            )
        )
        connection.execute(
            approvals.insert().values(
                approval_id="ap-1",
                run_id="run-ok",
                report_id="report-ok",
                report_version=1,
                report_fingerprint="d" * 64,
                state="pending",
                requires_approval=True,
                requested_by="key:owner",
                requested_at="2026-10-31T00:00:00+00:00",
                decided_by=None,
                decided_at=None,
                comment=None,
                created_at="2026-10-31T00:00:00+00:00",
                updated_at="2026-10-31T00:00:00+00:00",
            )
        )
        connection.execute(
            approvals.insert().values(
                approval_id="ap-2",
                run_id="run-warn",
                report_id="report-bad",
                report_version=1,
                report_fingerprint="e" * 64,
                state="approved",
                requires_approval=True,
                requested_by="key:owner",
                requested_at="2026-10-30T00:00:00+00:00",
                decided_by="key:approver",
                decided_at="2026-10-31T00:00:00+00:00",
                comment=None,
                created_at="2026-10-30T00:00:00+00:00",
                updated_at="2026-10-31T00:00:00+00:00",
            )
        )


def test_metrics_snapshot_computes_runtime_quality(store):
    _seed(store)
    settings = Settings(data_start=date(2024, 1, 1), data_months=24)
    snapshot = metrics_snapshot(store, settings)

    assert snapshot["scope"] == "shared_database"
    assert snapshot["runs"]["total"] == 4
    assert snapshot["runs"]["completed"] == 2
    assert snapshot["runs"]["failed"] == 1
    assert snapshot["runs"]["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert snapshot["runs"]["failure_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert snapshot["runs"]["avg_execution_seconds"] == pytest.approx(20.0)
    assert snapshot["runs"]["p95_execution_seconds"] == pytest.approx(30.0)
    assert snapshot["runs"]["resumed_runs"] == 2
    assert snapshot["runs"]["resume_count"] == 2
    assert snapshot["runs"]["resume_success_rate"] == pytest.approx(0.5)

    assert snapshot["tools"]["total_calls"] == 2
    assert snapshot["tools"]["failure_rate"] == pytest.approx(0.5)
    assert snapshot["tools"]["avg_calls_per_run"] == pytest.approx(1.0)

    assert snapshot["model"]["calls"] == 2
    assert snapshot["model"]["retries"] == 1
    assert snapshot["model"]["errors"] == 1
    assert snapshot["model"]["total_tokens"] == 600
    assert snapshot["model"]["calls_per_run"] == pytest.approx(1.0)
    assert snapshot["model"]["avg_latency_ms"] is None

    assert snapshot["cost"]["estimated"] is False
    assert snapshot["cost"]["estimated_total"] is None
    assert snapshot["cost"]["price_source"] == "unconfigured"
    assert snapshot["performance"]["model_calls_per_run"]["total"] == 2
    assert snapshot["performance"]["model_calls_per_run"]["max"] == 1
    assert snapshot["performance"]["retries"]["llm"] == 1
    assert snapshot["performance"]["retries"]["tool"] is None
    assert snapshot["performance"]["timeouts"]["runs"] == 0
    assert snapshot["performance"]["by_domain"]["unspecified"]["runs"] == 1
    assert snapshot["performance"]["by_domain"]["unspecified"][
        "avg_execution_seconds"
    ] == pytest.approx(10.0)

    assert snapshot["checkpoints"]["count"] == 1
    assert snapshot["checkpoints"]["storage_bytes"] == 40

    assert snapshot["validation"]["reports"] == 2
    assert snapshot["validation"]["validated"] == 1
    assert snapshot["validation"]["pass_rate"] == pytest.approx(0.5)
    assert snapshot["validation"]["numeric_interceptions"] == 1
    assert snapshot["validation"]["unsourced_numbers"] == 1
    assert snapshot["validation"]["evidence_runs"] == 1
    assert snapshot["validation"]["evidence_coverage"] == pytest.approx(0.5)
    assert snapshot["validation"]["evidence_coverage"] <= 1.0
    assert snapshot["validation"]["findings"] == 1

    assert snapshot["approvals"]["total"] == 2
    assert snapshot["approvals"]["pending"] == 1
    assert snapshot["approvals"]["avg_pending_wait_seconds"] is not None
    assert snapshot["approvals"]["avg_decision_wait_seconds"] == pytest.approx(86400.0)

    assert snapshot["database"]["backend"] == "sqlite"
    assert snapshot["database"]["connection_errors_scope"] == "process_local"
    assert snapshot["observability"]["exported_spans"] == 0
    assert snapshot["observability"]["exporter_configured"] is False
    assert snapshot["observability"]["exported_spans_scope"] == "process_local"


def test_metrics_cost_and_latency_use_configured_prices(store):
    _seed(store)
    payload = json.dumps(
        {
            "run_id": "run-ok",
            "status": "completed",
            "llm_tokens": {"input": 1000, "output": 500},
            "llm_latency_ms": 1200.0,
            "tool_calls": [
                {"tool": "query_metric", "duration_ms": 15.0},
            ],
        }
    )
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_runs SET payload = :payload WHERE run_id = 'run-ok'"),
            {"payload": payload},
        )
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        model_input_price_per_million=1.0,
        model_output_price_per_million=2.0,
        model_price_source="contract-2026",
        model_price_currency="CNY",
    )
    snapshot = metrics_snapshot(store, settings)

    assert snapshot["cost"]["estimated"] is True
    assert snapshot["cost"]["price_source"] == "contract-2026"
    # token 数来自 run store：input 1300 / output 650。
    assert snapshot["cost"]["estimated_total"] == pytest.approx(0.0026)
    assert snapshot["cost"]["estimated_per_completed_run"] == pytest.approx(0.0013)
    assert snapshot["performance"]["avg_model_latency_ms"] == pytest.approx(1200.0)
    assert snapshot["performance"]["p95_model_latency_ms"] == pytest.approx(1200.0)
    assert snapshot["performance"]["avg_tool_duration_ms"] == pytest.approx(15.0)
    assert snapshot["model"]["avg_latency_ms"] == pytest.approx(1200.0)


def test_metrics_endpoint_requires_admin_and_returns_payload(store):
    _seed(store)
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        allow_anonymous_api=False,
        api_keys={"admin-key": "admin", "agent-key": "analyst"},
    )
    engine = make_engine("sqlite://")
    app = create_app(
        settings,
        session_factory=None,
        engine=engine,
        llm_client=None,
        result_store=ResultStore(),
        run_store=store,
    )
    with TestClient(app) as client:
        forbidden = client.get(
            "/api/v1/metrics", headers={"X-API-Key": "agent-key"}
        )
        assert forbidden.status_code == 403
        response = client.get("/api/v1/metrics", headers={"X-API-Key": "admin-key"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["runs"]["total"] == 4
        assert body["cost"]["estimated"] is False
        assert body["performance"]["model_calls_per_run"]["total"] == 2
        assert "by_domain" in body["performance"]
        assert body["observability"]["note"].startswith("EBA_OTEL_ENABLED is false")
    engine.dispose()
