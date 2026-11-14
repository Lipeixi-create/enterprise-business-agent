"""Approval workbench API tests: scope isolation, filters and pagination."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.evidence.approval import ApprovalStore, report_fingerprint
from enterprise_business_agent.evidence.contracts import (
    DataScope,
    Recommendation,
    ReportDocument,
    ReportInfo,
    ValidationResult,
)
from enterprise_business_agent.evidence.report import ReportStore
from enterprise_business_agent.security.auth import authenticate


@pytest.fixture()
def env(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=10,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        api_keys={
            "agent-key": "analyst",
            "other-key": "analyst",
            "approver-key": "approver",
            "manager-key": "business_manager",
            "admin-key": "admin",
            "viewer-key": "viewer",
        },
        allow_anonymous_api=False,
        phase5_narrative_enabled=False,
    )
    db_path = tmp_path / "workbench-data.db"
    write_engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()
    engine = make_readonly_engine(f"sqlite:///{db_path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    run_store = PersistentRunStore(f"sqlite:///{tmp_path / 'workbench-runs.db'}")
    yield {
        "settings": settings,
        "factory": factory,
        "engine": engine,
        "run_store": run_store,
    }
    run_store.close()
    engine.dispose()


@contextmanager
def _client(env):
    app = create_app(
        env["settings"],
        session_factory=env["factory"],
        engine=env["engine"],
        llm_client=None,
        result_store=ResultStore(),
        run_store=env["run_store"],
    )
    with TestClient(app) as client:
        yield client


def _approval(
    env,
    *,
    index: int,
    run_id: str,
    requested_by: str,
    decision: str | None = None,
    decided_by: str | None = None,
) -> None:
    reports = ReportStore(env["run_store"].engine)
    approvals = ApprovalStore(env["run_store"].engine)
    report = ReportDocument(
        report_info=ReportInfo(
            report_id=f"report-{index}",
            run_id=run_id,
            version=1,
            generated_at="2026-10-31T00:00:00+00:00",
            question="审批工作台测试",
            principal_id=requested_by,
            role="analyst",
        ),
        objective="审批工作台测试",
        recommendations=[
            Recommendation(
                action="调整资源投放",
                rationale="高风险建议需要人工复核",
                expected_direction="下一期验证",
                risk="关联不等同于因果",
                validation_metric="sales_amount",
                requires_approval=True,
            )
        ],
        data_scope=DataScope(),
    )
    stored = reports.save(report, ValidationResult(passed=True, checks=[]))
    record = approvals.ensure_for_report(stored, requested_by=requested_by)
    if decision is not None:
        approvals.decide(
            report_id=record.report_id,
            version=record.report_version,
            action=decision,
            actor=decided_by or "key:approver",
            current_fingerprint=report_fingerprint(stored.report),
        )


def test_workbench_scope_isolation_and_filters(env):
    settings = env["settings"]
    analyst = authenticate(settings, "agent-key").principal_id
    approver = authenticate(settings, "approver-key").principal_id
    _approval(
        env,
        index=1,
        run_id="run-a",
        requested_by=analyst,
        decision="approve",
        decided_by=approver,
    )
    _approval(env, index=2, run_id="run-b", requested_by="key:owner-b")
    _approval(env, index=3, run_id="run-c", requested_by="key:owner-c")

    with _client(env) as client:
        analyst_view = client.get("/api/v1/agent/approvals", headers={"X-API-Key": "agent-key"})
        assert analyst_view.status_code == 200, analyst_view.text
        body = analyst_view.json()
        assert body["scope"] == "self"
        assert body["total"] == 1
        assert body["items"][0]["run_id"] == "run-a"

        approver_view = client.get("/api/v1/agent/approvals", headers={"X-API-Key": "approver-key"})
        assert approver_view.status_code == 200, approver_view.text
        assert approver_view.json()["total"] == 1
        assert approver_view.json()["items"][0]["run_id"] == "run-a"

        manager_view = client.get("/api/v1/agent/approvals", headers={"X-API-Key": "manager-key"})
        assert manager_view.status_code == 200, manager_view.text
        manager_body = manager_view.json()
        assert manager_body["scope"] == "all"
        assert manager_body["total"] == 3

        approved = client.get(
            "/api/v1/agent/approvals",
            headers={"X-API-Key": "manager-key"},
            params={"state": "approved"},
        ).json()
        assert approved["total"] == 1
        assert approved["items"][0]["state"] == "approved"

        pending = client.get(
            "/api/v1/agent/approvals",
            headers={"X-API-Key": "manager-key"},
            params=[("state", "pending")],
        ).json()
        assert pending["total"] == 2

        by_approver = client.get(
            "/api/v1/agent/approvals",
            headers={"X-API-Key": "manager-key"},
            params={"approver": approver},
        ).json()
        assert by_approver["total"] == 1
        assert by_approver["items"][0]["run_id"] == "run-a"

        by_requester = client.get(
            "/api/v1/agent/approvals",
            headers={"X-API-Key": "manager-key"},
            params={"requester": "key:owner-b"},
        ).json()
        assert by_requester["total"] == 1
        assert by_requester["items"][0]["run_id"] == "run-b"


def test_workbench_pagination_sorting_and_time_filters(env):
    for index in range(1, 6):
        _approval(
            env,
            index=index,
            run_id=f"run-bulk-{index}",
            requested_by="key:bulk-owner",
        )
    with env["run_store"].engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE agent_report_approvals SET created_at = '2025-01-01T00:00:00+00:00' WHERE run_id = 'run-bulk-1'"
            )
        )

    with _client(env) as client:
        first = client.get(
            "/api/v1/agent/approvals",
            headers={"X-API-Key": "manager-key"},
            params={"page": 1, "page_size": 2, "sort_by": "created_at", "sort_dir": "asc"},
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["total"] == 5
        assert body["pages"] == 3
        assert len(body["items"]) == 2
        assert body["items"][0]["run_id"] == "run-bulk-1"

        last = client.get(
            "/api/v1/agent/approvals",
            headers={"X-API-Key": "manager-key"},
            params={"page": 3, "page_size": 2, "sort_by": "created_at", "sort_dir": "asc"},
        ).json()
        assert len(last["items"]) == 1

        old = client.get(
            "/api/v1/agent/approvals",
            headers={"X-API-Key": "manager-key"},
            params={"created_to": "2025-06-01T00:00:00Z"},
        ).json()
        assert old["total"] == 1
        assert old["items"][0]["run_id"] == "run-bulk-1"

        recent = client.get(
            "/api/v1/agent/approvals",
            headers={"X-API-Key": "manager-key"},
            params={"created_from": "2026-01-01T00:00:00Z"},
        ).json()
        assert recent["total"] == 4


def test_workbench_requires_approval_read_scope(env):
    with _client(env) as client:
        response = client.get("/api/v1/agent/approvals", headers={"X-API-Key": "viewer-key"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
