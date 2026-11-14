from __future__ import annotations

import pytest

from enterprise_business_agent.security import GuardRejection, QueryErrorCode, SQLGuard


@pytest.fixture()
def guard() -> SQLGuard:
    return SQLGuard(max_rows=10, max_offset=100)


def assert_rejected(guard: SQLGuard, sql: str, code: QueryErrorCode, params=None, dialect="sqlite"):
    with pytest.raises(GuardRejection) as exc:
        guard.validate(sql, params=params, dialect=dialect)
    assert exc.value.code == code, exc.value.to_dict()


def test_allows_aggregate_select_with_named_parameters(guard):
    query = guard.validate(
        "SELECT region_id, SUM(sales_amount) AS total FROM sales_fact "
        "WHERE period >= :start GROUP BY region_id ORDER BY total DESC LIMIT 5",
        params={"start": "2025-01-01"},
    )
    assert query.limit == 5
    assert query.tables == ("sales_fact",)
    assert ":start" in query.sql
    assert query.sql_hash


def test_allows_read_only_cte(guard):
    query = guard.validate(
        "WITH recent AS (SELECT region_id, sales_amount FROM sales_fact WHERE period >= :start) "
        "SELECT region_id, SUM(sales_amount) AS total FROM recent GROUP BY region_id",
        params={"start": "2025-01-01"},
    )
    assert "WITH recent" in query.sql
    assert query.tables == ("sales_fact",)


def test_allows_count_star_without_select_star(guard):
    query = guard.validate(
        "SELECT COUNT(*) AS n FROM sales_fact WHERE period >= :start", params={"start": "2025-01-01"}
    )
    assert query.sql.endswith("LIMIT 11")


def test_clamps_oversized_limit_to_hard_maximum(guard):
    query = guard.validate("SELECT region_id FROM sales_fact LIMIT 9999")
    assert query.limit == 11
    assert "LIMIT 11" in query.sql


def test_rejects_ddl_dml_and_multiple_statements(guard):
    for sql, code in [
        ("DROP TABLE sales_fact", QueryErrorCode.FORBIDDEN_STATEMENT),
        ("ALTER TABLE sales_fact ADD COLUMN x INT", QueryErrorCode.FORBIDDEN_STATEMENT),
        ("CREATE TABLE x (id INT)", QueryErrorCode.FORBIDDEN_STATEMENT),
        ("TRUNCATE TABLE sales_fact", QueryErrorCode.FORBIDDEN_STATEMENT),
        ("INSERT INTO sales_fact (region_id) VALUES (:x)", QueryErrorCode.FORBIDDEN_STATEMENT),
        ("UPDATE sales_fact SET region_id = :x", QueryErrorCode.FORBIDDEN_STATEMENT),
        ("DELETE FROM sales_fact", QueryErrorCode.FORBIDDEN_STATEMENT),
        ("PRAGMA query_only", QueryErrorCode.FORBIDDEN_STATEMENT),
        ("SELECT 1; DROP TABLE sales_fact", QueryErrorCode.MULTIPLE_STATEMENTS),
    ]:
        assert_rejected(guard, sql, code, params={"x": 1} if ":x" in sql else None)


def test_rejects_unknown_table_and_schema(guard):
    assert_rejected(guard, "SELECT region_id FROM not_a_table", QueryErrorCode.UNKNOWN_TABLE)
    assert_rejected(guard, "SELECT region_id FROM evil.sales_fact", QueryErrorCode.UNKNOWN_TABLE)


def test_rejects_unknown_column_and_select_star(guard):
    assert_rejected(guard, "SELECT nope FROM sales_fact", QueryErrorCode.UNKNOWN_COLUMN)
    assert_rejected(guard, "SELECT s.nope FROM sales_fact AS s", QueryErrorCode.UNKNOWN_COLUMN)
    assert_rejected(guard, "SELECT * FROM sales_fact", QueryErrorCode.SELECT_STAR)


def test_rejects_unbound_literals_and_positional_parameters(guard):
    assert_rejected(guard, "SELECT region_id FROM sales_fact WHERE region_id = 1", QueryErrorCode.UNBOUND_VALUE)
    assert_rejected(guard, "SELECT region_id FROM sales_fact WHERE region_id = %s", QueryErrorCode.INVALID_SQL)


def test_rejects_dangerous_functions_and_constructs(guard):
    assert_rejected(guard, "SELECT pg_sleep(1) FROM sales_fact", QueryErrorCode.FORBIDDEN_FUNCTION)
    assert_rejected(guard, "SELECT region_id FROM sales_fact FOR UPDATE", QueryErrorCode.FORBIDDEN_STATEMENT)
    assert_rejected(guard, "SELECT region_id INTO copy FROM sales_fact", QueryErrorCode.FORBIDDEN_STATEMENT)
    assert_rejected(guard, "SELECT region_id FROM sales_fact -- bypass", QueryErrorCode.INVALID_SQL)


def test_rejects_offset_beyond_limit(guard):
    assert_rejected(guard, "SELECT region_id FROM sales_fact OFFSET 1000", QueryErrorCode.OFFSET_EXCEEDED)


def test_rejects_missing_and_unused_parameters(guard):
    assert_rejected(
        guard,
        "SELECT region_id FROM sales_fact WHERE period >= :start",
        QueryErrorCode.INVALID_PARAMETER,
    )
    assert_rejected(
        guard,
        "SELECT region_id FROM sales_fact",
        QueryErrorCode.INVALID_PARAMETER,
        params={"unused": 1},
    )
    assert_rejected(
        guard,
        "SELECT region_id FROM sales_fact WHERE region_id = :ids",
        QueryErrorCode.INVALID_PARAMETER,
        params={"ids": [1, 2]},
    )


def test_renders_named_binds_for_postgres_dialect(guard):
    query = guard.validate(
        "SELECT region_id FROM sales_fact WHERE period >= :start LIMIT 5",
        params={"start": "2025-01-01"},
        dialect="postgres",
    )
    assert ":start" in query.sql
    assert "%(" not in query.sql
    assert query.dialect == "postgres"


def test_rejects_unsupported_dialect(guard):
    assert_rejected(guard, "SELECT region_id FROM sales_fact", QueryErrorCode.DIALECT_ERROR, dialect="mysql")
