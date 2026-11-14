from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from enterprise_business_agent.db import make_engine, make_readonly_engine
from enterprise_business_agent.security import (
    ExecutionFailure,
    GuardedQuery,
    QueryErrorCode,
    SafeQueryExecutor,
    SecureQueryService,
    SQLGuard,
)


@pytest.fixture()
def database_file(tmp_path):
    path = tmp_path / "executor.db"
    engine = make_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE t (x INTEGER, label TEXT)"))
        connection.execute(text("INSERT INTO t (x, label) VALUES (1, 'a'), (2, 'b'), (3, 'c'), (4, 'd'), (5, 'e')"))
    yield path, engine
    engine.dispose()


def _service(engine, *, max_rows=10, timeout_ms=5000):
    guard = SQLGuard(
        allowed_tables={"t"},
        allowed_columns={"t": {"x", "label"}},
        max_rows=max_rows,
        max_offset=100,
    )
    executor = SafeQueryExecutor(engine, max_rows=max_rows, timeout_ms=timeout_ms)
    return SecureQueryService(guard, executor)


def test_executes_guarded_query_and_limits_rows(database_file):
    _, engine = database_file
    service = _service(engine, max_rows=3)
    result = service.run("SELECT x, label FROM t ORDER BY x LIMIT 2")
    assert result.row_count == 2
    assert [row["x"] for row in result.rows] == [1, 2]
    assert result.truncated is False
    assert result.readonly is True
    assert result.dialect == "sqlite"


def test_row_cap_is_enforced_beyond_sql_limit(database_file):
    _, engine = database_file
    service = _service(engine, max_rows=3)
    result = service.run("SELECT x FROM t ORDER BY x")
    assert result.row_count == 3
    assert result.truncated is True


def test_row_output_is_json_serializable(database_file):
    _, engine = database_file
    service = _service(engine)
    result = service.run("SELECT x, label FROM t ORDER BY x")
    json.dumps(result.model_dump())
    assert result.rows[0]["label"] == "a"


def test_guard_rejection_prevents_execution(database_file):
    _, engine = database_file
    service = _service(engine)
    with pytest.raises(Exception) as exc:
        service.run("DELETE FROM t")
    assert getattr(exc.value, "code") == QueryErrorCode.FORBIDDEN_STATEMENT


def test_database_layer_rejects_writes_even_without_guard(database_file):
    path, _ = database_file
    readonly = make_readonly_engine(f"sqlite:///{path}")
    try:
        with pytest.raises(DBAPIError):
            with readonly.begin() as connection:
                connection.execute(text("CREATE TABLE should_fail (x INTEGER)"))
    finally:
        readonly.dispose()


def test_readonly_engine_can_still_read(database_file):
    path, _ = database_file
    readonly = make_readonly_engine(f"sqlite:///{path}")
    try:
        executor = SafeQueryExecutor(readonly, max_rows=10, timeout_ms=5000)
        result = executor.execute(
            GuardedQuery(
                sql="SELECT x FROM t ORDER BY x LIMIT 2",
                params={},
                tables=("t",),
                columns=("x",),
                limit=2,
                offset=0,
                dialect="sqlite",
                sql_hash="hash",
                original_sql="SELECT x FROM t ORDER BY x LIMIT 2",
            )
        )
        assert [row["x"] for row in result.rows] == [1, 2]
    finally:
        readonly.dispose()


def test_timeout_is_mapped_to_structured_error(database_file):
    _, engine = database_file
    executor = SafeQueryExecutor(engine, max_rows=10, timeout_ms=100)
    query = GuardedQuery(
        sql=("WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt) SELECT COUNT(*) AS n FROM cnt"),
        params={},
        tables=(),
        columns=("x",),
        limit=10,
        offset=0,
        dialect="sqlite",
        sql_hash="hash",
        original_sql="WITH RECURSIVE ...",
    )
    with pytest.raises(ExecutionFailure) as exc:
        executor.execute(query)
    assert exc.value.code == QueryErrorCode.TIMEOUT


def test_database_error_is_mapped_to_structured_error(database_file):
    _, engine = database_file
    executor = SafeQueryExecutor(engine, max_rows=10, timeout_ms=5000)
    query = GuardedQuery(
        sql="SELECT missing_column FROM t",
        params={},
        tables=("t",),
        columns=("missing_column",),
        limit=10,
        offset=0,
        dialect="sqlite",
        sql_hash="hash",
        original_sql="SELECT missing_column FROM t",
    )
    with pytest.raises(ExecutionFailure) as exc:
        executor.execute(query)
    assert exc.value.code == QueryErrorCode.DATABASE_ERROR


def test_executor_rejects_non_select_prefix(database_file):
    _, engine = database_file
    executor = SafeQueryExecutor(engine, max_rows=10, timeout_ms=5000)
    query = GuardedQuery(
        sql="DELETE FROM t",
        params={},
        tables=("t",),
        columns=(),
        limit=10,
        offset=0,
        dialect="sqlite",
        sql_hash="hash",
        original_sql="DELETE FROM t",
    )
    with pytest.raises(ExecutionFailure) as exc:
        executor.execute(query)
    assert exc.value.code == QueryErrorCode.READ_ONLY_VIOLATION


def test_audit_record_contains_no_sql_or_parameter_values(database_file):
    _, engine = database_file
    records: list[dict] = []
    guard = SQLGuard(
        allowed_tables={"t"},
        allowed_columns={"t": {"x", "label"}},
        max_rows=10,
        max_offset=100,
    )
    executor = SafeQueryExecutor(
        engine,
        max_rows=10,
        timeout_ms=5000,
        audit=records.append,
    )
    SecureQueryService(guard, executor).run(
        "SELECT x FROM t WHERE label = :label",
        {"label": "secret-value"},
    )
    assert len(records) == 1
    payload = records[0]
    assert payload["status"] == "ok"
    assert "sql" not in payload
    assert "secret-value" not in json.dumps(payload)
    assert payload["sql_hash"]


def test_ping(database_file):
    _, engine = database_file
    assert SafeQueryExecutor(engine).ping() is True
