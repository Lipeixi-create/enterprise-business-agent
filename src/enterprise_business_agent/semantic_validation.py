from __future__ import annotations

import re

from sqlalchemy import inspect

from .schemas import DimensionDefinition, MetricDefinition
from .semantic import load_dimensions, load_metrics

SAFE_EXPRESSION = re.compile(r"^[A-Za-z0-9_\s(),.*+\-/]+$")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ALLOWED_FUNCTIONS = {"SUM", "AVG", "COUNT", "NULLIF", "COALESCE", "ABS", "ROUND", "CAST", "DISTINCT", "AS"}


def _expression_identifiers(expression: str) -> set[str]:
    return {token for token in IDENTIFIER.findall(expression) if token.upper() not in ALLOWED_FUNCTIONS}


def _check_expression(prefix: str, expression: str, columns: set[str], errors: list[str]) -> None:
    if not SAFE_EXPRESSION.match(expression):
        errors.append(f"{prefix}: expression contains unsupported characters")
        return
    unknown = _expression_identifiers(expression) - columns
    if unknown:
        errors.append(f"{prefix}: unknown columns {sorted(unknown)}")


def validate_semantic_config(
    metrics: list[MetricDefinition],
    dimensions: list[DimensionDefinition],
    schema: dict[str, set[str]],
) -> list[str]:
    errors: list[str] = []
    metric_names = [item.name for item in metrics]
    dimension_names = [item.name for item in dimensions]
    if len(metric_names) != len(set(metric_names)):
        errors.append("metrics.json: duplicate canonical metric names")
    if len(dimension_names) != len(set(dimension_names)):
        errors.append("dimensions.json: duplicate dimension names")

    by_dimension = {item.name: item for item in dimensions}
    for dimension in dimensions:
        if dimension.kind == "categorical":
            if not dimension.table or dimension.table not in schema:
                errors.append(f"dimension {dimension.name}: table does not exist")
                continue
            columns = schema[dimension.table]
            for field in (dimension.key_column, dimension.code_column, dimension.name_column):
                if not field or field not in columns:
                    errors.append(f"dimension {dimension.name}: column {field!r} does not exist in {dimension.table}")
            if not dimension.fact_column:
                errors.append(f"dimension {dimension.name}: fact_column is required")
        elif dimension.kind == "time" and not dimension.time_grains:
            errors.append(f"dimension {dimension.name}: time dimension must declare time_grains")

    for metric in metrics:
        prefix = f"metric {metric.name}"
        if metric.source_table not in schema:
            errors.append(f"{prefix}: source table {metric.source_table} does not exist")
            continue
        columns = schema[metric.source_table]
        missing_fields = [field for field in metric.source_fields if field not in columns]
        if missing_fields:
            errors.append(f"{prefix}: unknown source fields {missing_fields}")
        _check_expression(f"{prefix}.numerator", metric.numerator, columns, errors)
        if metric.denominator:
            _check_expression(f"{prefix}.denominator", metric.denominator, columns, errors)
        if metric.metric_type == "ratio" and not metric.denominator:
            errors.append(f"{prefix}: ratio metric must define a denominator")
        if metric.metric_type != "ratio" and metric.denominator:
            errors.append(f"{prefix}: non-ratio metric must not define a denominator")
        if metric.aggregation not in {"sum", "sum_formula", "ratio", "weighted_average", "weighted_ratio", "count"}:
            errors.append(f"{prefix}: unsupported aggregation {metric.aggregation!r}")
        if not metric.allowed_aggregations:
            errors.append(f"{prefix}: allowed_aggregations must not be empty")
        for dimension_name in metric.dimensions:
            dimension = by_dimension.get(dimension_name)
            if dimension is None:
                errors.append(f"{prefix}: unknown dimension {dimension_name!r}")
                continue
            if dimension.kind == "categorical" and dimension.fact_column not in columns:
                errors.append(
                    f"{prefix}: dimension {dimension_name!r} requires column {dimension.fact_column!r} "
                    f"on {metric.source_table}"
                )
        time_dimension = by_dimension.get("period")
        if time_dimension and not set(metric.time_grains) <= set(time_dimension.time_grains):
            errors.append(f"{prefix}: unsupported time grain")
        if not metric.unit:
            errors.append(f"{prefix}: unit is required")
    return errors


def inspect_schema(engine) -> dict[str, set[str]]:
    inspector = inspect(engine)
    return {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in inspector.get_table_names()
    }


def validate_semantic_against_engine(engine) -> list[str]:
    return validate_semantic_config(
        load_metrics(),
        load_dimensions(),
        inspect_schema(engine),
    )
