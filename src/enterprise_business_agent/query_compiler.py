from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import Date, MetaData, Table, and_, bindparam, literal, literal_column, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from .analysis.contracts import DimensionFilter, QueryExplain
from .analysis.time_ranges import parse_period_value
from .metric_resolver import DimensionRegistry, MetricResolver
from .schemas import DimensionDefinition, MetricDefinition


class QueryCompileError(ValueError):
    """Raised when a metric query cannot be compiled from the trusted registry."""


@dataclass
class CompiledQuery:
    metric: MetricDefinition
    statement: Select
    params: dict[str, Any]
    dimensions: list[str]
    filters: list[DimensionFilter]
    start: date
    end: date
    dialect: Any
    dialect_name: str
    sql: str
    joined_tables: list[str] = field(default_factory=list)

    def render(self, dialect=None) -> str:
        target = dialect or self.dialect
        return str(
            self.statement.compile(
                dialect=target,
                compile_kwargs={"render_postcompile": True},
            )
        )

    def explain(self, time_grain: str, aggregation: str | None = None) -> QueryExplain:
        return QueryExplain(
            metric=self.metric.name,
            source_table=self.metric.source_table,
            expression=self.metric.expression,
            numerator=self.metric.numerator,
            denominator=self.metric.denominator,
            aggregation=aggregation or self.metric.aggregation,
            dimensions=list(self.dimensions),
            filters=[
                f"{item.dimension} {item.operator} {item.value!r}"
                for item in self.filters
            ],
            time_grain=time_grain,
            dialect=self.dialect_name,
            sql=self.sql,
            params={key: value for key, value in self.params.items()},
        )


