"""Phase 8.4 production acceptance tests.

These tests use the real read-only SQLite database, the real LangGraph agent,
the real Evidence/Finding/Report pipeline and the real FastAPI runtime. The LLM
is the repository's scripted test double: these are offline functional and
failure-mode tests, not a real-model quality evaluation.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import (
    AgentRuntime,
    AnalysisGateway,
    ToolRegistry,
)
from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.llm import LLMUnavailable
from enterprise_business_agent.security import AuthorizationPolicy, authenticate
from tests.agent_fakes import ScriptedLLM, finish_response, tool_response

KEYS = {
    "agent-a": "analyst",
    "agent-b": "analyst",
    "agent-admin": "admin",
}
AUTH_A = {"X-API-Key": "agent-a"}
AUTH_B = {"X-API-Key": "agent-b"}
AUTH_ADMIN = {"X-API-Key": "agent-admin"}
TERMINAL = {
    "completed",
    "completed_with_warnings",
    "no_evidence",
    "no_data",
    "failed",
    "budget_exhausted",
    "timeout",
    "llm_unavailable",
    "llm_timeout",
    "invalid_action",
    "repeated_tool_call",
    "tool_failure",
    "cancelled",
    "interrupted",
}


@pytest.fixture()
def env(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=10,
        agent_max_seconds=10,
        agent_max_tool_calls=4,
        agent_tool_timeout_seconds=2,
        agent_tool_max_retries=0,
        api_keys=KEYS,
        allow_anonymous_api=False,
        phase5_narrative_enabled=False,
    )
    db_path = tmp_path / "business.db"
    write_engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()

    engine = make_readonly_engine(f"sqlite:///{db_path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    run_store = PersistentRunStore(f"sqlite:///{tmp_path / 'runs.db'}")
    try:
        yield {
            "settings": settings,
            "factory": factory,
            "engine": engine,
            "run_store": run_store,
            "tmp_path": tmp_path,
        }
    finally:
        run_store.close()
        engine.dispose()


@contextmanager
def _client(env, llm, *, runtime=None, run_store=None):
    app = create_app(
        env["settings"],
        session_factory=env["factory"],
        engine=env["engine"],
        llm_client=llm,
        result_store=ResultStore(),
        run_store=run_store or env["run_store"],
        agent_runtime=runtime,
    )
    with TestClient(app) as client:
        yield client


def _call_pair(prefix: str = "c"):
    return [
        tool_response(
            f"{prefix}1",
            "compare_periods",
            {"metric": "sales_amount", "kind": "mom", "period": "2025-12-01"},
        ),
        tool_response(
            f"{prefix}2",
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


def _path(data: Any, path: str) -> Any:
    current = data
    for part in path.replace("[", ".").replace("]", "").split("."):
        if not part or part.startswith("@"):
            return None
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            return None
    return current


def _assert_chain(client: TestClient, run: dict, *, expected_calls: int) -> dict:
    run_id = run["run_id"]
    assert run["status"] in {"completed", "completed_with_warnings"}, run
    assert run["report_id"], run
    assert run["report_validation_passed"] is True, run

    evidence_response = client.get(
        f"/api/v1/agent/runs/{run_id}/evidence", headers=AUTH_A
    )
    assert evidence_response.status_code == 200
    evidence = evidence_response.json()["evidence"]
    assert len(evidence) == expected_calls
    evidence_ids = {item["evidence_id"] for item in evidence}
    for item in evidence:
        assert item["run_id"] == run_id
        assert item["tool_call_id"]
        assert item["status"] == "valid"
        assert item["traceable"] is True
        assert item["source_tables"]
        assert item["metric_definition_version"]
        assert item["result"]

    findings_response = client.get(
        f"/api/v1/agent/runs/{run_id}/findings", headers=AUTH_A
    )
    assert findings_response.status_code == 200
    findings = findings_response.json()["findings"]
    assert findings
    finding_ids = {item["finding_id"] for item in findings}
    for finding in findings:
        assert finding["evidence_ids"]
        assert set(finding["evidence_ids"]) <= evidence_ids

    report_response = client.get(
        f"/api/v1/agent/runs/{run_id}/report", headers=AUTH_A
    )
    assert report_response.status_code == 200
    stored = report_response.json()
    assert stored["validation"]["passed"] is True
    assert stored["validation"]["issues"] == []
    document = stored["report"]
    assert document["report_info"]["run_id"] == run_id
    assert {
        item["evidence_id"] for item in document["evidence_references"]
    } == evidence_ids
    assert {
        finding_id
        for item in document["conclusions"]
        for finding_id in item["finding_ids"]
    } <= finding_ids

    for card in document["key_metrics"]:
        assert card["value"]["evidence_id"] in evidence_ids
        assert card["value"]["value"] != ""
    for conclusion in document["conclusions"]:
        assert set(conclusion["evidence_ids"]) <= evidence_ids
        assert set(conclusion["finding_ids"]) <= finding_ids
        for ref in conclusion["value_refs"]:
            assert ref["evidence_id"] in evidence_ids
    return {"evidence": evidence, "report": document, "findings": findings}


SCENARIOS = [
    (
        "sales-region",
        "2025年12月华东销售额是多少？",
        [
            tool_response(
                "s1",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                    "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
                },
            ),
            finish_response("已完成分析。"),
        ],
        "points.0.value",
        (18129454.18, 18129454.20),
    ),
    (
        "marketing-roi",
        "2025年12月营销ROI排名前三的活动",
        [
            tool_response(
                "m1",
                "top_n",
                {
                    "metric": "ROI",
                    "dimension": "campaign",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                    "n": 3,
                },
            ),
            finish_response("已完成分析。"),
        ],
        "items.0.value",
        (1.73734, 1.73736),
    ),
    (
        "finance-qoq",
        "2025年第四季度净利润环比变化如何？",
        [
            tool_response(
                "f1",
                "compare_periods",
                {"metric": "net_profit", "kind": "qoq", "year": 2025, "quarter": 4},
            ),
            finish_response("已完成分析。"),
        ],
        "items.0.change_rate",
        (-0.22474, -0.22473),
    ),
    (
        "supply-stockout",
        "2025年12月缺货率最高的5个产品",
        [
            tool_response(
                "p1",
                "top_n",
                {
                    "metric": "stockout_rate",
                    "dimension": "product",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                    "n": 5,
                },
            ),
            finish_response("已完成分析。"),
        ],
        "items.0.value",
        (0.22357, 0.22358),
    ),
    (
        "hr-cost",
        "2025年12月人力成本最高的3个部门",
        [
            tool_response(
                "h1",
                "top_n",
                {
                    "metric": "labor_cost",
                    "dimension": "department",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                    "n": 3,
                },
            ),
            finish_response("已完成分析。"),
        ],
        "items.0.value",
        (0.0, float("inf")),
    ),
]


@pytest.mark.parametrize(
    "case_id,question,responses,value_path,value_range",
    SCENARIOS,
    ids=[item[0] for item in SCENARIOS],
)
def test_business_scenarios_complete_the_full_chain(
    env, case_id, question, responses, value_path, value_range
):
    with _client(env, ScriptedLLM(responses)) as client:
        response = client.post(
            "/api/v1/agent/runs", headers=AUTH_A, json={"question": question}
        )
        assert response.status_code == 200, response.text
        run = response.json()
        chain = _assert_chain(client, run, expected_calls=1)
        value = _path(chain["evidence"][0]["result"], value_path)
        assert value is not None, (case_id, chain["evidence"][0]["result"])
        assert value_range[0] <= float(value) <= value_range[1]


def test_cross_domain_scenario_keeps_both_evidence_sources(env):
    responses = [
        tool_response(
            "x1",
            "compare_periods",
            {
                "metric": "sales_amount",
                "kind": "mom",
                "period": "2025-12-01",
                "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
            },
        ),
        tool_response(
            "x2",
            "top_n",
            {
                "metric": "stockout_rate",
                "dimension": "product",
                "start": "2025-12-01",
                "end": "2025-12-01",
                "n": 3,
                "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
            },
        ),
        finish_response("已完成跨域分析。"),
    ]
    with _client(env, ScriptedLLM(responses)) as client:
        response = client.post(
            "/api/v1/agent/runs",
            headers=AUTH_A,
            json={"question": "华东2025年12月销售额环比下降，请结合缺货数据找原因"},
        )
        assert response.status_code == 200, response.text
        run = response.json()
        chain = _assert_chain(client, run, expected_calls=2)
        assert [item["evidence_type"] for item in chain["evidence"]] == [
            "comparison",
            "ranking",
        ]
        change_rate = _path(chain["evidence"][0]["result"], "items.0.change_rate")
        top_stockout = _path(chain["evidence"][1]["result"], "items.0.value")
        assert -0.364968 <= float(change_rate) <= -0.364965
        assert 0.72021 <= float(top_stockout) <= 0.72023


class OneEvidenceLLM:
    """Thread-safe scripted LLM for concurrent runs; no shared response list."""

    model = "fake-concurrent-model"

    def complete_with_tools(self, *, system, messages, tools, **kwargs):
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            return tool_response(
                f"c-{uuid.uuid4().hex[:10]}",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                },
            )
        return finish_response("并发分析完成。")


def test_concurrent_async_runs_are_unique_and_owner_isolated(env):
    with _client(env, OneEvidenceLLM()) as client:
        submitted: dict[str, list[str]] = {"agent-a": [], "agent-b": []}

        def submit(key: str, index: int) -> str:
            headers = {"X-API-Key": key}
            response = client.post(
                "/api/v1/agent/runs/async",
                headers=headers,
                json={"question": f"并发测试 {key} #{index}"},
            )
            assert response.status_code == 202, response.text
            return response.json()["run_id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = []
            for key in submitted:
                for index in range(4):
                    futures.append((key, pool.submit(submit, key, index)))
            for key, future in futures:
                submitted[key].append(future.result())

        assert len(set(submitted["agent-a"]) | set(submitted["agent-b"])) == 8
        assert set(submitted["agent-a"]).isdisjoint(submitted["agent-b"])

        deadline = time.monotonic() + 20
        histories = {}
        while time.monotonic() < deadline:
            histories = {
                key: client.get(
                    "/api/v1/agent/runs?limit=50", headers={"X-API-Key": key}
                ).json()
                for key in submitted
            }
            statuses = [
                item["status"]
                for payload in histories.values()
                for item in payload["items"]
            ]
            if len(statuses) == 8 and all(status in TERMINAL for status in statuses):
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"concurrent runs did not finish: {histories}")

        for key, run_ids in submitted.items():
            payload = histories[key]
            assert payload["scope"] == "self"
            visible = {item["run_id"] for item in payload["items"]}
            assert visible == set(run_ids)
            for run_id in run_ids:
                run = client.get(
                    f"/api/v1/agent/runs/{run_id}",
                    headers={"X-API-Key": key},
                ).json()
                assert run["status"] == "completed", run
                assert run["report_validation_passed"] is True
                evidence = client.get(
                    f"/api/v1/agent/runs/{run_id}/evidence",
                    headers={"X-API-Key": key},
                ).json()
                assert evidence["count"] == 1

        cross_owner = client.get(
            f"/api/v1/agent/runs/{submitted['agent-a'][0]}", headers=AUTH_B
        )
        assert cross_owner.status_code == 404


def test_concurrent_duplicate_idempotency_creates_one_run(env):
    with _client(env, OneEvidenceLLM()) as client:
        barrier = threading.Barrier(4)

        def submit() -> tuple[int, str]:
            barrier.wait(timeout=5)
            response = client.post(
                "/api/v1/agent/runs/async",
                headers={**AUTH_A, "Idempotency-Key": "phase8-concurrent-key"},
                json={"question": "并发幂等测试"},
            )
            return response.status_code, response.json()["run_id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: submit(), range(4)))

        assert {status for status, _ in results} == {202}
        assert len({run_id for _, run_id in results}) == 1

        history = client.get(
            "/api/v1/agent/runs?limit=50", headers=AUTH_A
        ).json()
        matching = [
            item for item in history["items"] if item["question"] == "并发幂等测试"
        ]
        assert len(matching) == 1


def test_timeout_is_terminal_and_does_not_fabricate_success(env):
    with _client(env, ScriptedLLM([finish_response("不应被调用。")])) as client:
        response = client.post(
            "/api/v1/agent/runs",
            headers=AUTH_A,
            json={"question": "时间预算测试", "max_seconds": 0.000001},
        )
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["status"] == "timeout"
        assert run["stop_reason"] == "time_budget_exhausted"
        assert run["tool_calls"] == []

        stored = client.get(
            f"/api/v1/agent/runs/{run['run_id']}", headers=AUTH_A
        ).json()
        assert stored["status"] == "timeout"
        evidence = client.get(
            f"/api/v1/agent/runs/{run['run_id']}/evidence", headers=AUTH_A
        ).json()
        assert evidence["count"] == 0
        report = client.get(
            f"/api/v1/agent/runs/{run['run_id']}/report", headers=AUTH_A
        )
        if report.status_code == 200:
            assert report.json()["report"]["conclusions"] == []


class BrokenSessionFactory:
    kw = {"bind": None}

    def __init__(self, bind):
        self.kw = {"bind": bind}

    def __call__(self):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))


def test_business_database_outage_becomes_a_structured_tool_failure(env):
    broken_factory = BrokenSessionFactory(env["engine"])
    llm = ScriptedLLM(
        [
            tool_response(
                "db1",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                },
            ),
            finish_response("数据库不可用。"),
        ]
    )
    runtime = AgentRuntime(
        llm_client=llm,
        registry=ToolRegistry(
            AnalysisGateway(broken_factory, env["settings"]),
            AuthorizationPolicy(env["settings"]),
            env["settings"],
        ),
        settings=env["settings"],
    )
    with _client(env, llm, runtime=runtime) as client:
        response = client.post(
            "/api/v1/agent/runs", headers=AUTH_A, json={"question": "数据库故障测试"}
        )
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["status"] in {
            "no_evidence",
            "completed_with_warnings",
            "tool_failure",
        }, run
        assert run["tool_calls"][0]["status"] == "database_unavailable"
        assert run["tool_calls"][0]["error_code"] == "database_unavailable"
        assert run["evidence"] == []
        assert "connection lost" not in response.text
        assert "password" not in response.text.lower()


def test_model_service_failure_returns_llm_unavailable_without_tools(env):
    llm = ScriptedLLM([LLMUnavailable("simulated provider outage")])
    with _client(env, llm) as client:
        response = client.post(
            "/api/v1/agent/runs", headers=AUTH_A, json={"question": "模型故障测试"}
        )
        assert response.status_code == 200, response.text
        run = response.json()
        assert run["status"] == "llm_unavailable"
        assert run["tool_calls"] == []
        assert run["evidence"] == []
        evidence = client.get(
            f"/api/v1/agent/runs/{run['run_id']}/evidence", headers=AUTH_A
        )
        assert evidence.status_code == 200
        assert evidence.json()["count"] == 0


def test_restart_marks_unfinished_run_interrupted_and_history_exposes_it(env):
    principal_id = authenticate(env["settings"], "agent-a").principal_id
    env["run_store"].put(
        "run-restart-phase8",
        {
            "run_id": "run-restart-phase8",
            "status": "running",
            "question": "重启恢复测试",
            "stop_reason": None,
            "errors": [],
        },
        owner=principal_id,
        status="running",
        question="重启恢复测试",
    )
    with _client(env, ScriptedLLM([finish_response("不应被调用。")])) as client:
        history = client.get("/api/v1/agent/runs", headers=AUTH_A).json()
        item = next(
            row for row in history["items"] if row["run_id"] == "run-restart-phase8"
        )
        assert item["status"] == "interrupted"
        run = client.get(
            "/api/v1/agent/runs/run-restart-phase8", headers=AUTH_A
        ).json()
        assert run["status"] == "interrupted"
        assert run["stop_reason"] == "process_restart"


def test_report_evidence_and_approval_are_owner_isolated(env):
    with _client(env, ScriptedLLM(_call_pair())) as client:
        response = client.post(
            "/api/v1/agent/runs", headers=AUTH_A, json={"question": "审批隔离测试"}
        )
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]

        for suffix in ("evidence", "findings", "report"):
            other = client.get(
                f"/api/v1/agent/runs/{run_id}/{suffix}", headers=AUTH_B
            )
            assert other.status_code == 404, (suffix, other.text)
            admin = client.get(
                f"/api/v1/agent/runs/{run_id}/{suffix}", headers=AUTH_ADMIN
            )
            assert admin.status_code == 200, (suffix, admin.text)

        approval_other = client.get(
            f"/api/v1/agent/runs/{run_id}/report/approval", headers=AUTH_B
        )
        assert approval_other.status_code == 404
        approval_admin = client.get(
            f"/api/v1/agent/runs/{run_id}/report/approval", headers=AUTH_ADMIN
        )
        assert approval_admin.status_code == 200
