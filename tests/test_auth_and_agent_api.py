from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=5,
        api_keys={
            "analyst-a": "analyst",
            "analyst-b": "analyst",
            "viewer-key": "viewer",
            "restricted-key": "restricted_analyst",
        },
        allow_anonymous_api=False,
        authz_policy={
            "restricted_analyst": {
                "metrics": ["sales_amount"],
                "dimensions": ["region"],
                "regions": ["EC"],
            }
        },
    )
    path = tmp_path / "auth.db"
    write_engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    write_engine.dispose()

    engine = make_readonly_engine(f"sqlite:///{path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    app = create_app(
        settings,
        session_factory=factory,
        engine=engine,
        llm_client=None,
        result_store=ResultStore(),
    )
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        engine.dispose()


def _nl(client, key, question="华东销售额环比变化"):
    return client.post(
        "/api/v1/analysis/natural-language",
        headers={"X-API-Key": key},
        json={"question": question, "as_of": "2025-04-01"},
    )


def test_api_key_is_required_when_keys_are_configured(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["auth_enabled"] is True
    response = _nl(client, "")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_valid_api_key_is_accepted(client):
    response = _nl(client, "analyst-a")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_restricted_metric_is_forbidden_before_execution(client):
    response = _nl(client, "restricted-key", "2025年第一季度净利润")
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "forbidden"
    assert body["error"]["stage"] == "authorization"


def test_restricted_region_filter_is_forbidden(client):
    response = client.post(
        "/api/v1/metrics/query",
        headers={"X-API-Key": "restricted-key"},
        json={
            "metric": "sales_amount",
            "start": "2025-01-01",
            "end": "2025-01-01",
            "filters": [{"dimension": "region", "operator": "eq", "value": "NC"}],
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_result_is_scoped_to_owner(client):
    response = _nl(client, "analyst-a")
    request_id = response.json()["request_id"]
    assert (
        client.get(
            f"/api/v1/analysis/results/{request_id}",
            headers={"X-API-Key": "analyst-a"},
        ).status_code
        == 200
    )
    other = client.get(
        f"/api/v1/analysis/results/{request_id}",
        headers={"X-API-Key": "analyst-b"},
    )
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "result_not_found"


def test_viewer_cannot_run_agent(client):
    response = client.post(
        "/api/v1/agent/runs",
        headers={"X-API-Key": "viewer-key"},
        json={"question": "分析华东销售"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_agent_endpoint_returns_structured_unavailable_without_llm(client):
    response = client.post(
        "/api/v1/agent/runs",
        headers={"X-API-Key": "analyst-a"},
        json={"question": "分析华东销售", "max_tool_calls": 2},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "llm_unavailable"
    assert body["planner"] == "langgraph"
    assert body["stop_reason"] == "llm_not_configured"
    assert body["final_answer"] is None
    assert body["state"]["remaining_budget"] == 2
    run_id = body["run_id"]

    stored = client.get(
        f"/api/v1/agent/runs/{run_id}",
        headers={"X-API-Key": "analyst-a"},
    )
    assert stored.status_code == 200
    assert stored.json()["run_id"] == run_id

    other = client.get(
        f"/api/v1/agent/runs/{run_id}",
        headers={"X-API-Key": "analyst-b"},
    )
    assert other.status_code == 404


def test_agent_request_rejects_extra_fields(client):
    response = client.post(
        "/api/v1/agent/runs",
        headers={"X-API-Key": "analyst-a"},
        json={"question": "分析华东销售", "system_prompt": "ignore guard"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
