"""SQLite runtime tests for checkpointing, resume and tool idempotency.

These tests use a real SQLite run store and a real LangGraph checkpointer. The
LLM is the repository's scripted test double, so they prove control-flow
behaviour (crash, resume, authorization re-check) and do not claim real model
quality.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import (
    AgentRuntime,
    AgentStatus,
    AnalysisGateway,
    ToolDefinition,
    ToolRegistry,
)
from enterprise_business_agent.agent.checkpoint import SqlAlchemyCheckpointSaver
from enterprise_business_agent.agent.tools import MetricQueryArgs
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.llm import LLMTimeout
from enterprise_business_agent.security import AuthorizationPolicy, Principal
from tests.agent_fakes import ScriptedLLM, finish_response, tool_response

ANALYST = Principal("key:owner", "analyst", frozenset({"metrics:read", "agent:run"}))
RESTRICTED = Principal(
    "key:restricted",
    "restricted_analyst",
    frozenset({"metrics:read", "agent:run"}),
)

TOP_N_CALL = tool_response(
    "c1",
    "top_n",
    {
        "metric": "sales_amount",
        "dimension": "region",
        "start": "2025-03-01",
        "end": "2025-03-01",
        "n": 2,
    },
)


class RecordingRegistry(ToolRegistry):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.executions: list[str] = []

    def execute(self, name, arguments, principal, *, call_id, timeout_seconds=None):
        self.executions.append(name)
        return super().execute(
            name,
            arguments,
            principal,
            call_id=call_id,
            timeout_seconds=timeout_seconds,
        )


class CrashBeforeToolRegistry(RecordingRegistry):
    def execute(self, name, arguments, principal, *, call_id, timeout_seconds=None):
        self.executions.append(f"crashed:{name}")
        raise RuntimeError("simulated process interruption before tool execution")


@pytest.fixture()
def env(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        agent_max_tool_calls=4,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        agent_tool_max_retries=0,
        agent_max_consecutive_failures=2,
        authz_policy={"restricted_analyst": {"tools": ["compare_periods", "query_metric"]}},
    )
    data_path = tmp_path / "data.db"
    write_engine = make_engine(f"sqlite:///{data_path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()
    read_engine = make_readonly_engine(f"sqlite:///{data_path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=read_engine, class_=Session, expire_on_commit=False)
    run_url = f"sqlite:///{tmp_path / 'runtime.db'}"
    yield SimpleNamespace(
        settings=settings,
        factory=factory,
        read_engine=read_engine,
        run_url=run_url,
        tmp_path=tmp_path,
    )
    read_engine.dispose()


def _registry(env, settings: Settings | None = None, *, crash: bool = False):
    cls = CrashBeforeToolRegistry if crash else RecordingRegistry
    return cls(
        AnalysisGateway(env.factory, env.settings),
        AuthorizationPolicy(settings or env.settings),
        settings or env.settings,
    )


def _runtime(
    env,
    llm,
    store: PersistentRunStore,
    registry: ToolRegistry,
    *,
    settings: Settings | None = None,
) -> AgentRuntime:
    return AgentRuntime(
        llm_client=llm,
        registry=registry,
        settings=settings or env.settings,
        checkpointer=SqlAlchemyCheckpointSaver(store.engine),
        tool_cache=store,
    )


def _open_run_store(url: str) -> PersistentRunStore:
    return PersistentRunStore(url)


def test_checkpoint_and_tool_result_survive_process_restart(env):
    run_id = "run-restart-1"
    first_store = _open_run_store(env.run_url)
    first_registry = _registry(env)
    first = _runtime(
        env,
        ScriptedLLM([TOP_N_CALL, RuntimeError("simulated process crash")]),
        first_store,
        first_registry,
    )

    response = first.run("分析 2025-03-01 各区域销售额", ANALYST, run_id=run_id)

    assert response.status is AgentStatus.FAILED
    assert first_registry.executions == ["top_n"]
    assert first_store.count_tool_results(run_id) == 1
    cached = first_store.get_tool_result(run_id, _call_key(TOP_N_CALL))
    assert cached is not None and cached["status"] == "ok"
    first_store.close()

    # A different process reopens the same durable store and checkpoint table.
    second_store = _open_run_store(env.run_url)
    try:
        saver = SqlAlchemyCheckpointSaver(second_store.engine)
        assert saver.latest_checkpoint_id(run_id) is not None
        assert second_store.count_tool_results(run_id) == 1

        second_registry = _registry(env)
        second = _runtime(
            env,
            ScriptedLLM([finish_response("已基于上次工具结果完成分析。")]),
            second_store,
            second_registry,
        )
        resumed = second.resume(run_id, ANALYST)

        assert resumed.status in {
            AgentStatus.COMPLETED,
            AgentStatus.COMPLETED_WITH_WARNINGS,
        }
        assert resumed.run_id == run_id
        # The completed tool call is replayed from the durable cache, never
        # executed against the data source a second time.
        assert second_registry.executions == []
        assert second_store.count_tool_results(run_id) == 1
        assert resumed.tool_calls and resumed.tool_calls[0].tool == "top_n"
    finally:
        second_store.close()


def test_resume_uses_checkpoint_before_pending_tool(env):
    run_id = "run-permission-recheck"
    first_store = _open_run_store(env.run_url)
    crashed = _runtime(
        env,
        ScriptedLLM([TOP_N_CALL]),
        first_store,
        _registry(env, crash=True),
    )

    response = crashed.run("分析 2025-03-01 各区域销售额", ANALYST, run_id=run_id)
    assert response.status is AgentStatus.FAILED
    first_store.close()

    second_store = _open_run_store(env.run_url)
    try:
        saver = SqlAlchemyCheckpointSaver(second_store.engine)
        saved = saver.get_tuple(_config(run_id))
        assert saved is not None
        assert (saved.checkpoint["channel_values"] or {}).get("pending_action")

        # The pending action was authorized for an analyst. Resume runs under a
        # restricted principal, so the tool must be re-authorized, not replayed.
        restricted = _registry(env)
        second = _runtime(
            env,
            ScriptedLLM([finish_response("权限已变更，无法继续该工具调用。")]),
            second_store,
            restricted,
        )
        resumed = second.resume(run_id, RESTRICTED, request_id="req-resume")

        assert restricted.executions == ["top_n"]
        assert resumed.principal_id == RESTRICTED.principal_id
        assert resumed.role == "restricted_analyst"
        assert resumed.tool_calls
        call = resumed.tool_calls[0]
        assert call.tool == "top_n"
        assert call.status == "permission_denied"
        assert call.error_code == "permission_denied"
        assert second_store.count_tool_results(run_id) == 0
    finally:
        second_store.close()


def test_cached_tool_result_is_not_replayed_for_a_less_privileged_role(env):
    run_id = "run-cache-reauth"
    first_store = _open_run_store(env.run_url)
    crashed = _runtime(
        env,
        ScriptedLLM([TOP_N_CALL]),
        first_store,
        _registry(env, crash=True),
    )
    assert crashed.run("分析 2025-03-01 各区域销售额", ANALYST, run_id=run_id).status is AgentStatus.FAILED

    call = TOP_N_CALL.tool_calls[0]
    call_key = _call_key(TOP_N_CALL)
    first_store.put_tool_result(
        run_id=run_id,
        call_key=call_key,
        tool_name=call.name,
        call_id=call.call_id,
        status="ok",
        result={"status": "ok", "summary": "旧权限下已取得的区域销售额"},
    )
    first_store.close()

    second_store = _open_run_store(env.run_url)
    try:
        restricted = _registry(env)
        second = _runtime(
            env,
            ScriptedLLM([finish_response("当前权限无法复用该结果。")]),
            second_store,
            restricted,
        )
        resumed = second.resume(run_id, RESTRICTED)

        # The result exists in the durable cache, but the resuming role may no
        # longer call this tool, so the cached payload must not be replayed.
        assert restricted.executions == []
        assert resumed.tool_calls
        assert resumed.tool_calls[0].status == "permission_denied"
        assert resumed.tool_calls[0].error_code == "permission_denied"
        assert second_store.count_tool_results(run_id) == 1
    finally:
        second_store.close()


def test_resume_without_checkpoint_is_a_structured_failure(env):
    store = _open_run_store(env.run_url)
    try:
        runtime = _runtime(env, ScriptedLLM([finish_response("不会执行")]), store, _registry(env))
        response = runtime.resume("missing-run", ANALYST)
        assert response.status is AgentStatus.FAILED
        assert response.stop_reason == "checkpoint_not_found"
        assert response.errors[0]["code"] == "checkpoint_not_found"
    finally:
        store.close()


def _config(run_id: str):
    return {"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}


def _call_key(response) -> str:
    from enterprise_business_agent.agent.graph import _tool_call_key

    call = response.tool_calls[0]
    return _tool_call_key(call.name, call.arguments)


def test_database_unavailable_is_retried_within_the_limit(env):
    settings = env.settings.model_copy(update={"agent_tool_max_retries": 1})
    attempts: list[int] = []

    def unavailable(gateway, args):
        attempts.append(1)
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    registry = ToolRegistry(
        AnalysisGateway(env.factory, settings),
        AuthorizationPolicy(settings),
        settings,
        definitions=[
            ToolDefinition(
                name="query_metric",
                description="database outage test tool",
                args_model=MetricQueryArgs,
                handler=unavailable,
            )
        ],
    )
    result = registry.execute(
        "query_metric",
        {"metric": "sales_amount", "start": "2025-01-01", "end": "2025-01-01"},
        ANALYST,
        call_id="db-fail",
    )
    assert result.status == "database_unavailable"
    assert result.error is not None and result.error.retryable is True
    assert len(attempts) == 2
    assert "connection lost" not in result.error.message


def test_cached_tool_result_is_reauthorized_for_current_role(env):
    registry = _registry(env)
    arguments = TOP_N_CALL.tool_calls[0].arguments
    assert registry.authorize_call("top_n", arguments, ANALYST) is None
    denied = registry.authorize_call("top_n", arguments, RESTRICTED)
    assert denied is not None
    assert denied.status == "permission_denied"
    assert denied.error is not None and denied.error.code == "permission_denied"


def test_llm_timeout_is_retried_within_the_limit(env):
    llm = ScriptedLLM(
        [
            TOP_N_CALL,
            LLMTimeout("simulated model timeout"),
            finish_response("超时重试后完成。"),
        ]
    )
    settings = env.settings.model_copy(update={"agent_llm_max_retries": 1})
    store = _open_run_store(env.run_url)
    try:
        runtime = _runtime(env, llm, store, _registry(env), settings=settings)
        response = runtime.run("分析 2025-03-01 各区域销售额", ANALYST)

        assert response.status in {
            AgentStatus.COMPLETED,
            AgentStatus.COMPLETED_WITH_WARNINGS,
        }
        assert response.stop_reason == "model_finished"
        assert len(llm.calls) == 3
    finally:
        store.close()


def test_llm_timeout_stops_after_the_retry_limit(env):
    llm = ScriptedLLM([LLMTimeout("first"), LLMTimeout("second")])
    settings = env.settings.model_copy(update={"agent_llm_max_retries": 1})
    store = _open_run_store(env.run_url)
    try:
        runtime = _runtime(env, llm, store, _registry(env), settings=settings)
        response = runtime.run("分析销售额", ANALYST)

        assert response.status is AgentStatus.LLM_TIMEOUT
        assert response.stop_reason == "llm_timeout"
        assert len(llm.calls) == 2
    finally:
        store.close()


def test_invalid_tool_action_gets_one_controlled_correction(env):
    llm = ScriptedLLM(
        [
            tool_response("bad-1", "drop_database", {}),
            TOP_N_CALL,
            finish_response("已纠正为合法工具并完成分析。"),
        ]
    )
    settings = env.settings.model_copy(update={"agent_invalid_action_max_retries": 1})
    store = _open_run_store(env.run_url)
    try:
        runtime = _runtime(env, llm, store, _registry(env), settings=settings)
        response = runtime.run("分析 2025-03-01 各区域销售额", ANALYST)

        assert response.status in {
            AgentStatus.COMPLETED,
            AgentStatus.COMPLETED_WITH_WARNINGS,
        }
        assert [call.tool for call in response.tool_calls] == [
            "drop_database",
            "top_n",
        ]
        assert response.tool_calls[0].status == "invalid_action"
        assert any("合法动作" in item for item in response.warnings)
    finally:
        store.close()


def test_invalid_tool_action_stops_after_the_correction_limit(env):
    llm = ScriptedLLM(
        [
            tool_response("bad-1", "drop_database", {}),
            tool_response("bad-2", "delete_audit_log", {}),
            finish_response("不会执行到这里。"),
        ]
    )
    settings = env.settings.model_copy(update={"agent_invalid_action_max_retries": 1})
    store = _open_run_store(env.run_url)
    try:
        runtime = _runtime(env, llm, store, _registry(env), settings=settings)
        response = runtime.run("分析销售额", ANALYST)

        assert response.status is AgentStatus.INVALID_ACTION
        assert response.stop_reason == "unknown_tool"
        assert [call.status for call in response.tool_calls] == [
            "invalid_action",
            "invalid_action",
        ]
    finally:
        store.close()


def test_resume_policy_rejects_completed_and_permission_stops():
    from enterprise_business_agent.api.run_manager import _is_resumable

    assert _is_resumable({"status": "failed", "stop_reason": "graph_error"}) == (
        True,
        "",
    )
    assert _is_resumable({"status": "interrupted"}) == (True, "")
    assert _is_resumable({"status": "completed", "stop_reason": None})[0] is False
    assert _is_resumable({"status": "failed", "stop_reason": "permission_denied"})[0] is False
    assert _is_resumable({"status": "budget_exhausted", "stop_reason": "tool_budget_exhausted"})[0] is False
    assert _is_resumable({"status": "cancelled", "stop_reason": "cancelled_by_user"})[0] is False


def test_unavailable_postgres_never_falls_back_to_sqlite(tmp_path):
    with pytest.raises(Exception) as caught:
        PersistentRunStore("postgresql+psycopg://eba_runtime:not-a-real-password@127.0.0.1:1/ebo")
    # An unreachable PostgreSQL must surface the connection error. Creating a
    # local SQLite database here would hide a production misconfiguration.
    assert "sqlite" not in str(caught.value).lower()


def _failed_run(store: PersistentRunStore, run_id: str) -> None:
    store.create(
        run_id=run_id,
        request_id=f"req-{run_id}",
        owner=ANALYST.principal_id,
        status="failed",
        question="分析销售额",
        payload={
            "run_id": run_id,
            "status": "failed",
            "stop_reason": "graph_error",
            "question": "分析销售额",
        },
    )


class _FakeStatus:
    value = "completed"


class _FakeResponse:
    status = _FakeStatus()
    question = "分析销售额"
    stop_reason = "model_finished"

    def __init__(self, run_id: str):
        self.run_id = run_id

    def model_dump(self, mode: str = "json") -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": "completed",
            "stop_reason": "model_finished",
            "question": self.question,
        }


class _FakeRuntime:
    llm = None

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def resume(self, run_id: str, principal, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"run_id": run_id, **kwargs})
        return _FakeResponse(run_id)


def _manager(store: PersistentRunStore, settings: Settings, runtime=None):
    import logging

    from enterprise_business_agent.api.run_manager import RunManager

    return RunManager(
        runtime=runtime or _FakeRuntime(),
        store=store,
        settings=settings,
        logger=logging.getLogger("test.run_manager"),
        worker_id="worker-test",
    )


def test_claim_is_exclusive_across_store_instances(env):
    url = f"sqlite:///{env.tmp_path / 'claims.db'}"
    first = PersistentRunStore(url)
    second = PersistentRunStore(url)
    try:
        _failed_run(first, "claim-exclusive")
        won = first.acquire_claim("claim-exclusive", "worker-a", 60, allowed_statuses={"failed"})
        assert won.claim is not None
        assert won.claim.claim_version == 1
        lost = second.acquire_claim("claim-exclusive", "worker-b", 60, allowed_statuses={"failed"})
        assert lost.claim is None
        assert lost.reason == "lease_held"
        assert lost.held_by == "worker-a"
    finally:
        first.close()
        second.close()


def test_expired_lease_allows_takeover_and_blocks_stale_writes(env):
    import time

    from enterprise_business_agent.api.run_store import RunClaimLost

    url = f"sqlite:///{env.tmp_path / 'takeover.db'}"
    first = PersistentRunStore(url)
    second = PersistentRunStore(url)
    try:
        _failed_run(first, "claim-takeover")
        stale = first.acquire_claim("claim-takeover", "worker-stale", 0.2, allowed_statuses={"failed"}).claim
        assert stale is not None
        time.sleep(0.3)
        fresh = second.acquire_claim("claim-takeover", "worker-fresh", 60, allowed_statuses={"failed"}).claim
        assert fresh is not None
        assert fresh.claim_version == stale.claim_version + 1

        with pytest.raises(RunClaimLost):
            first.put(
                "claim-takeover",
                {"status": "completed", "run_id": "claim-takeover"},
                owner=ANALYST.principal_id,
                status="completed",
                fence=stale,
            )
        second.put(
            "claim-takeover",
            {"status": "completed", "run_id": "claim-takeover"},
            owner=ANALYST.principal_id,
            status="completed",
            fence=fresh,
        )
        assert second.get_status("claim-takeover") == "completed"
    finally:
        first.close()
        second.close()


def test_crashed_worker_lease_expires_and_manager_takes_over(env):
    import time

    url = f"sqlite:///{env.tmp_path / 'crash-takeover.db'}"
    store = PersistentRunStore(url)
    try:
        _failed_run(store, "claim-crash")
        crashed = store.acquire_claim("claim-crash", "worker-crashed", 0.2, allowed_statuses={"failed"}).claim
        assert crashed is not None
        time.sleep(0.3)
        settings = env.settings.model_copy(update={"run_lease_seconds": 5, "agent_run_max_lifecycle_seconds": 60})
        runtime = _FakeRuntime()
        manager = _manager(store, settings, runtime)
        try:
            result = manager.resume("claim-crash", ANALYST, owner=ANALYST.principal_id)
        finally:
            manager.shutdown()
        assert result["status"] == "completed"
        assert runtime.calls and runtime.calls[0]["max_seconds"] == settings.agent_max_seconds
        state = store.claim_state("claim-crash")
        assert state is not None and state["claim_owner"] is None
    finally:
        store.close()


def test_lifecycle_budget_accumulates_across_resumes(env):
    from enterprise_business_agent.api.run_manager import RunResumeError

    url = f"sqlite:///{env.tmp_path / 'lifecycle.db'}"
    store = PersistentRunStore(url)
    try:
        _failed_run(store, "lifecycle-run")
        settings = env.settings.model_copy(
            update={
                "agent_run_max_lifecycle_seconds": 5,
                "run_lease_seconds": 30,
            }
        )
        claim = store.acquire_claim("lifecycle-run", "worker-a", 30, allowed_statuses={"failed"}).claim
        assert claim is not None
        store.record_execution(claim, 3.0, resumed=True)
        store.release_claim(claim)
        state = store.lifecycle_state("lifecycle-run")
        assert state["execution_seconds"] == pytest.approx(3.0)
        assert state["resume_count"] == 1

        second = store.acquire_claim("lifecycle-run", "worker-a", 30, allowed_statuses={"failed"}).claim
        assert second is not None
        store.record_execution(second, 2.5, resumed=True)
        store.release_claim(second)
        state = store.lifecycle_state("lifecycle-run")
        assert state["execution_seconds"] == pytest.approx(5.5)
        assert state["resume_count"] == 2

        manager = _manager(store, settings)
        try:
            with pytest.raises(RunResumeError) as caught:
                manager.resume("lifecycle-run", ANALYST, owner=ANALYST.principal_id)
        finally:
            manager.shutdown()
        assert caught.value.code == "run_lifecycle_exhausted"
        assert store.get_status("lifecycle-run") == "failed"
    finally:
        store.close()


def test_api_run_completion_releases_the_database_lease(api_env):
    from fastapi.testclient import TestClient

    from enterprise_business_agent.api import ResultStore, create_app

    auth = {"X-API-Key": "agent-key"}
    app = create_app(
        api_env.settings,
        session_factory=api_env.factory,
        engine=api_env.engine,
        llm_client=ScriptedLLM([finish_response("完成。")]),
        result_store=ResultStore(),
        run_store=api_env.run_store,
    )
    with TestClient(app) as client:
        created = client.post("/api/v1/agent/runs", headers=auth, json={"question": "分析销售额"})
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]
        for _ in range(100):
            document = api_env.run_store.get(run_id)
            if document and document["status"] in {
                "completed",
                "completed_with_warnings",
                "failed",
            }:
                break
            import time

            time.sleep(0.02)
        state = api_env.run_store.claim_state(run_id)
        assert state is not None
        assert state["claim_owner"] is None
        assert state["lease_expires_at"] is None


@pytest.fixture()
def api_env(tmp_path):
    from enterprise_business_agent.db import Base, make_engine, make_readonly_engine

    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=10,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        api_keys={"agent-key": "analyst", "other-key": "analyst"},
        allow_anonymous_api=False,
        phase5_narrative_enabled=False,
    )
    data_path = tmp_path / "api-data.db"
    write_engine = make_engine(f"sqlite:///{data_path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()
    engine = make_readonly_engine(f"sqlite:///{data_path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    run_store = PersistentRunStore(f"sqlite:///{tmp_path / 'api-runs.db'}")
    yield SimpleNamespace(
        settings=settings,
        factory=factory,
        engine=engine,
        run_store=run_store,
    )
    run_store.close()
    engine.dispose()


def test_api_resume_continues_a_crashed_run(api_env):
    from fastapi.testclient import TestClient

    from enterprise_business_agent.api import ResultStore, create_app

    auth = {"X-API-Key": "agent-key"}
    llm = ScriptedLLM([TOP_N_CALL, RuntimeError("simulated process crash"), finish_response("恢复完成。")])
    app = create_app(
        api_env.settings,
        session_factory=api_env.factory,
        engine=api_env.engine,
        llm_client=llm,
        result_store=ResultStore(),
        run_store=api_env.run_store,
    )
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/agent/runs",
            headers=auth,
            json={"question": "分析 2025-03-01 各区域销售额"},
        )
        assert created.status_code == 200, created.text
        run = created.json()
        assert run["status"] == "failed"
        run_id = run["run_id"]
        assert api_env.run_store.count_tool_results(run_id) == 1

        resumed = client.post(f"/api/v1/agent/runs/{run_id}/resume", headers=auth)
        assert resumed.status_code == 200, resumed.text
        body = resumed.json()
        assert body["status"] in {"completed", "completed_with_warnings"}
        assert body["run_id"] == run_id
        assert api_env.run_store.count_tool_results(run_id) == 1

        repeated = client.post(f"/api/v1/agent/runs/{run_id}/resume", headers=auth)
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] == "run_not_resumable"

        other = client.post(
            f"/api/v1/agent/runs/{run_id}/resume",
            headers={"X-API-Key": "other-key"},
        )
        assert other.status_code == 404

        events = api_env.run_store.list_events(run_id)
        assert any(item["event_type"] == "run_resumed" for item in events)
