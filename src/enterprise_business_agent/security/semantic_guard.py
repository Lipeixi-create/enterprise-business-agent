from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Column, Table
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import ColumnClause, Label, TextClause
from sqlalchemy.sql.selectable import Select

from ..analysis.contracts import DimensionFilter
from ..metric_resolver import DimensionRegistry, MetricResolver
from ..query_compiler import CompiledQuery
from .errors import GuardRejection, QueryErrorCode


_ALLOWED_OPERATORS = {"eq", "ne", "in", "gt", "gte", "lt", "lte"}
_CATEGORICAL_OPERATORS = {"eq", "ne", "in"}
_FORBIDDEN_SQL_TOKENS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "create",
    "grant",
    "revoke",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "into",
)


@dataclass(frozen=True)
class TrustedSelect:
    statement: Select
    params: dict[str, Any]
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    sql_hash: str
    dialect: str


class SemanticQueryGuard:
    """Validate the structured query contract produced by QueryCompiler.

    SQL Guard remains the boundary for the Phase 3 Text-to-SQL path. The
    semantic path does not accept SQL text at all: it accepts a CompiledQuery
    built from metrics.json and dimensions.json. This guard independently
    re-resolves the metric, dimensions and filters, rejects tampered
    definitions, and verifies that the SQLAlchemy statement only reads the
    expected trusted tables and columns with bound parameters.
    """

    def __init__(
        self,
        metric_resolver: MetricResolver | None = None,
        dimension_registry: DimensionRegistry | None = None,
    ):
        self.metric_resolver = metric_resolver or MetricResolver()
        self.dimension_registry = dimension_registry or DimensionRegistry()

    def _reject(self, code: QueryErrorCode, message: str, detail: dict[str, Any] | None = None) -> None:
        raise GuardRejection(code, message, detail)

    def validate(self, compiled: CompiledQuery) -> TrustedSelect:
        metric = self.metric_resolver.get(compiled.metric.name)
        if metric is None:
            self._reject(
                QueryErrorCode.UNKNOWN_TABLE,
                f"metric {compiled.metric.name!r} is not in the trusted semantic layer",
            )
        if metric.model_dump() != compiled.metric.model_dump():
            self._reject(
                QueryErrorCode.FORBIDDEN_EXPRESSION,
                "compiled metric definition does not match the trusted semantic definition",
                {"metric": compiled.metric.name},
            )

        statement = compiled.statement
        if not isinstance(statement, Select):
            self._reject(
                QueryErrorCode.READ_ONLY_VIOLATION,
                "structured query must be a SQLAlchemy Select statement",
            )

        if not compiled.sql.lstrip().upper().startswith("SELECT"):
            self._reject(QueryErrorCode.READ_ONLY_VIOLATION, "compiled SQL must start with SELECT")
        if any(token in compiled.sql for token in (";", "--", "/*", "*/")):
            self._reject(QueryErrorCode.INVALID_SQL, "compiled SQL contains a forbidden separator or comment")
        lowered = compiled.sql.lower()
        for token in _FORBIDDEN_SQL_TOKENS:
            if token in lowered:
                self._reject(
                    QueryErrorCode.FORBIDDEN_STATEMENT,
                    f"compiled SQL contains a forbidden token: {token}",
                )

        if compiled.start > compiled.end:
            self._reject(QueryErrorCode.INVALID_SQL, "compiled time range is inverted")
        if compiled.dialect_name not in {"sqlite", "postgres", "postgresql"}:
            self._reject(QueryErrorCode.DIALECT_ERROR, f"unsupported compiled dialect: {compiled.dialect_name}")

        dimensions = []
        for name in compiled.dimensions:
            dimension = self.dimension_registry.get(name)
            if dimension is None:
                self._reject(QueryErrorCode.UNKNOWN_COLUMN, f"unknown dimension: {name}")
            if dimension.kind == "time":
                self._reject(QueryErrorCode.FORBIDDEN_EXPRESSION, "time cannot be used as a group dimension")
            if dimension.name not in metric.dimensions:
                self._reject(
                    QueryErrorCode.UNKNOWN_COLUMN,
                    f"dimension {dimension.name!r} is not supported by metric {metric.name!r}",
                )
            dimensions.append(dimension)

        table_columns: dict[str, set[str]] = {
            metric.source_table: set(metric.source_fields)
            | {"period"}
            | {item.fact_column for item in dimensions if item.fact_column}
        }
        expected_tables = {metric.source_table}
        for dimension in dimensions:
            if dimension.table:
                expected_tables.add(dimension.table)
                table_columns.setdefault(dimension.table, set()).update(
                    {
                        value
                        for value in (dimension.key_column, dimension.code_column, dimension.name_column)
                        if value
                    }
                )

        for item in compiled.filters:
            dimension = self._validate_filter(item)
            if dimension.kind == "time":
                continue
            if dimension.name not in metric.dimensions:
                self._reject(
                    QueryErrorCode.UNKNOWN_COLUMN,
                    f"filter dimension {dimension.name!r} is not supported by metric {metric.name!r}",
                )
            if dimension.table:
                expected_tables.add(dimension.table)
                table_columns.setdefault(dimension.table, set()).update(
                    {
                        value
                        for value in (dimension.key_column, dimension.code_column, dimension.name_column)
                        if value
                    }
                )
                table_columns[metric.source_table].add(dimension.fact_column or "")

        actual_tables = {
            str(node.name).lower()
            for node in visitors.iterate(statement)
            if isinstance(node, Table) and node.name
        }
        expected_tables = {item.lower() for item in expected_tables if item}
        if actual_tables != expected_tables:
            self._reject(
                QueryErrorCode.UNKNOWN_TABLE,
                "compiled statement reads tables outside the trusted metric contract",
                {"actual": sorted(actual_tables), "expected": sorted(expected_tables)},
            )

        selected = {str(key) for key in statement.selected_columns.keys()}
        allowed_selected = {"period", "numerator_value", "denominator_value"}
        for dimension in dimensions:
            allowed_selected.add(dimension.name)
            allowed_selected.add(f"{dimension.name}__label")
        if not selected.issubset(allowed_selected):
            self._reject(
                QueryErrorCode.UNKNOWN_COLUMN,
                "compiled statement selects columns outside the trusted contract",
                {"selected": sorted(selected), "allowed": sorted(allowed_selected)},
            )
        if not {"period", "numerator_value"}.issubset(selected):
            self._reject(
                QueryErrorCode.UNKNOWN_COLUMN,
                "compiled statement is missing required period or numerator columns",
            )

        for node in visitors.iterate(statement):
            if isinstance(node, TextClause):
                self._reject(QueryErrorCode.FORBIDDEN_EXPRESSION, "raw SQL text is not allowed in a structured query")
            if isinstance(node, Label):
                continue
            if isinstance(node, Column):
                table_name = str(node.table.name).lower() if node.table is not None else ""
                name = str(node.name).lower()
                allowed = table_columns.get(table_name, set())
                if table_name not in expected_tables or name not in {value.lower() for value in allowed if value}:
                    self._reject(
                        QueryErrorCode.UNKNOWN_COLUMN,
                        f"column {table_name}.{name} is outside the trusted metric contract",
                    )
            elif isinstance(node, ColumnClause):
                name = str(node.name or "").strip()
                trusted_expressions = {metric.numerator.strip()}
                if metric.denominator:
                    trusted_expressions.add(metric.denominator.strip())
                if name not in trusted_expressions:
                    self._reject(
                        QueryErrorCode.FORBIDDEN_EXPRESSION,
                        f"untrusted column expression: {name[:80]}",
                    )

        self._validate_params(compiled)
        sql_hash = hashlib.sha256(compiled.sql.encode("utf-8")).hexdigest()
        return TrustedSelect(
            statement=statement,
            params=dict(compiled.params),
            tables=tuple(sorted(actual_tables)),
            columns=tuple(sorted(selected)),
            sql_hash=sql_hash,
            dialect=compiled.dialect_name,
        )

    def _validate_filter(self, item: DimensionFilter):
        dimension = self.dimension_registry.get(item.dimension)
        if dimension is None:
            self._reject(QueryErrorCode.UNKNOWN_COLUMN, f"unknown filter dimension: {item.dimension}")
        if item.operator not in _ALLOWED_OPERATORS:
            self._reject(QueryErrorCode.INVALID_PARAMETER, f"unsupported filter operator: {item.operator}")
        if dimension.kind != "time" and item.operator not in _CATEGORICAL_OPERATORS:
            self._reject(
                QueryErrorCode.INVALID_PARAMETER,
                f"operator {item.operator!r} is not allowed for categorical dimension {dimension.name!r}",
            )
        values = item.value if isinstance(item.value, list) else [item.value]
        if not values:
            self._reject(QueryErrorCode.INVALID_PARAMETER, f"filter {item.dimension!r} has no values")
        for value in values:
            if not _is_scalar(value):
                self._reject(
                    QueryErrorCode.INVALID_PARAMETER,
                    f"filter {item.dimension!r} contains a non-scalar value",
                )
        return dimension

    def _validate_params(self, compiled: CompiledQuery) -> None:
        if "start" not in compiled.params or "end" not in compiled.params:
            self._reject(QueryErrorCode.INVALID_PARAMETER, "compiled query is missing start/end bind parameters")
        allowed_names = {"start", "end"}
        for index, item in enumerate(compiled.filters):
            allowed_names.add(f"filter_{index}")
            if item.operator == "in" and self.dimension_registry.get(item.dimension).kind != "time":
                allowed_names.update({f"filter_{index}_code", f"filter_{index}_name"})
        extra = sorted(set(compiled.params) - allowed_names)
        if extra:
            self._reject(
                QueryErrorCode.INVALID_PARAMETER,
                "compiled query contains unexpected bind parameters",
                {"parameters": extra},
            )
        for name, value in compiled.params.items():
            if isinstance(value, (list, tuple)):
                if not value or not all(_is_scalar(item) for item in value):
                    self._reject(
                        QueryErrorCode.INVALID_PARAMETER,
                        f"bind parameter {name!r} must be a non-empty list of scalars",
                    )
            elif not _is_scalar(value):
                self._reject(
                    QueryErrorCode.INVALID_PARAMETER,
                    f"bind parameter {name!r} must be a scalar",
                )


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(
        value,
        (str, int, float, bool, date, datetime, Decimal),
    )
