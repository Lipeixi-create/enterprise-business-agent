from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from enterprise_business_agent.config import Settings, get_settings
from enterprise_business_agent.db import _sqlite_readonly_url, make_engine, make_readonly_engine


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:admin-pass@localhost/db")
    monkeypatch.setenv("READONLY_DATABASE_URL", "postgresql+psycopg://reader:reader-pass@localhost/db")
    monkeypatch.setenv("DATA_START", "2024-01-01")
    monkeypatch.setenv("DATA_MONTHS", "24")
    monkeypatch.setenv("QUERY_TIMEOUT_MS", "5000")
    monkeypatch.setenv("QUERY_MAX_ROWS", "50")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    settings = get_settings()
    assert settings.data_end == date(2025, 12, 1)
    assert settings.query_timeout_ms == 5000
    assert settings.query_max_rows == 50
    assert settings.application_database_url.startswith("postgresql+psycopg://reader:")
    assert settings.llm_enabled is True


def test_application_falls_back_to_setup_database_when_readonly_missing():
    settings = get_settings(database_url="sqlite:///./local.db", readonly_database_url="")
    assert settings.application_database_url == "sqlite:///./local.db"


def test_unreachable_postgres_is_not_silently_replaced_by_sqlite():
    settings = get_settings(
        database_url="postgresql+psycopg://setup:not-a-real-password@127.0.0.1:1/db",
        readonly_database_url="",
    )
    assert settings.application_database_url.startswith("postgresql+psycopg://")

    engine = make_readonly_engine(settings.application_database_url, timeout_ms=1000)
    try:
        assert engine.url.get_backend_name() == "postgresql"
        with pytest.raises(DBAPIError):
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


def test_api_key_is_not_serialized_or_represented():
    settings = get_settings()
    settings = settings.model_copy(update={"llm_api_key": "sk-very-secret"})
    assert "sk-very-secret" not in repr(settings)
    assert "sk-very-secret" not in settings.model_dump_json()


def test_sqlite_readonly_url_uses_uri_mode():
    assert _sqlite_readonly_url("sqlite:///./data.db") == "sqlite+pysqlite:///file:./data.db?mode=ro&uri=true"


def test_writable_engine_allows_writes_and_readonly_engine_does_not(tmp_path):
    database = tmp_path / "mode.db"
    writable = make_engine(f"sqlite:///{database}")
    with writable.begin() as connection:
        connection.execute(text("CREATE TABLE t (x INTEGER)"))
        connection.execute(text("INSERT INTO t (x) VALUES (1)"))
    writable.dispose()

    readonly = make_readonly_engine(f"sqlite:///{database}")
    try:
        with readonly.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM t")).scalar_one() == 1
        with pytest.raises(DBAPIError):
            with readonly.begin() as connection:
                connection.execute(text("INSERT INTO t (x) VALUES (2)"))
    finally:
        readonly.dispose()


def test_postgres_outage_is_reported_as_degraded_not_ok():
    from fastapi.testclient import TestClient

    from enterprise_business_agent.api import create_app

    settings = Settings(
        database_url="postgresql+psycopg://setup:not-a-real-password@127.0.0.1:1/db",
        readonly_database_url="",
        run_store_url="sqlite://",
        phase5_enabled=False,
        api_keys={},
        allow_anonymous_api=True,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unavailable"


def test_live_llm_config_rejects_non_anthropic_endpoint():
    settings = Settings(
        llm_provider="anthropic",
        llm_model="glm-5.3-flash",
        llm_base_url="https://open.bigmodel.cn/api/paas/v4/",
        llm_api_key="test-key-not-a-real-secret",
    )
    issues = settings.llm_endpoint_issues()
    assert any("LLM_MODEL" in issue for issue in issues)
    assert any("/v1/messages" in issue for issue in issues)
    assert all("test-key-not-a-real-secret" not in issue for issue in issues)


def test_live_llm_config_accepts_anthropic_defaults():
    settings = Settings(llm_api_key="test-key-not-a-real-secret")
    assert settings.llm_endpoint_issues() == []


def test_env_example_contains_no_real_key():
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
    assert "ANTHROPIC_API_KEY=CHANGE_ME" in text
    assert re.search(r"sk-ant-[A-Za-z0-9_-]{20,}", text) is None
    assert re.search(r"sk-[A-Za-z0-9]{20,}", text) is None
