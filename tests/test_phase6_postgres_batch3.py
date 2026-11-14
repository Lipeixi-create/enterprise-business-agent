"""Real PostgreSQL tests for Batch 3 claims, leases, fencing and cleanup.

Set ``EBA_TEST_POSTGRES_URL`` to a disposable PostgreSQL database owned by the
setup role. These tests create and drop the real ``agent_*`` tables; they never
fall back to SQLite. Set ``EBA_TEST_POSTGRES_RUNTIME_URL`` to exercise the
least-privilege runtime role as well.
"""

from __future__ import annotations

import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent.checkpoint import CHECKPOINT_METADATA
from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_store import (
    RUN_STORE_METADATA,
    PersistentRunStore,
    RunClaimLost,
)
from enterprise_business_agent.api.runtime_schema import (
    create_runtime_schema,
    drop_runtime_schema,
    grant_runtime_role,
)
from enterprise_business_agent.config import Settings
from enterprise_business_agent.db import make_engine
from enterprise_business_agent.evidence.approval import APPROVAL_METADATA
from enterprise_business_agent.evidence.schema import EVIDENCE_METADATA
from enterprise_business_agent.security.auth import authenticate
from tests.agent_fakes import ScriptedLLM

URL = os.getenv("EBA_TEST_POSTGRES_URL")
RUNTIME_URL = os.getenv("EBA_TEST_POSTGRES_RUNTIME_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="EBA_TEST_POSTGRES_URL is not set"),
]

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


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL, future=True)
    drop_runtime_schema(eng)
    create_runtime_schema(eng)
    if RUNTIME_URL:
        grant_runtime_role(
            eng, create_engine(RUNTIME_URL, future=True).url.username
        )
    yield eng
    drop_runtime_schema(eng)
    eng.dispose()


@pytest.fixture(autouse=True)
def truncate_runtime_tables(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE agent_checkpoint_writes, agent_checkpoints, "
                "agent_report_approval_events, agent_report_approvals, "
                "agent_reports, agent_findings, agent_evidence, "
                "agent_tool_results, agent_audit_events, agent_runs CASCADE"
            )
        )
    yield


@pytest.fixture()
def store(engine):
    value = PersistentRunStore(engine.url.render_as_string(hide_password=False))
    yield value
    value.close()


def _run(store: PersistentRunStore, run_id: str, status: str = "interrupted") -> None:
    store.create(
        run_id=run_id,
        request_id=f"req-{run_id}",
        owner="key:owner",
        status=status,
        question="PostgreSQL 并发测试",
        payload={"run_id": run_id, "status": status},
    )


def _stamp(days_old: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat(
        timespec="microseconds"
    )


def _expire(store: PersistentRunStore, run_id: str) -> None:
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_runs SET lease_expires_at = :expired "
                "WHERE run_id = :run_id"
            ),
            {"expired": _stamp(1), "run_id": run_id},
        )


def _claim_worker(url: str, run_id: str, barrier, queue) -> None:
    store = PersistentRunStore(url)
    try:
        barrier.wait(timeout=30)
        attempt = store.acquire_claim(
            run_id, "worker", 120, allowed_statuses={"interrupted"}
        )
        queue.put(
            (
                attempt.reason,
                attempt.claim.claim_version if attempt.claim else None,
                attempt.held_by,
            )
        )
    finally:
        store.close()


def test_two_independent_processes_only_one_claim_wins(store):
    _run(store, "pg-claim-race")
    url = store.engine.url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(target=_claim_worker, args=(url, "pg-claim-race", barrier, queue))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=60)
        assert process.exitcode == 0
    results = [queue.get(timeout=5), queue.get(timeout=5)]
    winners = [item for item in results if item[1] is not None]
    losers = [item for item in results if item[0] == "lease_held"]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    assert winners[0][1] == 1
    assert losers[0][2] is not None

    state = store.claim_state("pg-claim-race")
    assert state is not None
    assert state["claim_owner"] == "worker"
    assert state["claim_version"] == 1


