"""SQLite tests for transactional run cleanup, retention and orphan detection.

These use real SQLite tables. The PostgreSQL suite repeats the same behaviour
against a real PostgreSQL database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from enterprise_business_agent.agent.checkpoint import CHECKPOINT_METADATA
from enterprise_business_agent.api.run_store import (
    RUN_STORE_METADATA,
    PersistentRunStore,
)
from enterprise_business_agent.api.runtime_schema import create_runtime_schema
from enterprise_business_agent.evidence.approval import APPROVAL_METADATA
from enterprise_business_agent.evidence.schema import EVIDENCE_METADATA

RUNS = RUN_STORE_METADATA.tables["agent_runs"]
EVENTS = RUN_STORE_METADATA.tables["agent_audit_events"]
TOOL_RESULTS = RUN_STORE_METADATA.tables["agent_tool_results"]
EVIDENCE = EVIDENCE_METADATA.tables["agent_evidence"]
FINDINGS = EVIDENCE_METADATA.tables["agent_findings"]
REPORTS = EVIDENCE_METADATA.tables["agent_reports"]
APPROVALS = APPROVAL_METADATA.tables["agent_report_approvals"]
APPROVAL_EVENTS = APPROVAL_METADATA.tables["agent_report_approval_events"]
CHECKPOINTS = CHECKPOINT_METADATA.tables["agent_checkpoints"]
CHECKPOINT_WRITES = CHECKPOINT_METADATA.tables["agent_checkpoint_writes"]


def _store(tmp_path) -> PersistentRunStore:
    store = PersistentRunStore(f"sqlite:///{tmp_path / 'cleanup.db'}")
    create_runtime_schema(store.engine)
    return store


def _run(
    store: PersistentRunStore,
    run_id: str,
    *,
    status: str = "completed",
    days_old: float = 10,
) -> None:
    store.create(
        run_id=run_id,
        request_id=f"req-{run_id}",
        owner="key:owner",
        status=status,
        question="清理测试",
        payload={"run_id": run_id, "status": status},
    )
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat(timespec="seconds")
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_runs SET created_at = :stamp, updated_at = :stamp WHERE run_id = :run_id"),
            {"stamp": stamp, "run_id": run_id},
        )


def _children(engine, run_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report_id = f"report-{run_id}"
    approval_id = f"approval-{run_id}"
    with engine.begin() as connection:
        connection.execute(
            EVENTS.insert().values(
                event_id=f"event-{run_id}",
                run_id=run_id,
                request_id="req",
                principal_id="key:owner",
                event_type="run_finished",
                tool_call_id=None,
                detail="{}",
                created_at=now,
            )
        )
        connection.execute(
            TOOL_RESULTS.insert().values(
                run_id=run_id,
                call_key="a" * 64,
                tool_name="query_metric",
                call_id="call-1",
                status="ok",
                result_json="{}",
                created_at=now,
            )
        )
        connection.execute(
            EVIDENCE.insert().values(
                evidence_id=f"evidence-{run_id}",
                run_id=run_id,
                tool_call_id="call-1",
                evidence_type="metric",
                filters="[]",
                dimensions="[]",
                query_parameters="{}",
                result="{}",
                source_tables="[]",
                status="ok",
                traceable=True,
                fingerprint=(run_id + "0" * 64)[:64],
                created_at=now,
            )
        )
        connection.execute(
            FINDINGS.insert().values(
                finding_id=f"finding-{run_id}",
                run_id=run_id,
                title="测试",
                finding_type="observation",
                severity="low",
                statement="测试",
                evidence_ids="[]",
                confidence=0.5,
                confidence_basis="test",
                limitations="[]",
                suggested_validation="[]",
                created_at=now,
            )
        )
        connection.execute(
            REPORTS.insert().values(
                report_id=report_id,
                run_id=run_id,
                report_version=1,
                status="validated",
                report_json="{}",
                validation_json="{}",
                created_at=now,
            )
        )
        connection.execute(
            APPROVALS.insert().values(
                approval_id=approval_id,
                run_id=run_id,
                report_id=report_id,
                report_version=1,
                report_fingerprint="b" * 64,
                state="pending",
                requires_approval=True,
                requested_by="key:owner",
                requested_at=now,
                decided_by=None,
                decided_at=None,
                comment=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            APPROVAL_EVENTS.insert().values(
                event_id=f"approval-event-{run_id}",
                approval_id=approval_id,
                run_id=run_id,
                report_id=report_id,
                report_version=1,
                from_state=None,
                to_state="pending",
                actor="key:owner",
                comment=None,
                created_at=now,
            )
        )
        connection.execute(
            CHECKPOINTS.insert().values(
                thread_id=run_id,
                checkpoint_ns="",
                checkpoint_id=f"ckpt-{run_id}",
                parent_checkpoint_id=None,
                checkpoint_type="msgpack",
                checkpoint_value=b"{}",
                metadata_type="msgpack",
                metadata_value=b"{}",
                created_at=now,
            )
        )
        connection.execute(
            CHECKPOINT_WRITES.insert().values(
                thread_id=run_id,
                checkpoint_ns="",
                checkpoint_id=f"ckpt-{run_id}",
                task_id="task-1",
                idx=0,
                channel="state",
                value_type="msgpack",
                value=b"{}",
                task_path="",
                created_at=now,
            )
        )


def _count(engine, table, key: str, run_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(select(func.count()).select_from(table).where(table.c[key] == run_id)).scalar_one()
        )


def test_cleanup_deletes_the_run_and_every_child_table(tmp_path):
    store = _store(tmp_path)
    try:
        _run(store, "run-clean", status="completed", days_old=10)
        _children(store.engine, "run-clean")

        report = store.cleanup_runs(
            dry_run=False,
            retention_seconds={
                "completed": 0,
                "failed": 60 * 86400,
                "cancelled": 60 * 86400,
                "unfinished": 60 * 86400,
            },
        )

        assert report["status"] == "ok", report
        assert report["run_count"] == 1
        assert report["deleted"]["agent_runs"] == 1
        for name in (
            "agent_audit_events",
            "agent_tool_results",
            "agent_evidence",
            "agent_findings",
            "agent_reports",
            "agent_report_approvals",
            "agent_report_approval_events",
            "agent_checkpoints",
            "agent_checkpoint_writes",
        ):
            assert report["deleted"][name] == 1, name
        assert store.get("run-clean") is None
        for table, key in (
            (EVENTS, "run_id"),
            (TOOL_RESULTS, "run_id"),
            (EVIDENCE, "run_id"),
            (FINDINGS, "run_id"),
            (REPORTS, "run_id"),
            (APPROVALS, "run_id"),
            (APPROVAL_EVENTS, "run_id"),
            (CHECKPOINTS, "thread_id"),
            (CHECKPOINT_WRITES, "thread_id"),
        ):
            assert _count(store.engine, table, key, "run-clean") == 0, table.name

        with store.engine.connect() as connection:
            event = connection.execute(select(EVENTS.c.detail).where(EVENTS.c.event_type == "run_cleanup")).first()
        assert event is not None
        assert '"run_count": 1' in event.detail
    finally:
        store.close()


def test_cleanup_dry_run_counts_without_deleting(tmp_path):
    store = _store(tmp_path)
    try:
        _run(store, "run-dry", status="completed", days_old=10)
        _children(store.engine, "run-dry")
        report = store.cleanup_runs(
            dry_run=True,
            retention_seconds={
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "unfinished": 0,
            },
        )
        assert report["dry_run"] is True
        assert report["run_count"] == 1
        assert report["deleted"]["agent_runs"] == 1
        assert report["deleted"]["agent_checkpoints"] == 1
        assert store.get("run-dry") is not None
        assert _count(store.engine, EVIDENCE, "run_id", "run-dry") == 1
        with store.engine.connect() as connection:
            audit = connection.execute(
                select(func.count()).select_from(EVENTS).where(EVENTS.c.event_type == "run_cleanup")
            ).scalar_one()
        assert int(audit) == 0
    finally:
        store.close()


def test_cleanup_protects_a_valid_lease_and_deletes_after_release(tmp_path):
    store = _store(tmp_path)
    try:
        _run(store, "run-active", status="running", days_old=100)
        claim = store.acquire_claim("run-active", "worker-live", 120, allowed_statuses={"running"}).claim
        assert claim is not None
        report = store.cleanup_runs(
            dry_run=False,
            retention_seconds={
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "unfinished": 0,
            },
        )
        assert report["run_count"] == 0
        assert store.get("run-active") is not None

        store.release_claim(claim)
        report = store.cleanup_runs(
            dry_run=False,
            retention_seconds={
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "unfinished": 0,
            },
        )
        assert report["run_count"] == 1
        assert store.get("run-active") is None
    finally:
        store.close()


def test_cleanup_uses_status_specific_ttl(tmp_path):
    store = _store(tmp_path)
    try:
        _run(store, "run-completed", status="completed", days_old=10)
        _run(store, "run-failed", status="failed", days_old=10)
        _run(store, "run-cancelled", status="cancelled", days_old=10)
        report = store.cleanup_runs(
            dry_run=False,
            retention_seconds={
                "completed": 30 * 86400,
                "failed": 7 * 86400,
                "cancelled": 30 * 86400,
                "unfinished": 30 * 86400,
            },
        )
        assert report["run_ids"] == ["run-failed"]
        assert store.get("run-failed") is None
        assert store.get("run-completed") is not None
        assert store.get("run-cancelled") is not None
    finally:
        store.close()


def test_cleanup_rolls_back_every_delete_when_one_statement_fails(tmp_path):
    store = _store(tmp_path)
    try:
        _run(store, "run-rollback", status="completed", days_old=10)
        _children(store.engine, "run-rollback")
        with store.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TRIGGER block_report_delete BEFORE DELETE ON agent_reports "
                    "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
                )
            )
        report = store.cleanup_runs(
            dry_run=False,
            retention_seconds={
                "completed": 0,
                "failed": 0,
                "cancelled": 0,
                "unfinished": 0,
            },
        )
        assert report["status"] == "failed"
        assert report["failure_reason"] == "IntegrityError"
        assert store.get("run-rollback") is not None
        assert _count(store.engine, APPROVALS, "run_id", "run-rollback") == 1
        assert _count(store.engine, REPORTS, "run_id", "run-rollback") == 1
    finally:
        store.close()


def test_orphan_detection_reports_children_without_parents(tmp_path):
    store = _store(tmp_path)
    try:
        _run(store, "run-good", status="completed", days_old=1)
        _children(store.engine, "run-good")
        with store.engine.begin() as connection:
            connection.execute(
                EVENTS.insert().values(
                    event_id="orphan-event",
                    run_id="run-missing",
                    request_id="req",
                    principal_id="key:owner",
                    event_type="run_finished",
                    tool_call_id=None,
                    detail="{}",
                    created_at="2026-10-31T00:00:00+00:00",
                )
            )
            connection.execute(
                APPROVALS.insert().values(
                    approval_id="orphan-approval",
                    run_id="run-good",
                    report_id="missing-report",
                    report_version=1,
                    report_fingerprint="c" * 64,
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
        report = store.detect_orphans()
        assert report["counts"]["agent_audit_events_missing_run"] == 1
        assert report["counts"]["agent_report_approvals_missing_report"] == 1
        assert report["total"] >= 2
        assert "run-missing" in report["samples"]["agent_audit_events_missing_run"]
    finally:
        store.close()
