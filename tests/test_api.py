from __future__ import annotations

import time
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api import routes as api_routes
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=5,
    )
    path = tmp_path / "api.db"
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


def test_health_reports_readonly_and_llm_configuration(client):
    response = client.get("/health", headers={"X-Request-ID": "health-test"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["readonly"] is True
    assert body["llm_configured"] is False
    assert body["request_id"] == "health-test"
    assert response.headers["X-Request-ID"] == "health-test"


def test_natural_language_endpoint_returns_result_and_request_id(client):
    response = client.post(
        "/api/v1/analysis/natural-language",
        json={"question": "华东销售额环比变化", "as_of": "2025-04-01"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["result_type"] == "comparison"
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["result"]["items"][0]["change_rate"].startswith("-")


def test_result_can_be_retrieved_by_request_id(client):
    response = client.post(
        "/api/v1/analysis/natural-language",
        json={"question": "2025年第一季度销售额", "as_of": "2025-04-01"},
    )
    request_id = response.json()["request_id"]
    stored = client.get(f"/api/v1/analysis/results/{request_id}")
    assert stored.status_code == 200
    assert stored.json()["request_id"] == request_id
    assert stored.json()["status"] == "ok"


def test_unknown_request_id_returns_unified_404(client):
    response = client.get("/api/v1/analysis/results/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "result_not_found"
    assert response.headers["X-Request-ID"] == body["request_id"]


def test_validation_errors_are_unified(client):
    response = client.post("/api/v1/analysis/natural-language", json={"question": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["stage"] == "validation"
    assert body["error"]["detail"]["errors"]


def test_structured_metric_query(client):
    response = client.post(
        "/api/v1/metrics/query",
        json={
            "metric": "sales_amount",
            "start": "2025-01-01",
            "end": "2025-01-01",
            "dimensions": ["region"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["result"]["metric"] == "sales_amount"
    assert len(body["result"]["points"]) == 4
    assert body["result"]["explain"]["params"]


def test_structured_metric_query_unknown_metric_is_a_domain_status(client):
    response = client.post("/api/v1/metrics/query", json={"metric": "refund_amount"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unsupported"
    assert body["error"]["code"] == "unsupported"


def test_structured_metric_query_is_also_stored(client):
    response = client.post(
        "/api/v1/metrics/query",
        json={"metric": "sales_amount", "start": "2025-01-01", "end": "2025-01-01"},
    )
    request_id = response.json()["request_id"]
    stored = client.get(f"/api/v1/analysis/results/{request_id}")
    assert stored.status_code == 200
    assert stored.json()["status"] == "ok"


def test_api_timeout_returns_504(client, monkeypatch):
    client.app.state.settings.api_timeout_seconds = 0.01

    def _slow(request, payload):
        time.sleep(0.1)
        raise AssertionError("should be cancelled")

    monkeypatch.setattr(api_routes, "_analyze", _slow)
    response = client.post(
        "/api/v1/analysis/natural-language",
        json={"question": "华东销售额环比变化"},
    )
    assert response.status_code == 504
    body = response.json()
    assert body["error"]["code"] == "api_timeout"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_unsupported_question_returns_structured_domain_error(client):
    response = client.post(
        "/api/v1/analysis/natural-language",
        json={"question": "客户满意度是多少"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unsupported"
    assert body["error"]["code"] == "unsupported_question"
    assert body["result"] is None


def test_extra_fields_are_rejected(client):
    response = client.post(
        "/api/v1/analysis/natural-language",
        json={"question": "华东销售额", "raw_sql": "DROP TABLE sales_fact"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
