from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import AnalysisGateway, ToolRegistry
from enterprise_business_agent.analysis.contracts import PeriodComparisonRequest, TimeGrain
from enterprise_business_agent.analysis.engine import compare_periods, query_metric
from enterprise_business_agent.analysis.contracts import MetricQueryRequest
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_readonly_engine
from enterprise_business_agent.security import (
    AuthorizationPolicy,
    ExecutionFailure,
    GuardedQuery,
    Principal,
    QueryErrorCode,
    SQLGuard,
    SafeQueryExecutor,
    SecureQueryService,
)

URL = os.getenv("EBA_TEST_POSTGRES_URL")
READONLY_URL = os.getenv("EBA_TEST_POSTGRES_READONLY_URL")
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not URL, reason="EBA_TEST_POSTGRES_URL is not set"),
]


@pytest.fixture(scope="module")
def postgres_session():
    engine = create_engine(URL, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = Session(engine)
    generate_data(session, date(2024, 1, 1), 24, reset=False)
    session.commit()
    yield session
    session.close()
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_postgres_semantic_query_chain(postgres_session):
    result = query_metric(
        postgres_session,
        MetricQueryRequest(
            metric="sales_amount",
            start=date(2025, 12, 1),
            end=date(2025, 12, 1),
            dimensions=["region"],
            grain=TimeGrain.MONTH,
        ),
    )
    assert result.status.value == "ok"
    assert len(result.points) == 4
    assert result.explain.dialect == "postgresql"


def test_postgres_comparison_values_match_sqlite_demo(postgres_session):
    result = compare_periods(
        postgres_session,
        PeriodComparisonRequest(metric="net_profit", kind="qoq", year=2025, quarter=4),
    )
    assert result.status.value == "ok"
    item = result.items[0]
    assert str(item.current_value) == "64875265.760000"
    assert str(item.baseline_value) == "83681849.060000"


def test_postgres_guarded_readonly_execution():
    engine = make_readonly_engine(URL, timeout_ms=5000)
    try:
        service = SecureQueryService(
            SQLGuard(max_rows=10, max_offset=100),
            SafeQueryExecutor(engine, max_rows=10, timeout_ms=5000),
        )
        result = service.run(
            "SELECT regions.code AS region, SUM(sales_amount) AS total "
            "FROM sales_fact JOIN regions ON sales_fact.region_id = regions.id "
            "WHERE sales_fact.period >= :start GROUP BY regions.code ORDER BY total DESC",
            {"start": date(2025, 12, 1)},
            dialect="postgres",
        )
        assert result.row_count == 4
        assert result.dialect == "postgres"
        assert result.readonly is True
        assert {row["region"] for row in result.rows} == {"EC", "NC", "SC", "WC"}
    finally:
        engine.dispose()


def test_postgres_readonly_engine_rejects_writes():
    engine = make_readonly_engine(URL, timeout_ms=5000)
    try:
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE should_not_exist (id INTEGER)"))
    finally:
        engine.dispose()


def test_postgres_timeout_maps_to_structured_error():
    engine = make_readonly_engine(URL, timeout_ms=5000)
    try:
        executor = SafeQueryExecutor(engine, max_rows=10, timeout_ms=100)
        query = GuardedQuery(
            sql="SELECT pg_sleep(2)",
            params={},
            tables=(),
            columns=(),
            limit=10,
            offset=0,
            dialect="postgres",
            sql_hash="hash",
            original_sql="SELECT pg_sleep(2)",
        )
        with pytest.raises(ExecutionFailure) as exc:
            executor.execute(query)
        assert exc.value.code == QueryErrorCode.TIMEOUT
    finally:
        engine.dispose()


def test_independent_readonly_role_rejects_writes(postgres_session):
    if not READONLY_URL:
        pytest.skip("EBA_TEST_POSTGRES_READONLY_URL is not set")
    engine = create_engine(READONLY_URL, future=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM sales_fact")).scalar_one() > 0
            try:
                connection.execute(
                    text("INSERT INTO regions (code, name) VALUES ('ZZ', 'test')")
                )
                connection.commit()
                raised = False
            except DBAPIError:
                raised = True
                connection.rollback()
        assert raised, "read-only role was able to write"
    finally:
        engine.dispose()


def test_postgres_structured_semantic_executor(postgres_session):
    engine = make_readonly_engine(READONLY_URL or URL, timeout_ms=5000)
    settings = Settings(
        query_max_rows=200,
        query_timeout_ms=5000,
        agent_tool_timeout_seconds=5,
        agent_tool_max_retries=0,
    )
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    registry = ToolRegistry(
        AnalysisGateway(factory, settings),
        AuthorizationPolicy(settings),
        settings,
    )
    principal = Principal("key:pg", "analyst", frozenset({"metrics:read", "agent:run"}))
    try:
        result = registry.execute(
            "query_metric",
            {
                "metric": "sales_amount",
                "start": "2025-12-01",
                "end": "2025-12-01",
                "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
            },
            principal,
            call_id="pg-structured",
        )
        assert result.status == "ok"
        assert result.evidence
        assert result.evidence[0].source == "sales_fact"
    finally:
        engine.dispose()