class QueryCompiler:
    """Compile trusted metric definitions into parameterized SQLAlchemy statements."""

    def __init__(
        self,
        session: Session,
        metrics: list[MetricDefinition] | None = None,
        dimensions: list[DimensionDefinition] | None = None,
    ):
        self.session = session
        bind = session.get_bind()
        self.engine = getattr(bind, "engine", bind)
        self.dialect = self.engine.dialect
        self.dialect_name = self.dialect.name
        self.metadata = MetaData()
        self._tables: dict[str, Table] = {}
        self.metric_resolver = MetricResolver(metrics)
        self.dimension_registry = DimensionRegistry(dimensions)

    def _table(self, name: str) -> Table:
        if name not in self._tables:
            self._tables[name] = Table(name, self.metadata, autoload_with=self.engine)
        return self._tables[name]

    def _resolve_dimension(self, name: str) -> DimensionDefinition:
        dimension = self.dimension_registry.resolve(name)
        if dimension is None:
            raise QueryCompileError(f"unknown dimension: {name}")
        return dimension

    def _validate_metric_table(self, metric: MetricDefinition) -> Table:
        try:
            table = self._table(metric.source_table)
        except Exception as exc:  # pragma: no cover - depends on missing physical table
            raise QueryCompileError(f"source table {metric.source_table} is not available") from exc
        missing = [field for field in metric.source_fields if field not in table.c]
        if missing:
            raise QueryCompileError(f"metric {metric.name} references missing columns: {missing}")
        if "period" not in table.c:
            raise QueryCompileError(f"source table {metric.source_table} has no period column")
        return table

    def compile_metric(
        self,
        metric: MetricDefinition,
        start: date,
        end: date,
        dimensions: list[str] | None = None,
        filters: list[DimensionFilter] | None = None,
    ) -> CompiledQuery:
        start = parse_period_value(start)
        end = parse_period_value(end)
        if start > end:
            raise QueryCompileError("start must not be later than end")
        dimensions = dimensions or []
        filters = filters or []

        requested_dimensions: list[DimensionDefinition] = []
        seen: set[str] = set()
        for name in dimensions:
            dimension = self._resolve_dimension(name)
            if dimension.kind == "time":
                raise QueryCompileError("time is controlled by time grain, not by dimensions")
            if dimension.name not in metric.dimensions:
                raise QueryCompileError(f"metric {metric.name} does not support dimension {dimension.name}")
            if dimension.name not in seen:
                seen.add(dimension.name)
                requested_dimensions.append(dimension)

        table = self._validate_metric_table(metric)
        from_clause = table
        joined: dict[str, Table] = {}

        def ensure_join(dimension: DimensionDefinition) -> Table:
            nonlocal from_clause
            if dimension.name in joined:
                return joined[dimension.name]
            if not dimension.table or not dimension.fact_column or not dimension.key_column:
                raise QueryCompileError(f"dimension {dimension.name} is not joinable")
            dimension_table = self._table(dimension.table)
            if dimension.fact_column not in table.c:
                raise QueryCompileError(
                    f"dimension {dimension.name} requires column {dimension.fact_column} on {metric.source_table}"
                )
            from_clause = from_clause.join(
                dimension_table,
                table.c[dimension.fact_column] == dimension_table.c[dimension.key_column],
            )
            joined[dimension.name] = dimension_table
            return dimension_table

        columns = [table.c.period.label("period")]
        group_columns = [table.c.period]
        for dimension in requested_dimensions:
            dimension_table = ensure_join(dimension)
            columns.append(dimension_table.c[dimension.code_column].label(dimension.name))
            columns.append(dimension_table.c[dimension.name_column].label(f"{dimension.name}__label"))
            group_columns.extend([dimension_table.c[dimension.code_column], dimension_table.c[dimension.name_column]])

        columns.append(literal_column(metric.numerator).label("numerator_value"))
        if metric.denominator:
            columns.append(literal_column(metric.denominator).label("denominator_value"))
        else:
            columns.append(literal(None).label("denominator_value"))

        params: dict[str, Any] = {"start": start, "end": end}
        clauses = [
            table.c.period >= bindparam("start", value=start, type_=Date),
            table.c.period <= bindparam("end", value=end, type_=Date),
        ]

        for index, item in enumerate(filters):
            dimension = self._resolve_dimension(item.dimension)
            if dimension.kind == "time":
                column = table.c.period
                name = f"filter_{index}"
                if item.operator == "in":
                    values = item.value if isinstance(item.value, list) else [item.value]
                    parsed = [parse_period_value(value) for value in values]
                    params[name] = parsed
                    clauses.append(column.in_(bindparam(name, value=parsed, type_=Date, expanding=True)))
                    continue
                parsed = parse_period_value(item.value)  # type: ignore[arg-type]
                params[name] = parsed
                bound = bindparam(name, value=parsed, type_=Date)
                operators = {
                    "eq": lambda col, value: col == value,
                    "ne": lambda col, value: col != value,
                    "gt": lambda col, value: col > value,
                    "gte": lambda col, value: col >= value,
                    "lt": lambda col, value: col < value,
                    "lte": lambda col, value: col <= value,
                }
                if item.operator == "in" or item.operator not in operators:
                    raise QueryCompileError(f"unsupported period operator: {item.operator}")
                clauses.append(operators[item.operator](column, bound))
                continue

            if dimension.name not in metric.dimensions:
                raise QueryCompileError(f"metric {metric.name} does not support filter dimension {dimension.name}")
            dimension_table = ensure_join(dimension)
            code_column = dimension_table.c[dimension.code_column]
            name_column = dimension_table.c[dimension.name_column]
            parameter = f"filter_{index}"
            if item.operator == "in":
                values = item.value if isinstance(item.value, list) else [item.value]
                if not values:
                    raise QueryCompileError("in filters require at least one value")
                values = [str(value) for value in values]
                params[parameter] = values
                code_param = bindparam(f"{parameter}_code", value=values, expanding=True)
                name_param = bindparam(f"{parameter}_name", value=values, expanding=True)
                params[f"{parameter}_code"] = values
                params[f"{parameter}_name"] = values
                clauses.append(or_(code_column.in_(code_param), name_column.in_(name_param)))
            elif item.operator in {"eq", "ne"}:
                value = str(item.value)
                params[parameter] = value
                bound = bindparam(parameter, value=value)
                if item.operator == "eq":
                    clauses.append(or_(code_column == bound, name_column == bound))
                else:
                    clauses.append(and_(code_column != bound, name_column != bound))
            else:
                raise QueryCompileError(
                    f"operator {item.operator!r} is only supported for the period dimension"
                )

        statement = (
            select(*columns)
            .select_from(from_clause)
            .where(*clauses)
            .group_by(*group_columns)
            .order_by(*group_columns)
        )
        sql = str(
            statement.compile(
                dialect=self.dialect,
                compile_kwargs={"render_postcompile": True},
            )
        )
        return CompiledQuery(
            metric=metric,
            statement=statement,
            params=params,
            dimensions=[item.name for item in requested_dimensions],
            filters=list(filters),
            start=start,
            end=end,
            dialect=self.dialect,
            dialect_name=self.dialect_name,
            sql=sql,
            joined_tables=sorted(joined),
        )
