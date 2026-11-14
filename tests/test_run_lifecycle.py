from __future__ import annotations

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import AgentRuntime, AnalysisGateway, ToolRegistry
from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.security import AuthorizationPolicy
from tests.agent_fakes import ScriptedLLM, finish_response


@pytest.fixture()
def env(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=10,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        api_keys={"agent-key": "analyst"},
        allow_anonymous_api=False,
    )
    db_path = tmp_path / "runs.db"
    write_engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()

    engine = make_readonly_engine(f"sqlite:///{db_path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    run_store = PersistentRunStore(f"sqlite:///{tmp_path / 'agent_runs.db'}")
    yield {
        "settings": settings,
        "factory": factory,
        "engine": engine,
        "run_store": run_store,
        "tmp_path": tmp_path,
    }
    run_store.close()
    engine.dispose()


def _app(env, llm):
    runtime = AgentRuntime(
        llm_client=llm,
        registry=ToolRegistry(
            AnalysisGateway(env["factory"], env["settings"]),
            AuthorizationPolicy(env["settings"]),
            env["settings"],
        ),
        settings=env["settings"],
    )
    return create_app(
        env["settings"],
        session_factory=env["factory"],
        engine=env["engine"],
        llm_client=llm,
        result_store=ResultStore(),
        run_store=env["run_store"],
        agent_runtime=runtime,
    )


def _wait_terminal(client: TestClient, run_id: str, headers: dict[str, str]) -> dict:
    for _ in range(100):
        response = client.get(f"/api/v1/agent/runs/{run_id}", headers=headers)
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
            return document
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach a terminal status")


def test_persistent_store_survives_reopen(tmp_path):
    url = f"sqlite:///{tmp_path / 'durable.db'}"
    first = PersistentRunStore(url)
    first.put(
        "run-1",
        {"run_id": "run-1", "status": "completed", "question": "q"},
        owner="key:a",
        status="completed",
        idempotency_key="idem-1",
    )
    first.append_event(
        event_type="run_finished",
        run_id="run-1",
        request_id="req-1",
        principal_id="key:a",
        detail={"status": "completed"},
    )
    first.close()

    second = PersistentRunStore(url)
    try:
        document = second.get("run-1", owner="key:a")
        assert document is not None
        assert document["status"] == "completed"
        assert second.get("run-1", owner="key:b") is None
        assert second.find_by_idempotency("key:a", "idem-1")["run_id"] == "run-1"
        events = second.list_events("run-1", owner="key:a")
        assert [event["event_type"] for event in events] == ["run_finished"]
        assert events[0]["principal_id"] == "key:a"
    finally:
        second.close()


def test_async_run_is_idempotent_and_queryable(env):
    llm = ScriptedLLM([finish_response("没有调用数据工具。")])
    app = _app(env, llm)
    headers = {"X-API-Key": "agent-key", "Idempotency-Key": "same-key"}

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/agent/runs/async", headers=headers, json={"question": "测试异步"}
        )
        assert first.status_code == 202
        assert first.json()["status"] in {"queued", "running"}
        run_id = first.json()["run_id"]

        second = client.post(
            "/api/v1/agent/runs/async", headers=headers, json={"question": "测试异步"}
        )
        assert second.status_code == 202
        assert second.json()["run_id"] == run_id
        assert second.headers.get("Idempotency-Replayed") == "true"

        document = _wait_terminal(client, run_id, {"X-API-Key": "agent-key"})
        assert document["status"] == "no_evidence"
        assert len(llm.calls) == 1

        events = client.get(
            f"/api/v1/agent/runs/{run_id}/events", headers={"X-API-Key": "agent-key"}
        )
        assert events.status_code == 200
        event_types = [item["event_type"] for item in events.json()["events"]]
        assert "run_created" in event_types
        assert "decision_started" in event_types
        assert "model_response" in event_types
        assert "run_finished" in event_types


def test_sync_run_writes_audit_and_sse_stream(env):
    llm = ScriptedLLM([finish_response("没有调用数据工具。")])
    app = _app(env, llm)
    auth = {"X-API-Key": "agent-key"}

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/agent/runs", headers=auth, json={"question": "同步测试"}
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]

        events = client.get(
            f"/api/v1/agent/runs/{run_id}/events", headers=auth
        ).json()["events"]
        assert [item["event_type"] for item in events] == ["run_finished"]
        assert events[0]["run_id"] == run_id

        with client.stream(
            "GET", f"/api/v1/agent/runs/{run_id}/stream", headers=auth
        ) as stream:
            assert stream.status_code == 200
            body = "".join(stream.iter_text())
        assert "event: status" in body
        assert "event: done" in body


def test_cancel_is_cooperative_and_persisted(env):
    def slow(_llm):
        time.sleep(0.6)
        return finish_response("迟到的结论。")

    llm = ScriptedLLM([slow])
    app = _app(env, llm)
    auth = {"X-API-Key": "agent-key"}

    with TestClient(app) as client:
        submitted = client.post(
            "/api/v1/agent/runs/async", headers=auth, json={"question": "取消测试"}
        )
        run_id = submitted.json()["run_id"]

        for _ in range(50):
            status = client.get(
                f"/api/v1/agent/runs/{run_id}", headers=auth
            ).json()["status"]
            if status == "running":
                break
            time.sleep(0.02)

        cancelled = client.post(f"/api/v1/agent/runs/{run_id}/cancel", headers=auth)
        assert cancelled.status_code == 200, cancelled.text
        document = _wait_terminal(client, run_id, auth)
        assert document["status"] == "cancelled"
        assert document["stop_reason"] == "cancelled_by_user"

        again = client.post(f"/api/v1/agent/runs/{run_id}/cancel", headers=auth)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "cancel_not_possible"


def test_interrupted_runs_are_marked_after_restart(env):
    store = PersistentRunStore(f"sqlite:///{env['tmp_path'] / 'restart.db'}")
    try:
        store.put(
            "run-x",
            {"run_id": "run-x", "status": "running", "question": "q",
             "stop_reason": None},
            owner="key:a",
            status="running",
        )
        assert store.mark_interrupted() == 1
        document = store.get("run-x", owner="key:a")
        assert document["status"] == "interrupted"
        assert document["stop_reason"] == "process_restart"
    finally:
        store.close()
