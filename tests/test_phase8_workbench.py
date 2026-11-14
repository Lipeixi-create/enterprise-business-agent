"""Phase 8.3 workbench integration tests."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine

KEYS = {
    "analyst-a": "analyst",
    "analyst-b": "analyst",
    "admin-key": "admin",
}


@contextmanager
def _client(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        api_timeout_seconds=5,
        api_keys=KEYS,
        allow_anonymous_api=False,
        run_store_url=f"sqlite:///{tmp_path / 'runs.db'}",
    )
    path = tmp_path / "business.db"
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
        with TestClient(app) as client:
            yield client
    finally:
        engine.dispose()


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def test_workbench_is_served_by_the_api(tmp_path):
    with _client(tmp_path) as client:
        page = client.get("/workbench/")
        assert page.status_code == 200
        assert "经营分析工作台" in page.text
        assert "./app.js" in page.text

        script = client.get("/workbench/app.js")
        assert script.status_code == 200
        assert "streamRun" in script.text

        redirect = client.get("/", follow_redirects=False)
        assert redirect.status_code in {307, 308}
        assert redirect.headers["location"] == "/workbench/"


def test_me_returns_role_and_scopes_without_the_key(tmp_path):
    with _client(tmp_path) as client:
        response = client.get("/api/v1/me", headers=_headers("analyst-a"))
        assert response.status_code == 200
        body = response.json()
        assert body["role"] == "analyst"
        assert body["authenticated"] is True
        assert "metrics:read" in body["scopes"]
        assert "analyst-a" not in response.text


def test_run_history_is_owner_scoped_and_admin_can_read_all(tmp_path):
    with _client(tmp_path) as client:
        me_a = client.get("/api/v1/me", headers=_headers("analyst-a")).json()
        me_b = client.get("/api/v1/me", headers=_headers("analyst-b")).json()
        store = client.app.state.run_store
        store.put(
            "run-a",
            {"status": "completed", "stop_reason": "finished", "errors": []},
            owner=me_a["principal_id"],
            status="completed",
            question="A 的问题",
        )
        store.put(
            "run-b",
            {"status": "failed", "stop_reason": "tool_failure", "errors": [{"code": "x"}]},
            owner=me_b["principal_id"],
            status="failed",
            question="B 的问题",
        )

        only_a = client.get("/api/v1/agent/runs", headers=_headers("analyst-a"))
        assert only_a.status_code == 200
        payload = only_a.json()
        assert payload["scope"] == "self"
        assert [item["run_id"] for item in payload["items"]] == ["run-a"]
        assert payload["items"][0]["question"] == "A 的问题"

        admin = client.get("/api/v1/agent/runs", headers=_headers("admin-key"))
        assert admin.status_code == 200
        admin_payload = admin.json()
        assert admin_payload["scope"] == "all"
        assert {item["run_id"] for item in admin_payload["items"]} == {"run-a", "run-b"}

        failed = client.get(
            "/api/v1/agent/runs",
            headers=_headers("admin-key"),
            params={"status": "failed"},
        )
        assert [item["run_id"] for item in failed.json()["items"]] == ["run-b"]
        assert failed.json()["items"][0]["error_count"] == 1

        limited = client.get(
            "/api/v1/agent/runs",
            headers=_headers("admin-key"),
            params={"limit": 1},
        )
        assert len(limited.json()["items"]) == 1
        assert limited.json()["has_more"] is True


def test_run_history_requires_authentication(tmp_path):
    with _client(tmp_path) as client:
        response = client.get("/api/v1/agent/runs")
        assert response.status_code == 401
