"""Phase 9.2 acceptance on a real PostgreSQL instance.

Every test in this module requires ``EBA_TEST_POSTGRES_URL``,
``EBA_TEST_POSTGRES_READONLY_URL`` and ``EBA_TEST_POSTGRES_RUNTIME_URL`` and
never falls back to SQLite. The business tables and runtime tables are created
in the configured disposable database. The LLM is the repository's scripted
test double; these tests validate PostgreSQL persistence, isolation and failure
handling, not real-model answer quality.
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.api import ResultStore, create_app
from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.api.runtime_schema import (
    create_runtime_schema,
    drop_runtime_schema,
    grant_runtime_role,
)
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_readonly_engine
from enterprise_business_agent.security import authenticate
from tests.agent_fakes import ScriptedLLM, finish_response

URL = os.getenv("EBA_TEST_POSTGRES_URL")
READONLY_URL = os.getenv("EBA_TEST_POSTGRES_READONLY_URL")
RUNTIME_URL = os.getenv("EBA_TEST_POSTGRES_RUNTIME_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not (URL and READONLY_URL and RUNTIME_URL),
        reason="real PostgreSQL test URLs are not set",
    ),
]

KEYS = {
    "analyst-a": "analyst",
    "analyst-b": "analyst",
    "admin-key": "admin",
    "restricted-key": "restricted_analyst",
}
AUTH_A = {"X-API-Key": "analyst-a"}
AUTH_B = {"X-API-Key": "analyst-b"}


def _role(url: str) -> str:
    name = make_url(url).username or ""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise RuntimeError(f"unsafe role name in test URL: {name!r}")
    return name


@pytest.fixture(scope="module")
def admin_engine():
    engine = create_engine(URL, future=True)
    drop_runtime_schema(engine)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        generate_data(session, date(2024, 1, 1), 24, reset=False)
        session.commit()
    create_runtime_schema(engine)
    grant_runtime_role(engine, _role(RUNTIME_URL))
    readonly = _role(READONLY_URL)
    with engine.begin() as connection:
        connection.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{readonly}"'))
    yield engine
    drop_runtime_schema(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def settings():
    return Settings(
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
        authz_policy={
            "restricted_analyst": {
                "metrics": ["sales_amount"],
                "dimensions": ["region", "product"],
                "regions": ["EC"],
            }
        },
    )


@pytest.fixture()
def business_engine(admin_engine, settings):
    engine = make_readonly_engine(READONLY_URL, settings.query_timeout_ms)
    yield engine
    engine.dispose()


@pytest.fixture()
def run_store(admin_engine):
    store = PersistentRunStore(RUNTIME_URL)
    yield store
    store.close()


def _client(settings, business_engine, run_store, llm=None):
    app = create_app(
        settings,
        session_factory=sessionmaker(bind=business_engine, class_=Session, expire_on_commit=False),
        engine=business_engine,
        llm_client=llm or ScriptedLLM([finish_response("已完成。")]),
        result_store=ResultStore(),
        run_store=run_store,
    )
    return TestClient(app)


def _query(client, key: str, **overrides):
    payload = {
        "metric": "sales_amount",
        "start": "2025-03-01",
        "end": "2025-03-01",
        **overrides,
    }
    return client.post("/api/v1/metrics/query", headers={"X-API-Key": key}, json=payload)


def test_postgres_region_scope_and_department_isolation(settings, business_engine, run_store):
    with _client(settings, business_engine, run_store) as client:
        omitted = _query(client, "restricted-key", dimensions=["region"])
        assert omitted.status_code == 403

        other_region = _query(
            client,
            "restricted-key",
            filters=[{"dimension": "region", "operator": "eq", "value": "NC"}],
        )
        assert other_region.status_code == 403

        widened = _query(
            client,
            "restricted-key",
            filters=[
                {
                    "dimension": "region",
                    "operator": "in",
                    "value": ["EC", "NC"],
                }
            ],
        )
        assert widened.status_code == 403

        allowed = _query(
            client,
            "restricted-key",
            filters=[{"dimension": "region", "operator": "eq", "value": "EC"}],
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["status"] == "ok"


def test_postgres_cross_user_run_isolation(settings, business_engine, run_store):
    with _client(settings, business_engine, run_store) as client:
        created = client.post(
            "/api/v1/agent/runs",
            headers=AUTH_A,
            json={"question": "分析华东销售", "max_tool_calls": 1},
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["run_id"]

        owner = client.get(f"/api/v1/agent/runs/{run_id}", headers=AUTH_A)
        assert owner.status_code == 200

        for key in ("analyst-b", "restricted-key"):
            denied = client.get(f"/api/v1/agent/runs/{run_id}", headers={"X-API-Key": key})
            assert denied.status_code in {403, 404}, denied.text
            assert denied.json()["error"]["code"] in {
                "forbidden",
                "agent_run_not_found",
            }

        admin = client.get(f"/api/v1/agent/runs/{run_id}", headers={"X-API-Key": "admin-key"})
        assert admin.status_code == 200


def test_postgres_concurrent_idempotency_creates_one_run(settings, business_engine, run_store):
    with _client(settings, business_engine, run_store) as client:
        barrier = threading.Barrier(4)

        def submit() -> tuple[int, str]:
            barrier.wait(timeout=5)
            response = client.post(
                "/api/v1/agent/runs/async",
                headers={**AUTH_A, "Idempotency-Key": "phase9-2-postgres-key"},
                json={"question": "PostgreSQL 并发幂等测试"},
            )
            return response.status_code, response.json()["run_id"]

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: submit(), range(4)))

        assert {status for status, _ in results} == {202}
        assert len({run_id for _, run_id in results}) == 1


def test_postgres_restart_marks_unfinished_run_interrupted(settings, business_engine, run_store):
    principal_id = authenticate(settings, "analyst-a").principal_id
    run_store.put(
        "run-pg-restart",
        {
            "run_id": "run-pg-restart",
            "status": "running",
            "question": "PostgreSQL 重启恢复测试",
            "stop_reason": None,
            "errors": [],
        },
        owner=principal_id,
        status="running",
        question="PostgreSQL 重启恢复测试",
    )
    run_store.close()

    restarted = PersistentRunStore(RUNTIME_URL)
    try:
        with _client(settings, business_engine, restarted) as client:
            history = client.get("/api/v1/agent/runs?limit=50", headers=AUTH_A)
            assert history.status_code == 200, history.text
            item = next(row for row in history.json()["items"] if row["run_id"] == "run-pg-restart")
            assert item["status"] == "interrupted"

            run = client.get("/api/v1/agent/runs/run-pg-restart", headers=AUTH_A)
            assert run.status_code == 200
            assert run.json()["status"] == "interrupted"
            assert run.json()["stop_reason"] == "process_restart"
    finally:
        restarted.close()


def test_postgres_connection_privilege_failure_is_not_silently_degraded(admin_engine):
    runtime_role = _role(RUNTIME_URL)
    database = make_url(URL).database
    with admin_engine.begin() as connection:
        connection.execute(text(f'REVOKE CONNECT ON DATABASE "{database}" FROM PUBLIC'))
        connection.execute(text(f'REVOKE CONNECT ON DATABASE "{database}" FROM "{runtime_role}"'))
    try:
        with pytest.raises(OperationalError):
            PersistentRunStore(RUNTIME_URL)
    finally:
        with admin_engine.begin() as connection:
            connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO "{runtime_role}"'))
            connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO PUBLIC'))
    recovered = PersistentRunStore(RUNTIME_URL)
    recovered.close()
