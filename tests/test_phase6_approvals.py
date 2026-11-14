"""Approval state-machine and API isolation tests (SQLite only)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.evidence.approval import (
    ApprovalNotFound,
    ApprovalSeparationViolation,
    ApprovalStale,
    ApprovalStore,
    InvalidApprovalTransition,
    report_fingerprint,
)
from enterprise_business_agent.evidence.contracts import (
    DataScope,
    Recommendation,
    ReportDocument,
    ReportInfo,
    ValidationResult,
)
from enterprise_business_agent.evidence.report import ReportStore
from tests.agent_fakes import ScriptedLLM, finish_response, tool_response

OWNER = "key:owner"
APPROVER = "key:approver"


@pytest.fixture()
def approval_env():
    engine = create_engine("sqlite://", future=True)
    report_store = ReportStore(engine)
    approvals = ApprovalStore(engine)
    yield engine, report_store, approvals
    engine.dispose()


def _stored(
    report_store: ReportStore,
    *,
    report_id: str = "rep-1",
    run_id: str = "run-1",
    version: int = 1,
    high_risk: bool = True,
    passed: bool = True,
):
    recommendations = (
        [
            Recommendation(
                action="调整区域资源投放",
                rationale="贡献度最高区域需要业务复核",
                expected_direction="下一期结构验证",
                risk="关联不等同于因果",
                validation_metric="sales_amount",
                requires_approval=True,
            )
        ]
        if high_risk
        else []
    )
    report = ReportDocument(
        report_info=ReportInfo(
            report_id=report_id,
            run_id=run_id,
            version=version,
            generated_at="2026-10-31T00:00:00+00:00",
            question="分析销售额",
            principal_id=OWNER,
            role="analyst",
        ),
        objective="分析销售额",
        recommendations=recommendations,
        data_scope=DataScope(),
    )
    return report_store.save(report, ValidationResult(passed=passed, checks=[]))


def test_high_risk_report_starts_pending_and_approves(approval_env):
    _, report_store, approvals = approval_env
    stored = _stored(report_store)

    record = approvals.ensure_for_report(stored, requested_by=OWNER)
    assert record.state == "pending"
    assert record.requires_approval is True
    assert record.report_fingerprint == report_fingerprint(stored.report)
    assert record.decided_by is None

    updated, duplicate = approvals.decide(
        report_id=stored.report_id,
        version=stored.version,
        action="approve",
        actor=APPROVER,
        current_fingerprint=report_fingerprint(stored.report),
        comment="同意，按流程复核后执行",
    )
    assert duplicate is False
    assert updated.state == "approved"
    assert updated.decided_by == APPROVER
    assert updated.comment == "同意，按流程复核后执行"

    same, duplicate_again = approvals.decide(
        report_id=stored.report_id,
        version=stored.version,
        action="approve",
        actor=APPROVER,
        current_fingerprint=report_fingerprint(stored.report),
    )
    assert duplicate_again is True
    assert same.state == "approved"

    events = approvals.events(record.approval_id)
    assert [event.to_state for event in events] == ["pending", "approved"]
    assert events[0].from_state is None
    assert events[1].from_state == "pending"
    assert events[1].actor == APPROVER


def test_owner_cannot_approve_or_reject_own_report(approval_env):
    _, report_store, approvals = approval_env
    stored = _stored(report_store)
    approvals.ensure_for_report(stored, requested_by=OWNER)

    for action in ("approve", "reject"):
        with pytest.raises(ApprovalSeparationViolation):
            approvals.decide(
                report_id=stored.report_id,
                version=stored.version,
                action=action,
                actor=OWNER,
                current_fingerprint=report_fingerprint(stored.report),
            )

    # The owner may still withdraw their own request.
    cancelled, _ = approvals.decide(
        report_id=stored.report_id,
        version=stored.version,
        action="cancel",
        actor=OWNER,
        current_fingerprint=report_fingerprint(stored.report),
    )
    assert cancelled.state == "cancelled"


def test_terminal_state_rejects_a_different_decision_and_other_actor(approval_env):
    _, report_store, approvals = approval_env
    stored = _stored(report_store)
    approvals.ensure_for_report(stored, requested_by=OWNER)
    fingerprint = report_fingerprint(stored.report)

    rejected, _ = approvals.decide(
        report_id=stored.report_id,
        version=stored.version,
        action="reject",
        actor=APPROVER,
        current_fingerprint=fingerprint,
    )
    assert rejected.state == "rejected"

    with pytest.raises(InvalidApprovalTransition):
        approvals.decide(
            report_id=stored.report_id,
            version=stored.version,
            action="approve",
            actor=APPROVER,
            current_fingerprint=fingerprint,
        )
    with pytest.raises(InvalidApprovalTransition):
        approvals.decide(
            report_id=stored.report_id,
            version=stored.version,
            action="reject",
            actor="key:second-approver",
            current_fingerprint=fingerprint,
        )


def test_new_report_version_does_not_inherit_old_approval(approval_env):
    _, report_store, approvals = approval_env
    v1 = _stored(report_store, version=1)
    first = approvals.ensure_for_report(v1, requested_by=OWNER)
    approved, _ = approvals.decide(
        report_id=v1.report_id,
        version=v1.version,
        action="approve",
        actor=APPROVER,
        current_fingerprint=report_fingerprint(v1.report),
    )
    assert approved.state == "approved"

    v2 = _stored(report_store, report_id="rep-2", version=2)
    second = approvals.ensure_for_report(v2, requested_by=OWNER)
    assert second.report_id != first.report_id
    assert second.report_version == first.report_version + 1
    assert second.state == "pending"
    assert second.report_fingerprint != first.report_fingerprint
    assert approvals.get(v1.report_id, 1).state == "approved"
    assert approvals.get(v2.report_id, 2).state == "pending"
    assert approvals.latest_for_run(v1.run_id).report_version == 2

    # A decision aimed at the old version cannot be justified with the new
    # report content, and an unapproved new version stays unapproved.
    with pytest.raises(ApprovalStale):
        approvals.decide(
            report_id=v1.report_id,
            version=1,
            action="approve",
            actor=APPROVER,
            current_fingerprint=report_fingerprint(v2.report),
        )
    assert approvals.get(v2.report_id, 2).state == "pending"


def test_low_risk_or_rejected_report_cannot_be_approved(approval_env):
    _, report_store, approvals = approval_env
    low_risk = _stored(report_store, report_id="rep-low", version=1, high_risk=False)
    record = approvals.ensure_for_report(low_risk, requested_by=OWNER)
    assert record.state == "not_required"
    assert record.requires_approval is False

    with pytest.raises(InvalidApprovalTransition):
        approvals.decide(
            report_id=low_risk.report_id,
            version=1,
            action="approve",
            actor=APPROVER,
            current_fingerprint=report_fingerprint(low_risk.report),
        )

    invalid = _stored(
        report_store,
        report_id="rep-invalid",
        run_id="run-2",
        version=1,
        passed=False,
    )
    invalid_record = approvals.ensure_for_report(invalid, requested_by=OWNER)
    assert invalid_record.state == "not_required"
    assert invalid_record.requires_approval is False


def test_missing_approval_is_reported(approval_env):
    _, _, approvals = approval_env
    with pytest.raises(ApprovalNotFound):
        approvals.decide(
            report_id="missing",
            version=1,
            action="approve",
            actor=APPROVER,
            current_fingerprint="0" * 64,
        )


# --- API-level tests -------------------------------------------------------


@pytest.fixture()
def api_env(tmp_path):
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
            "viewer-key": "viewer",
            "manager-key": "business_manager",
        },
        allow_anonymous_api=False,
        phase5_narrative_enabled=False,
    )
    db_path = tmp_path / "approval-api.db"
    write_engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()
    engine = make_readonly_engine(f"sqlite:///{db_path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    run_store = PersistentRunStore(f"sqlite:///{tmp_path / 'runs.db'}")
    yield {"settings": settings, "factory": factory, "engine": engine, "run_store": run_store}
    run_store.close()
    engine.dispose()


@contextmanager
def _client(env, llm):
    app = create_app(
        env["settings"],
        session_factory=env["factory"],
        engine=env["engine"],
        llm_client=llm,
        result_store=ResultStore(),
        run_store=env["run_store"],
    )
    with TestClient(app) as client:
        yield client


def _investigation_llm() -> ScriptedLLM:
    return ScriptedLLM(
        [
            tool_response(
                "c1",
                "compare_periods",
                {"metric": "sales_amount", "kind": "mom", "period": "2025-12-01"},
            ),
            tool_response(
                "c2",
                "contribution",
                {
                    "metric": "sales_amount",
                    "dimension": "region",
                    "current_start": "2025-12-01",
                    "current_end": "2025-12-01",
                    "baseline_start": "2025-11-01",
                    "baseline_end": "2025-11-01",
                },
            ),
            finish_response("已完成分析。"),
        ]
    )


ANALYST_AUTH = {"X-API-Key": "agent-key"}
OTHER_AUTH = {"X-API-Key": "other-key"}
APPROVER_AUTH = {"X-API-Key": "approver-key"}
VIEWER_AUTH = {"X-API-Key": "viewer-key"}
MANAGER_AUTH = {"X-API-Key": "manager-key"}


def _run(client: TestClient, headers: dict | None = None) -> dict:
    response = client.post(
        "/api/v1/agent/runs",
        headers=headers or ANALYST_AUTH,
        json={"question": "分析 2025 年 12 月销售额变化"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_api_approval_requires_separation_and_fingerprint(api_env):
    with _client(api_env, _investigation_llm()) as client:
        run = _run(client)
        run_id = run["run_id"]
        assert run["approval_state"] == "pending"
        assert run["approval_required"] is True
        assert run["approval_id"]

        current = client.get(
            f"/api/v1/agent/runs/{run_id}/report/approval",
            headers=ANALYST_AUTH,
        )
        assert current.status_code == 200
        approval = current.json()
        assert approval["state"] == "pending"
        assert approval["stale"] is False

        # A decision must bind both the exact version and the content
        # fingerprint; the server does not guess the latest version.
        unbound = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=APPROVER_AUTH,
            json={"action": "approve"},
        )
        assert unbound.status_code == 422
        assert unbound.json()["error"]["code"] == "invalid_request"

        # An analyst lacks the decision scope altogether.
        owner_decision = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=ANALYST_AUTH,
            json={
                "action": "approve",
                "report_version": 1,
                "report_fingerprint": approval["report_fingerprint"],
            },
        )
        assert owner_decision.status_code == 403
        assert owner_decision.json()["error"]["code"] == "forbidden"

        # Another analyst is outside the run's owner boundary.
        other_read = client.get(
            f"/api/v1/agent/runs/{run_id}/report/approval",
            headers=OTHER_AUTH,
        )
        assert other_read.status_code == 404
        assert other_read.json()["error"]["code"] == "agent_run_not_found"

        # A viewer lacks the report scope.
        viewer_read = client.get(
            f"/api/v1/agent/runs/{run_id}/report/approval",
            headers=VIEWER_AUTH,
        )
        assert viewer_read.status_code == 403
        assert viewer_read.json()["error"]["code"] == "forbidden"

        # A caller-supplied fingerprint that does not match the report is stale.
        stale = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=APPROVER_AUTH,
            json={
                "action": "approve",
                "report_version": 1,
                "report_fingerprint": "0" * 64,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "approval_stale"

        approved = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=APPROVER_AUTH,
            json={
                "action": "approve",
                "report_version": 1,
                "report_fingerprint": approval["report_fingerprint"],
                "comment": "复核后同意",
            },
        )
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["state"] == "approved"
        assert body["duplicate"] is False
        assert body["decided_by"]

        duplicate = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=APPROVER_AUTH,
            json={
                "action": "approve",
                "report_version": 1,
                "report_fingerprint": approval["report_fingerprint"],
            },
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True

        rejected = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=APPROVER_AUTH,
            json={
                "action": "reject",
                "report_version": 1,
                "report_fingerprint": approval["report_fingerprint"],
            },
        )
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "invalid_approval_transition"

        events = client.get(
            f"/api/v1/agent/runs/{run_id}/report/approval/events",
            headers=ANALYST_AUTH,
        )
        assert events.status_code == 200
        assert [item["to_state"] for item in events.json()["events"]] == [
            "pending",
            "approved",
        ]

    # Audit events were written to the durable run store.
    audit = api_env["run_store"].list_events(run_id)
    assert any(item["event_type"] == "approval_approve" for item in audit)


def test_api_manager_with_decide_scope_still_cannot_approve_own_report(api_env):
    with _client(api_env, _investigation_llm()) as client:
        run = _run(client, MANAGER_AUTH)
        run_id = run["run_id"]
        approval = client.get(
            f"/api/v1/agent/runs/{run_id}/report/approval",
            headers=MANAGER_AUTH,
        ).json()

        response = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=MANAGER_AUTH,
            json={
                "action": "approve",
                "report_version": 1,
                "report_fingerprint": approval["report_fingerprint"],
            },
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "approval_separation_violation"
        assert (
            client.get(
                f"/api/v1/agent/runs/{run_id}/report/approval",
                headers=MANAGER_AUTH,
            ).json()["state"]
            == "pending"
        )


def test_api_owner_can_cancel_own_pending_approval(api_env):
    with _client(api_env, _investigation_llm()) as client:
        run_id = _run(client)["run_id"]
        approval = client.get(
            f"/api/v1/agent/runs/{run_id}/report/approval",
            headers=ANALYST_AUTH,
        ).json()

        cancelled = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=ANALYST_AUTH,
            json={
                "action": "cancel",
                "report_version": 1,
                "report_fingerprint": approval["report_fingerprint"],
                "comment": "需求已撤销",
            },
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["state"] == "cancelled"

        approve_after_cancel = client.post(
            f"/api/v1/agent/runs/{run_id}/report/approval/decision",
            headers=APPROVER_AUTH,
            json={
                "action": "approve",
                "report_version": 1,
                "report_fingerprint": approval["report_fingerprint"],
            },
        )
        assert approve_after_cancel.status_code == 409
        assert approve_after_cancel.json()["error"]["code"] == ("invalid_approval_transition")