def test_live_lease_returns_409_on_the_resume_endpoint(store):
    _run(store, "pg-claim-http")
    claim = store.acquire_claim(
        "pg-claim-http", "worker-live", 120, allowed_statuses={"interrupted"}
    ).claim
    assert claim is not None
    settings = Settings(
        phase5_enabled=False,
        allow_anonymous_api=False,
        api_keys={"agent-key": "analyst"},
        run_store_url="sqlite://",
    )
    business_engine = make_engine("sqlite://")
    factory = sessionmaker(bind=business_engine, class_=Session, expire_on_commit=False)
    app = create_app(
        settings,
        session_factory=factory,
        engine=business_engine,
        llm_client=ScriptedLLM([]),
        result_store=ResultStore(),
        run_store=store,
    )
    owner = authenticate(settings, "agent-key").principal_id
    with store.engine.begin() as connection:
        connection.execute(
            text("UPDATE agent_runs SET owner = :owner WHERE run_id = 'pg-claim-http'"),
            {"owner": owner},
        )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/agent/runs/pg-claim-http/resume",
                headers={"X-API-Key": "agent-key"},
            )
        assert response.status_code == 409, response.text
        payload = response.json()["error"]
        assert payload["code"] == "run_claim_conflict"
        assert payload["detail"]["held_by"] == "worker-live"
    finally:
        store.release_claim(claim)
        business_engine.dispose()


def test_expired_lease_allows_takeover_and_stale_worker_cannot_write(store):
    _run(store, "pg-fencing")
    first = store.acquire_claim(
        "pg-fencing", "worker-old", 120, allowed_statuses={"interrupted"}
    ).claim
    assert first is not None
    _expire(store, "pg-fencing")

    second = store.acquire_claim(
        "pg-fencing", "worker-new", 120, allowed_statuses={"interrupted"}
    ).claim
    assert second is not None
    assert second.claim_version == first.claim_version + 1

    with pytest.raises(RunClaimLost):
        store.put(
            "pg-fencing",
            {"run_id": "pg-fencing", "status": "completed", "winner": "old"},
            fence=first,
        )
    with pytest.raises(RunClaimLost):
        store.update_status("pg-fencing", "completed", fence=first)

    store.put(
        "pg-fencing",
        {"run_id": "pg-fencing", "status": "completed", "winner": "new"},
        status="completed",
        fence=second,
    )
    document = store.get("pg-fencing")
    assert document["winner"] == "new"
    assert document["status"] == "completed"
    assert store.claim_state("pg-fencing")["claim_owner"] == "worker-new"

    assert store.release_claim(second) is True
    state = store.claim_state("pg-fencing")
    assert state["claim_owner"] is None
    assert state["lease_expires_at"] is None


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


def _age(store: PersistentRunStore, run_id: str, days_old: float = 10) -> None:
    value = _stamp(days_old)
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_runs SET created_at = :stamp, updated_at = :stamp "
                "WHERE run_id = :run_id"
            ),
            {"stamp": value, "run_id": run_id},
        )


def _count(engine, table, key: str, run_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                select(func.count()).select_from(table).where(table.c[key] == run_id)
            ).scalar_one()
        )


RETENTION_ZERO = {
    "completed": 0,
    "failed": 0,
    "cancelled": 0,
    "unfinished": 0,
}


