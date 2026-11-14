"""PostgreSQL integration tests for the Batch 2 runtime.

These tests only run when ``EBA_TEST_POSTGRES_URL`` points at a disposable
PostgreSQL test database. They use real PostgreSQL tables; nothing here is a
mock or a SQLite substitute. The optional ``EBA_TEST_POSTGRES_RUNTIME_URL``
points at the least-privilege runtime role and enables the grant tests.
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from enterprise_business_agent.agent.checkpoint import SqlAlchemyCheckpointSaver
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.api.runtime_schema import (
    create_runtime_schema,
    drop_runtime_schema,
    grant_runtime_role,
)
from enterprise_business_agent.evidence.approval import ApprovalStore, report_fingerprint
from enterprise_business_agent.evidence.contracts import (
    DataScope,
    Recommendation,
    ReportDocument,
    ReportInfo,
    ValidationResult,
)
from enterprise_business_agent.evidence.report import ReportStore

URL = os.getenv("EBA_TEST_POSTGRES_URL")
RUNTIME_URL = os.getenv("EBA_TEST_POSTGRES_RUNTIME_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="EBA_TEST_POSTGRES_URL is not set"),
]


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(URL, future=True)
    drop_runtime_schema(eng)
    create_runtime_schema(eng)
    if RUNTIME_URL:
        runtime_role = create_engine(RUNTIME_URL, future=True).url.username
        grant_runtime_role(eng, runtime_role)
    yield eng
    drop_runtime_schema(eng)
    eng.dispose()


def test_postgres_runtime_schema_contains_all_batch2_tables(engine):
    from sqlalchemy import inspect

    names = set(inspect(engine).get_table_names())
    assert {
        "agent_runs",
        "agent_audit_events",
        "agent_tool_results",
        "agent_evidence",
        "agent_findings",
        "agent_reports",
        "agent_checkpoints",
        "agent_checkpoint_writes",
        "agent_report_approvals",
        "agent_report_approval_events",
    } <= names


def test_postgres_run_store_survives_reopen(engine):
    url = engine.url.render_as_string(hide_password=False)
    first = PersistentRunStore(url)
    run_id = "pg-batch2-run-1"
    first.create(
        run_id=run_id,
        request_id="pg-batch2-request-1",
        owner="key:pg-owner",
        status="running",
        question="PostgreSQL 运行持久化",
        payload={"run_id": run_id, "status": "running"},
    )
    first.put_tool_result(
        run_id=run_id,
        call_key="a" * 64,
        tool_name="compare_periods",
        call_id="call-1",
        status="ok",
        result={"status": "ok", "summary": "销售额 1000.00"},
    )
    first.close()

    second = PersistentRunStore(url)
    try:
        assert second.get(run_id)["status"] == "running"
        assert second.owner_of(run_id) == "key:pg-owner"
        cached = second.get_tool_result(run_id, "a" * 64)
        assert cached is not None and cached["summary"] == "销售额 1000.00"
        assert second.count_tool_results(run_id) == 1
        second.update_status(run_id, "completed", owner="key:pg-owner")
    finally:
        second.close()

    third = PersistentRunStore(url)
    try:
        assert third.get_status(run_id) == "completed"
    finally:
        third.close()


class _GraphState(TypedDict):
    value: int
    stage: str


def test_postgres_checkpoint_resumes_after_a_crash(engine):
    fail = {"enabled": True}
    executed: list[str] = []

    def first(state: _GraphState) -> dict[str, Any]:
        executed.append("first")
        return {"value": state["value"] + 1, "stage": "first"}

    def second(state: _GraphState) -> dict[str, Any]:
        executed.append("second")
        if fail["enabled"]:
            raise RuntimeError("simulated process crash before second node")
        return {"stage": "second"}

    def build(saver: SqlAlchemyCheckpointSaver):
        builder = StateGraph(_GraphState)
        builder.add_node("first", first)
        builder.add_node("second", second)
        builder.add_edge(START, "first")
        builder.add_edge("first", "second")
        builder.add_edge("second", END)
        return builder.compile(checkpointer=saver)

    config = {"configurable": {"thread_id": "pg-checkpoint-run", "checkpoint_ns": ""}}
    saver = SqlAlchemyCheckpointSaver(engine)
    graph = build(saver)
    saver.delete_thread("pg-checkpoint-run")

    with pytest.raises(RuntimeError):
        graph.invoke({"value": 1, "stage": "start"}, config=config)
    assert "first" in executed
    assert saver.get_tuple(config) is not None

    # A fresh saver and graph simulate a new process reading the same table.
    fail["enabled"] = False
    resumed_saver = SqlAlchemyCheckpointSaver(engine)
    resumed_graph = build(resumed_saver)
    final = resumed_graph.invoke(None, config=config)
    assert final["value"] == 2
    assert final["stage"] == "second"
    assert executed.count("first") == 1


def test_postgres_approval_state_machine(engine):
    reports = ReportStore(engine)
    approvals = ApprovalStore(engine)
    report = ReportDocument(
        report_info=ReportInfo(
            report_id="pg-approval-report",
            run_id="pg-approval-run",
            version=1,
            generated_at="2026-10-31T00:00:00+00:00",
            question="PostgreSQL 审批测试",
            principal_id="key:owner",
            role="analyst",
        ),
        objective="PostgreSQL 审批测试",
        recommendations=[
            Recommendation(
                action="调整资源投放",
                rationale="需要业务复核",
                expected_direction="下一期验证",
                risk="关联不等同于因果",
                validation_metric="sales_amount",
                requires_approval=True,
            )
        ],
        data_scope=DataScope(),
    )
    stored = reports.save(report, ValidationResult(passed=True, checks=[]))
    record = approvals.ensure_for_report(stored, requested_by="key:owner")
    assert record.state == "pending"

    updated, duplicate = approvals.decide(
        report_id=record.report_id,
        version=record.report_version,
        action="approve",
        actor="key:approver",
        current_fingerprint=report_fingerprint(stored.report),
        comment="PostgreSQL 审批通过",
    )
    assert duplicate is False
    assert updated.state == "approved"
    assert updated.decided_by == "key:approver"
    events = approvals.events(record.approval_id)
    assert [item.to_state for item in events] == ["pending", "approved"]


@pytest.mark.skipif(
    not RUNTIME_URL, reason="EBA_TEST_POSTGRES_RUNTIME_URL is not set"
)
def test_runtime_role_can_dml_but_not_ddl_or_superuser(engine):
    runtime_engine = create_engine(RUNTIME_URL, future=True)
    role = runtime_engine.url.username
    try:
        with runtime_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO agent_runs "
                    "(run_id, request_id, owner, status, question, created_at, "
                    " updated_at, payload) "
                    "VALUES ('pg-runtime-role-run', 'req', 'key:owner', 'queued', "
                    "'role test', '2026-10-31T00:00:00+00:00', "
                    "'2026-10-31T00:00:00+00:00', '{}')"
                )
            )
            connection.execute(
                text(
                    "UPDATE agent_runs SET status = 'completed' "
                    "WHERE run_id = 'pg-runtime-role-run'"
                )
            )
            value = connection.execute(
                text(
                    "SELECT status FROM agent_runs "
                    "WHERE run_id = 'pg-runtime-role-run'"
                )
            ).scalar_one()
            assert value == "completed"
            connection.execute(
                text("DELETE FROM agent_runs WHERE run_id = 'pg-runtime-role-run'")
            )

            attrs = connection.execute(
                text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            assert tuple(attrs) == (False, False, False)

            can_create_schema = connection.execute(
                text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
            ).scalar_one()
            assert can_create_schema is False

        with engine.begin() as admin_connection:
            admin_connection.execute(
                text("CREATE TABLE eba_business_probe (id int)")
            )
        try:
            with pytest.raises(DBAPIError):
                with runtime_engine.connect() as connection:
                    connection.execute(text("SELECT * FROM eba_business_probe"))
        finally:
            with engine.begin() as admin_connection:
                admin_connection.execute(text("DROP TABLE eba_business_probe"))

        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(text("CREATE TABLE eba_forbidden_ddl (id int)"))
        with pytest.raises(DBAPIError):
            with runtime_engine.begin() as connection:
                connection.execute(text("DROP TABLE agent_runs"))
    finally:
        runtime_engine.dispose()
