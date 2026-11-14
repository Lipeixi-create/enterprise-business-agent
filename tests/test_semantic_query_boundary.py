from __future__ import annotations

import dataclasses
from datetime import date

import pytest
from sqlalchemy import delete, literal_column, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.selectable import Select

from enterprise_business_agent.agent import AnalysisGateway, ToolRegistry
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.query_compiler import QueryCompiler
from enterprise_business_agent.security import (
    AuthorizationPolicy,
    ExecutionFailure,
    GuardRejection,
    Principal,
    QueryErrorCode,
    SafeQueryExecutor,
    SemanticQueryGuard,
    SQLGuard,
    TrustedSelect,
)
from enterprise_business_agent.semantic import load_metrics


ANALYST = Principal("key:test", "analyst", frozenset({"metrics:read", "agent:run"}))


class RecordingExecutor:
    def __init__(self, real: SafeQueryExecutor):
        self.real = real
        self.calls: list[TrustedSelect] = []

    def execute_select(self, trusted: TrustedSelect):
        self.calls.append(trusted)
        return self.real.execute_select(trusted)


@pytest.fixture()
def boundary_env(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        query_max_rows=200,
        query_timeout_ms=5_000,
        agent_tool_timeout_seconds=5,
        agent_tool_max_retries=0,
    )
    path = tmp_path / "boundary.db"
    write_engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    read_engine = make_readonly_engine(f"sqlite:///{path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=read_engine, class_=Session, expire_on_commit=False)
    real_executor = SafeQueryExecutor(
        read_engine,
        max_rows=settings.query_max_rows,
        timeout_ms=settings.query_timeout_ms,
    )
    recorder = RecordingExecutor(real_executor)
    gateway = AnalysisGateway(
        factory,
        settings,
        query_executor=recorder,
        semantic_guard=SemanticQueryGuard(),
    )
    registry = ToolRegistry(gateway, AuthorizationPolicy(settings), settings)
    try:
        yield {
            "settings": settings,
            "factory": factory,
            "read_engine": read_engine,
            "gateway": gateway,
            "registry": registry,
            "recorder": recorder,
            "guard": gateway.semantic_guard,
        }
    finally:
        read_engine.dispose()
        write_engine.dispose()


def _compile(env, metric_name="sales_amount", *, dimensions=None, filters=None, start=None, end=None):
    with env["factory"]() as session:
        compiler = QueryCompiler(session)
        metric = compiler.metric_resolver.get(metric_name)
        assert metric is not None
        return compiler.compile_metric(
            metric,
            start or date(2025, 1, 1),
            end or date(2025, 3, 1),
            dimensions if dimensions is not None else ["region"],
            filters or [],
        )


def test_sql_guard_cannot_parse_compiler_positional_parameters(boundary_env):
    compiled = _compile(boundary_env)
    with pytest.raises(GuardRejection) as exc:
        SQLGuard().validate(compiled.sql, compiled.params, "sqlite")
    assert exc.value.code == QueryErrorCode.INVALID_PARAMETER


def test_semantic_guard_accepts_every_metric_compiled_query(boundary_env):
    guard = boundary_env["guard"]
    metrics = load_metrics()
    accepted = 0
    with boundary_env["factory"]() as session:
        compiler = QueryCompiler(session)
        for metric in metrics:
            dimensions = [item for item in metric.dimensions if item != "time"][:1]
            filters = []
            if "region" in metric.dimensions:
                from enterprise_business_agent.analysis.contracts import DimensionFilter

                filters = [DimensionFilter(dimension="region", operator="eq", value="EC")]
                dimensions = ["region"]
            compiled = compiler.compile_metric(
                metric,
                date(2025, 1, 1),
                date(2025, 3, 1),
                dimensions,
                filters,
            )
            trusted = guard.validate(compiled)
            assert isinstance(trusted.statement, Select)
            assert "sales_fact" in trusted.tables or metric.source_table in trusted.tables
            accepted += 1
    assert accepted == len(metrics)


def test_all_data_tools_execute_through_the_structured_executor(boundary_env):
    registry = boundary_env["registry"]
    cases = [
        ("query_metric", {"metric": "sales_amount", "start": "2025-03-01", "end": "2025-03-01"}),
        ("compare_periods", {"metric": "sales_amount", "kind": "mom", "period": "2025-03-01"}),
        (
            "top_n",
            {
                "metric": "sales_amount",
                "dimension": "product",
                "start": "2025-03-01",
                "end": "2025-03-01",
                "n": 3,
            },
        ),
        (
            "contribution",
            {
                "metric": "sales_amount",
                "dimension": "product",
                "current_start": "2025-03-01",
                "current_end": "2025-03-01",
                "baseline_start": "2025-02-01",
                "baseline_end": "2025-02-01",
            },
        ),
        ("trend", {"metric": "sales_amount", "start": "2025-01-01", "end": "2025-03-01"}),
        (
            "anomaly_scan",
            {
                "metric": "sales_amount",
                "start": "2025-01-01",
                "end": "2025-03-01",
                "method": "mom_threshold",
                "threshold": 0.01,
            },
        ),
    ]
    for index, (tool, arguments) in enumerate(cases):
        result = registry.execute(tool, arguments, ANALYST, call_id=f"boundary-{index}")
        assert result.status == "ok", (tool, result.status, result.error)
        assert result.evidence

    assert len(boundary_env["recorder"].calls) >= len(cases)
    for trusted in boundary_env["recorder"].calls:
        assert trusted.tables
        assert trusted.sql_hash
        assert isinstance(trusted.statement, Select)


def test_guard_rejects_tampered_metric_definition(boundary_env):
    compiled = _compile(boundary_env)
    tampered_metric = compiled.metric.model_copy(
        update={"numerator": "SUM(sales_amount) + 1"}
    )
    tampered = dataclasses.replace(compiled, metric=tampered_metric)
    with pytest.raises(GuardRejection) as exc:
        boundary_env["guard"].validate(tampered)
    assert exc.value.code == QueryErrorCode.FORBIDDEN_EXPRESSION


def test_guard_rejects_non_select_statement(boundary_env):
    compiled = _compile(boundary_env)
    sales = Base.metadata.tables["sales_fact"]
    forged = dataclasses.replace(
        compiled,
        statement=delete(sales).where(sales.c.period == date(2025, 1, 1)),
    )
    with pytest.raises(GuardRejection) as exc:
        boundary_env["guard"].validate(forged)
    assert exc.value.code == QueryErrorCode.READ_ONLY_VIOLATION


def test_guard_rejects_statement_reading_another_table(boundary_env):
    compiled = _compile(boundary_env)
    hr = Base.metadata.tables["hr_monthly"]
    forged_statement = (
        select(
            hr.c.period.label("period"),
            literal_column("SUM(labor_cost)").label("numerator_value"),
            literal_column("NULL").label("denominator_value"),
        )
        .select_from(hr)
    )
    forged = dataclasses.replace(compiled, statement=forged_statement)
    with pytest.raises(GuardRejection) as exc:
        boundary_env["guard"].validate(forged)
    assert exc.value.code in {
        QueryErrorCode.UNKNOWN_TABLE,
        QueryErrorCode.FORBIDDEN_EXPRESSION,
    }


def test_guard_rejects_untrusted_column_expression(boundary_env):
    compiled = _compile(boundary_env, dimensions=[])
    sales = Base.metadata.tables["sales_fact"]
    forged_statement = select(
        sales.c.period.label("period"),
        literal_column("password").label("numerator_value"),
        literal_column("NULL").label("denominator_value"),
    ).select_from(sales)
    forged = dataclasses.replace(compiled, statement=forged_statement)
    with pytest.raises(GuardRejection) as exc:
        boundary_env["guard"].validate(forged)
    assert exc.value.code == QueryErrorCode.FORBIDDEN_EXPRESSION


def test_guard_rejects_raw_text_clause(boundary_env):
    compiled = _compile(boundary_env, dimensions=[])
    sales = Base.metadata.tables["sales_fact"]
    forged_statement = (
        select(
            sales.c.period.label("period"),
            literal_column("SUM(sales_amount)").label("numerator_value"),
            literal_column("NULL").label("denominator_value"),
        )
        .select_from(sales)
        .where(text("1=1"))
    )
    forged = dataclasses.replace(compiled, statement=forged_statement)
    with pytest.raises(GuardRejection):
        boundary_env["guard"].validate(forged)


def test_guard_rejects_non_scalar_bind_parameter(boundary_env):
    compiled = _compile(boundary_env)
    forged = dataclasses.replace(
        compiled,
        params={**compiled.params, "start": {"nested": "value"}},
    )
    with pytest.raises(GuardRejection) as exc:
        boundary_env["guard"].validate(forged)
    assert exc.value.code == QueryErrorCode.INVALID_PARAMETER


def test_executor_rejects_non_select_even_if_trusted_object_is_forged(boundary_env):
    trusted = TrustedSelect(
        statement=text("DELETE FROM sales_fact"),
        params={},
        tables=("sales_fact",),
        columns=(),
        sql_hash="forged",
        dialect="sqlite",
    )
    with pytest.raises(ExecutionFailure) as exc:
        boundary_env["recorder"].real.execute_select(trusted)
    assert exc.value.code == QueryErrorCode.READ_ONLY_VIOLATION


def test_executor_enforces_row_limit(boundary_env):
    compiled = _compile(boundary_env)
    trusted = boundary_env["guard"].validate(compiled)
    executor = SafeQueryExecutor(
        boundary_env["read_engine"],
        max_rows=1,
        timeout_ms=5_000,
    )
    result = executor.execute_select(trusted)
    assert result.truncated is True
    assert result.row_count == 1


def test_filter_injection_is_bound_not_executed(boundary_env):
    from enterprise_business_agent.analysis.contracts import DimensionFilter

    injection = "EC' OR 1=1 --"
    compiled = _compile(
        boundary_env,
        dimensions=["region"],
        filters=[DimensionFilter(dimension="region", operator="eq", value=injection)],
    )
    assert injection not in compiled.sql
    assert injection in compiled.params.values()
    trusted = boundary_env["guard"].validate(compiled)
    result = boundary_env["recorder"].real.execute_select(trusted)
    assert result.row_count == 0