def test_postgres_cleanup_deletes_all_ten_objects_in_one_transaction(store):
    _run(store, "pg-clean", status="completed")
    _age(store, "pg-clean")
    _children(store.engine, "pg-clean")
    report = store.cleanup_runs(dry_run=False, retention_seconds=RETENTION_ZERO)
    assert report["status"] == "ok", report
    assert report["run_count"] == 1
    for name in (
        "agent_runs",
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
    assert store.get("pg-clean") is None
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
        assert _count(store.engine, table, key, "pg-clean") == 0, table.name
    with store.engine.connect() as connection:
        detail = connection.execute(
            select(EVENTS.c.detail).where(EVENTS.c.event_type == "run_cleanup")
        ).first()
    assert detail is not None
    assert '"run_count": 1' in detail[0]


def test_postgres_cleanup_dry_run_and_active_lease_protection(store):
    _run(store, "pg-dry", status="completed")
    _age(store, "pg-dry")
    _children(store.engine, "pg-dry")
    _run(store, "pg-live-clean", status="running")
    claim = store.acquire_claim(
        "pg-live-clean", "worker-live", 120, allowed_statuses={"running"}
    ).claim
    assert claim is not None

    dry = store.cleanup_runs(dry_run=True, retention_seconds=RETENTION_ZERO)
    assert dry["run_count"] == 1
    assert dry["run_ids"] == ["pg-dry"]
    assert store.get("pg-dry") is not None
    assert _count(store.engine, EVIDENCE, "run_id", "pg-dry") == 1

    live = store.cleanup_runs(dry_run=False, retention_seconds=RETENTION_ZERO)
    assert live["run_ids"] == ["pg-dry"]
    assert store.get("pg-live-clean") is not None
    store.release_claim(claim)
    after = store.cleanup_runs(dry_run=False, retention_seconds=RETENTION_ZERO)
    assert after["run_ids"] == ["pg-live-clean"]
    assert store.get("pg-live-clean") is None


def test_postgres_cleanup_rolls_back_every_table_on_failure(store):
    _run(store, "pg-rollback", status="completed")
    _age(store, "pg-rollback")
    _children(store.engine, "pg-rollback")
    statements = [
        "CREATE OR REPLACE FUNCTION eba_block_report_delete() RETURNS trigger AS "
        "$$ BEGIN RAISE EXCEPTION 'blocked'; END; $$ LANGUAGE plpgsql",
        "CREATE TRIGGER eba_block_report_delete BEFORE DELETE ON agent_reports "
        "FOR EACH ROW EXECUTE FUNCTION eba_block_report_delete()",
    ]
    with store.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    try:
        report = store.cleanup_runs(dry_run=False, retention_seconds=RETENTION_ZERO)
        assert report["status"] == "failed"
        assert report["failure_reason"]
        assert store.get("pg-rollback") is not None
        assert _count(store.engine, APPROVALS, "run_id", "pg-rollback") == 1
        assert _count(store.engine, REPORTS, "run_id", "pg-rollback") == 1
        assert _count(store.engine, EVIDENCE, "run_id", "pg-rollback") == 1
        assert _count(store.engine, CHECKPOINTS, "thread_id", "pg-rollback") == 1
    finally:
        with store.engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER IF EXISTS eba_block_report_delete ON agent_reports")
            )
            connection.execute(text("DROP FUNCTION IF EXISTS eba_block_report_delete()"))


@pytest.mark.skipif(not RUNTIME_URL, reason="EBA_TEST_POSTGRES_RUNTIME_URL is not set")
def test_postgres_runtime_role_cannot_read_business_or_run_ddl(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE eba_business_probe (id int)"))
    runtime_engine = create_engine(RUNTIME_URL, future=True)
    try:
        with runtime_engine.connect() as connection:
            attrs = connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            assert tuple(attrs) == (False, False, False)
            with pytest.raises(Exception):
                connection.execute(text("SELECT * FROM eba_business_probe LIMIT 1"))
            connection.rollback()
            with pytest.raises(Exception):
                connection.execute(text("CREATE TABLE eba_no_ddl (id int)"))
            connection.rollback()
            connection.execute(text("SELECT count(*) FROM agent_runs")).scalar_one()
    finally:
        runtime_engine.dispose()
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS eba_business_probe"))
            connection.execute(text("DROP TABLE IF EXISTS eba_no_ddl"))
