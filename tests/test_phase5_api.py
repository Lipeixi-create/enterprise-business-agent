from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from tests.agent_fakes import ScriptedLLM, finish_response, tool_response


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
            "viewer-key": "viewer",
        },
        allow_anonymous_api=False,
        phase5_narrative_enabled=False,
    )
    db_path = tmp_path / "api.db"
    write_engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()

    engine = make_readonly_engine(f"sqlite:///{db_path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    run_store = PersistentRunStore(f"sqlite:///{tmp_path / 'agent_runs.db'}")
    try:
        yield {
            "settings": settings,
            "factory": factory,
            "engine": engine,
            "run_store": run_store,
        }
    finally:
        run_store.close()
        engine.dispose()


@contextmanager
def _client(env, llm, *, overrides: dict | None = None):
    settings = env["settings"].model_copy(update=overrides or {})
    app = create_app(
        settings,
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


AUTH = {"X-API-Key": "agent-key"}


def _run(client: TestClient, *, question: str = "分析 2025 年 12 月销售额变化") -> dict:
    response = client.post(
        "/api/v1/agent/runs", headers=AUTH, json={"question": question}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_run_evidence_findings_and_report_are_queryable(env):
    with _client(env, _investigation_llm()) as client:
        run = _run(client)
        run_id = run["run_id"]
        assert run["report_id"]
        assert run["report_version"] == 1
        assert run["report_status"] == "validated"
        assert run["report_validation_passed"] is True
        assert run["finding_ids"]

        evidence = client.get(
            f"/api/v1/agent/runs/{run_id}/evidence", headers=AUTH
        )
        assert evidence.status_code == 200
        body = evidence.json()
        assert body["count"] == 2
        assert {item["evidence_type"] for item in body["evidence"]} == {
            "comparison",
            "contribution",
        }
        assert all(item["status"] == "valid" for item in body["evidence"])
        assert all(item["tool_call_id"] for item in body["evidence"])

        evidence_id = body["evidence"][0]["evidence_id"]
        single = client.get(
            f"/api/v1/agent/runs/{run_id}/evidence/{evidence_id}", headers=AUTH
        )
        assert single.status_code == 200
        assert single.json()["evidence_id"] == evidence_id
        assert single.json()["result"]

        findings = client.get(
            f"/api/v1/agent/runs/{run_id}/findings", headers=AUTH
        )
        assert findings.status_code == 200
        assert findings.json()["count"] >= 2
        assert all(item["evidence_ids"] for item in findings.json()["findings"])

        report = client.get(f"/api/v1/agent/runs/{run_id}/report", headers=AUTH)
        assert report.status_code == 200
        document = report.json()
        assert document["run_id"] == run_id
        assert document["version"] == 1
        assert document["status"] == "validated"
        assert document["report"]["report_info"]["run_id"] == run_id
        assert document["report"]["evidence_references"]

        versions = client.get(
            f"/api/v1/agent/runs/{run_id}/report/versions", headers=AUTH
        )
        assert versions.status_code == 200
        assert versions.json()["count"] == 1
        assert versions.json()["versions"][0]["validation_passed"] is True

        validation = client.get(
            f"/api/v1/agent/runs/{run_id}/report/validation", headers=AUTH
        )
        assert validation.status_code == 200
        assert validation.json()["validation"]["passed"] is True


def test_unknown_run_and_evidence_return_unified_errors(env):
    with _client(env, _investigation_llm()) as client:
        run_id = _run(client)["run_id"]

        missing_run = client.get(
            "/api/v1/agent/runs/does-not-exist/evidence", headers=AUTH
        )
        assert missing_run.status_code == 404
        assert missing_run.json()["error"]["code"] == "agent_run_not_found"

        missing_evidence = client.get(
            f"/api/v1/agent/runs/{run_id}/evidence/does-not-exist", headers=AUTH
        )
        assert missing_evidence.status_code == 404
        assert missing_evidence.json()["error"]["code"] == "evidence_not_found"

        missing_version = client.get(
            f"/api/v1/agent/runs/{run_id}/report?version=99", headers=AUTH
        )
        assert missing_version.status_code == 404
        assert missing_version.json()["error"]["code"] == "report_not_found"


def test_runs_are_isolated_by_owner(env):
    with _client(env, _investigation_llm()) as client:
        run_id = _run(client)["run_id"]
        other = client.get(
            f"/api/v1/agent/runs/{run_id}/evidence",
            headers={"X-API-Key": "other-key"},
        )
        assert other.status_code == 404
        assert other.json()["error"]["code"] == "agent_run_not_found"


def test_viewer_role_cannot_read_evidence(env):
    with _client(env, _investigation_llm()) as client:
        run_id = _run(client)["run_id"]
        response = client.get(
            f"/api/v1/agent/runs/{run_id}/evidence",
            headers={"X-API-Key": "viewer-key"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"


def test_phase5_endpoints_report_unavailable_when_disabled(env):
    with _client(env, ScriptedLLM([finish_response("没有查询数据。")]), overrides={"phase5_enabled": False}) as client:
        response = client.post(
            "/api/v1/agent/runs", headers=AUTH, json={"question": "测试"}
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        assert response.json()["report_id"] is None

        evidence = client.get(
            f"/api/v1/agent/runs/{run_id}/evidence", headers=AUTH
        )
        assert evidence.status_code == 503
        assert evidence.json()["error"]["code"] == "evidence_store_unavailable"


def test_async_run_evidence_uses_the_run_manager_id(env):
    with _client(env, _investigation_llm()) as client:
        created = client.post(
            "/api/v1/agent/runs/async", headers=AUTH, json={"question": "异步证据链"}
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]

        document = None
        for _ in range(100):
            response = client.get(f"/api/v1/agent/runs/{run_id}", headers=AUTH)
            assert response.status_code == 200
            document = response.json()
            if document["status"] in {
                "completed",
                "completed_with_warnings",
                "no_evidence",
                "no_data",
                "failed",
                "cancelled",
                "interrupted",
                "timeout",
                "llm_unavailable",
                "invalid_action",
                "tool_failure",
                "budget_exhausted",
                "repeated_tool_call",
            }:
                break
            time.sleep(0.05)
        assert document is not None and document["status"] != "running"

        evidence = client.get(
            f"/api/v1/agent/runs/{run_id}/evidence", headers=AUTH
        )
        assert evidence.status_code == 200
        assert evidence.json()["count"] == 2
        assert {item["run_id"] for item in evidence.json()["evidence"]} == {run_id}

        report = client.get(f"/api/v1/agent/runs/{run_id}/report", headers=AUTH)
        assert report.status_code == 200
        assert report.json()["run_id"] == run_id
