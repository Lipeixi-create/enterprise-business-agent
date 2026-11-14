"""PostgreSQL cutover guards: missing schema must fail, never fall back."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError

from enterprise_business_agent.api.run_store import PersistentRunStore
from enterprise_business_agent.api.runtime_schema import (
    create_runtime_schema,
    drop_runtime_schema,
)

URL = os.getenv("EBA_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="EBA_TEST_POSTGRES_URL is not set"),
]


def test_runtime_store_refuses_missing_schema_instead_of_using_sqlite():
    admin = create_engine(URL, future=True)
    drop_runtime_schema(admin)
    try:
        with pytest.raises(RuntimeError) as exc:
            PersistentRunStore(URL)
        assert "scripts/init_runtime_store.py" in str(exc.value)
        assert "sqlite" not in str(exc.value).lower()
    finally:
        create_runtime_schema(admin)
        assert inspect(admin).has_table("agent_runs")
        admin.dispose()


def test_runtime_store_fails_fast_when_postgres_is_unreachable():
    with pytest.raises(OperationalError):
        PersistentRunStore("postgresql+psycopg://eba_runtime@127.0.0.1:1/eba_unreachable")
