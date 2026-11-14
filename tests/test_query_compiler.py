from datetime import date

import pytest
from sqlalchemy.dialects import postgresql

from enterprise_business_agent.analysis.contracts import DimensionFilter
from enterprise_business_agent.metric_resolver import MetricResolver
from enterprise_business_agent.query_compiler import QueryCompileError, QueryCompiler


def _compiler(fixed_dataset):
    return QueryCompiler(fixed_dataset)


def test_filter_values_are_bound_not_interpolated(fixed_dataset):
    compiler = _compiler(fixed_dataset)
    metric = compiler.metric_resolver.get("sales_amount")
    injection = "华东' OR 1=1 --"
    compiled = compiler.compile_metric(
        metric,
        date(2025, 1, 1),
        date(2025, 3, 1),
        dimensions=["region"],
        filters=[DimensionFilter(dimension="region", operator="eq", value=injection)],
    )
    assert injection not in compiled.sql
    assert injection in compiled.params.values()
    assert "GROUP BY" in compiled.sql.upper()
    assert compiled.explain("month").source_table == "sales_fact"


def test_compiled_statement_renders_for_postgresql(fixed_dataset):
    compiler = _compiler(fixed_dataset)
    metric = compiler.metric_resolver.get("gross_margin")
    compiled = compiler.compile_metric(metric, date(2025, 1, 1), date(2025, 3, 1), dimensions=["region"])
    sql = compiled.render(postgresql.dialect())
    assert "GROUP BY" in sql.upper()
    assert "sales_fact" in sql
    assert "SUM(sales_amount - cogs_amount)" in sql


def test_unsupported_dimension_is_rejected(fixed_dataset):
    compiler = _compiler(fixed_dataset)
    metric = compiler.metric_resolver.get("sales_amount")
    with pytest.raises(QueryCompileError):
        compiler.compile_metric(metric, date(2025, 1, 1), date(2025, 3, 1), dimensions=["department"])


def test_categorical_ordering_operator_is_rejected(fixed_dataset):
    compiler = _compiler(fixed_dataset)
    metric = compiler.metric_resolver.get("sales_amount")
    with pytest.raises(QueryCompileError):
        compiler.compile_metric(
            metric,
            date(2025, 1, 1),
            date(2025, 3, 1),
            filters=[DimensionFilter(dimension="region", operator="gt", value="EC")],
        )


def test_unknown_filter_dimension_is_rejected(fixed_dataset):
    compiler = _compiler(fixed_dataset)
    metric = compiler.metric_resolver.get("sales_amount")
    with pytest.raises(QueryCompileError):
        compiler.compile_metric(
            metric,
            date(2025, 1, 1),
            date(2025, 3, 1),
            filters=[DimensionFilter(dimension="not_a_dimension", operator="eq", value="x")],
        )


def test_metric_resolver_returns_definition_for_canonical_name():
    definition = MetricResolver().get("revenue")
    assert definition is not None
    assert definition.source_table == "finance_monthly"
