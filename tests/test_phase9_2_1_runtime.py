"""Phase 9.2.1 run-level reliability tests.

The LLM client tests prove the socket layer. These tests prove that a stalled
or cancelled provider call actually ends the Agent Run with the right status,
releases the request thread, stops retries, and cannot overwrite a run that a
newer worker has taken over. The provider is a real loopback HTTP server; no
external model is contacted.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import AgentRuntime, AnalysisGateway, ToolRegistry
from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.llm import GLMClient, LLMCancelled, LLMTimeout
from enterprise_business_agent.llm.transport import active_request_threads
from enterprise_business_agent.security import AuthorizationPolicy, Principal
from tests.agent_fakes import ScriptedLLM, finish_response
from tests.test_phase9_2_1_reliability import _Provider, _ok_body

ANALYST = Principal("key:test", "analyst", frozenset({"metrics:read", "agent:run"}))
AUTH = {"X-API-Key": "agent-key"}
TERMINAL = {
    "completed",
    "completed_with_warnings",
    "no_evidence",
    "no_data",
    "failed",
    "cancelled",
    "interrupted",
    "timeout",
    "llm_unavailable",
    "llm_timeout",
    "invalid_action",
    "repeated_tool_call",
    "tool_failure",
    "budget_exhausted",
}


@pytest.fixture(autouse=True)
def _bypass_system_proxy(monkeypatch):
    # The host's system proxy swallows loopback HTTP otherwise.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture()
def provider():
    instance = _Provider()
    try:
        yield instance
    finally:
        instance.close()


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
        run_lease_seconds=1.0,
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


def _glm_settings(base_url: str, **overrides) -> Settings:
    values = {
        "llm_provider": "glm",
        "llm_model": "glm-5.3-flash",
        "llm_base_url": base_url,
        "llm_api_key": "test-key",
        "llm_timeout_seconds": 30.0,
        "llm_connect_timeout_seconds": 1.0,
        "llm_read_timeout_seconds": 30.0,
        "llm_max_retries": 3,
        "llm_retry_backoff_base_seconds": 0.01,
        "llm_retry_backoff_max_seconds": 0.02,
        "llm_circuit_failure_threshold": 0,
    }
    values.update(overrides)
    return Settings(**values)


def _runtime(env, llm) -> AgentRuntime:
    return AgentRuntime(
        llm_client=llm,
        registry=ToolRegistry(
            AnalysisGateway(env["factory"], env["settings"]),
            AuthorizationPolicy(env["settings"]),
            env["settings"],
        ),
        settings=env["settings"],
    )


def _app(env, llm):
    return create_app(
        env["settings"],
        session_factory=env["factory"],
        engine=env["engine"],
        llm_client=llm,
        result_store=ResultStore(),
        run_store=env["run_store"],
        agent_runtime=_runtime(env, llm),
    )


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _wait_terminal(client: TestClient, run_id: str) -> dict:
    for _ in range(300):
        document = client.get(f"/api/v1/agent/runs/{run_id}", headers=AUTH).json()
        if document["status"] in TERMINAL:
            return document
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached a terminal status")


def _blocked_provider(provider: _Provider) -> threading.Event:
    release = threading.Event()
    provider.responder = lambda count, handler: (
        release.wait(10.0),
        handler.write_json(200, _ok_body()),
    )
    return release


def test_cancelled_model_call_ends_the_run_as_cancelled(env):
    llm = ScriptedLLM([LLMCancelled("owner cancelled during the model call")])
    result = _runtime(env, llm).run("销售额是多少", ANALYST, request_id="req-cancel")

    assert result.status.value == "cancelled"
    assert result.stop_reason == "cancelled_by_user"
    assert len(llm.calls) == 1


def test_run_hard_deadline_stop_is_not_retried(env):
    deadline_error = LLMTimeout("run deadline reached", kind="timeout")
    deadline_error.task_deadline = True
    llm = ScriptedLLM([deadline_error])
    result = _runtime(env, llm).run("销售额是多少", ANALYST, request_id="req-deadline")

    assert result.status.value == "timeout"
    assert result.stop_reason == "time_budget_exhausted"
    assert result.state.budget_stop_reason == "deadline_exceeded"
    assert [error["code"] for error in result.errors] == ["time_budget_exhausted"]
    assert len(llm.calls) == 1


def test_unreachable_provider_ends_the_run_with_bounded_retries(env):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    client = GLMClient(
        _glm_settings(
            f"http://127.0.0.1:{port}/api/paas/v4",
            llm_max_retries=1,
            llm_read_timeout_seconds=1.0,
        )
    )
    started = time.monotonic()
    result = _runtime(env, client).run(
        "销售额是多少", ANALYST, request_id="req-unreachable"
    )
    elapsed = time.monotonic() - started

    assert result.status.value == "llm_unavailable"
    assert elapsed < 5.0


def test_cancel_interrupts_a_blocked_provider_call(env, provider):
    release = _blocked_provider(provider)
    app = _app(env, GLMClient(_glm_settings(provider.base_url)))
    try:
        with TestClient(app) as client:
            submitted = client.post(
                "/api/v1/agent/runs/async",
                headers=AUTH,
                json={"question": "取消阻塞调用"},
            )
            run_id = submitted.json()["run_id"]
            assert _wait(lambda: provider.requests == 1)

            started = time.monotonic()
            cancelled = client.post(
                f"/api/v1/agent/runs/{run_id}/cancel", headers=AUTH
            )
            assert cancelled.status_code == 200, cancelled.text
            document = _wait_terminal(client, run_id)
            elapsed = time.monotonic() - started

            assert document["status"] == "cancelled"
            assert document["stop_reason"] == "cancelled_by_user"
            assert elapsed < 3.0
            # Cancellation must stop the retry ladder, not just this attempt.
            assert provider.requests == 1
            assert _wait(lambda: active_request_threads() == 0, timeout=2.0)

            events = client.get(
                f"/api/v1/agent/runs/{run_id}/events", headers=AUTH
            ).json()["events"]
            finished = [
                event
                for event in events
                if event["event_type"] == "run_finished"
                and "worker_id" in (event.get("detail") or {})
            ]
            assert len(finished) == 1
            assert finished[0]["detail"]["status"] == "cancelled"
    finally:
        release.set()


def test_shutdown_interrupts_an_in_flight_run(env, provider):
    release = _blocked_provider(provider)
    app = _app(env, GLMClient(_glm_settings(provider.base_url)))
    try:
        with TestClient(app) as client:
            submitted = client.post(
                "/api/v1/agent/runs/async",
                headers=AUTH,
                json={"question": "停机中断"},
            )
            run_id = submitted.json()["run_id"]
            assert _wait(lambda: provider.requests == 1)

            app.state.run_manager.shutdown()
            document = _wait_terminal(client, run_id)
            assert document["status"] == "cancelled"
            assert document["stop_reason"] == "cancelled_by_user"
            assert _wait(lambda: active_request_threads() == 0, timeout=2.0)
    finally:
        release.set()


def test_taken_over_lease_blocks_the_stale_result(env, provider):
    release = _blocked_provider(provider)
    app = _app(env, GLMClient(_glm_settings(provider.base_url)))
    manager = app.state.run_manager
    store = env["run_store"]
    try:
        payload, created = manager.submit(
            question="租约被接管",
            principal=ANALYST,
            request_id="req-lease",
            max_tool_calls=None,
            max_seconds=10,
        )
        assert created
        run_id = payload["run_id"]
        assert _wait(lambda: provider.requests == 1)

        with store.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE agent_runs SET lease_expires_at = "
                    "'2000-01-01T00:00:00+00:00' WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            )
        attempt = store.acquire_claim(
            run_id, "other-worker", 30.0, allowed_statuses={"running"}
        )
        assert attempt.claim is not None
        new_claim = attempt.claim

        # The heartbeat notices the lost lease and cancels the stale attempt;
        # the stale worker then must fail its fenced write.
        assert _wait(
            lambda: run_id not in manager._cancel_events, timeout=8.0
        ), "the stale worker never finished"
        assert _wait(lambda: active_request_threads() == 0, timeout=2.0)

        document = store.get(run_id, owner=ANALYST.principal_id)
        assert document["status"] == "running"
        events = store.list_events(run_id, owner=ANALYST.principal_id)
        assert not [e for e in events if e["event_type"] == "run_finished"]

        with store.engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT claim_owner, claim_version, lease_expires_at "
                    "FROM agent_runs WHERE run_id = :run_id"
                ),
                {"run_id": run_id},
            ).first()
        assert row.claim_owner == "other-worker"
        assert int(row.claim_version) == new_claim.claim_version
        assert row.lease_expires_at is not None
    finally:
        release.set()
        manager.shutdown()


def test_cancel_before_persistence_wins_over_a_complete_answer(env):
    event = threading.Event()

    def answer_then_cancel(_llm):
        event.set()
        return finish_response("销售额同比增长 12.3%。")

    llm = ScriptedLLM([answer_then_cancel])
    result = _runtime(env, llm).run(
        "销售额是多少",
        ANALYST,
        cancel_event=event,
        request_id="req-late-cancel",
    )

    assert result.status.value == "cancelled"
    assert result.stop_reason == "cancelled_by_user"
