from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.orm import Session

from enterprise_business_agent.analysis.contracts import TopNRequest

from ..metric_resolver import MetricResolution, MetricResolver
from ..query_compiler import CompiledQuery, QueryCompileError, QueryCompiler
from ..schemas import MetricDefinition
from .comparison import same_month_last_year
from .contracts import (
    AnalysisStatus,
    AnomalyPoint,
    AnomalyRequest,
    AnomalyResponse,
    ContributionItem,
    ContributionRequest,
    ContributionResponse,
    MetricPoint,
    MetricQueryRequest,
    MetricQueryResult,
    PeriodComparisonItem,
    PeriodComparisonRequest,
    PeriodComparisonResponse,
    TopNItem,
    TopNResponse,
    TrendPoint,
    TrendRequest,
    TrendResponse,
)
from .time_ranges import (
    bucket_key,
    bucket_label,
    month_sequence,
    parse_period_value,
    previous_quarter_start,
    quarter_label,
    quarter_months,
    shift_month,
)

ZERO = Decimal("0")
RATE_QUANTUM = Decimal("0.000001")
MONEY_QUANTUM = Decimal("0.000001")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _rate(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def _value(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass
class _Row:
    period: date
    dimensions: dict[str, str]
    labels: dict[str, str]
    numerator: Decimal | None
    denominator: Decimal | None


def _dimension_key(dimensions: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(dimensions.items()))


class AnalysisEngine:
    def __init__(
        self,
        session: Session,
        metrics: list[MetricDefinition] | None = None,
        dimensions=None,
        *,
        query_executor=None,
        semantic_guard=None,
    ):
        self.session = session
        self.resolver = MetricResolver(metrics)
        self.compiler = QueryCompiler(session, metrics, dimensions)
        self.dimension_registry = self.compiler.dimension_registry
        self.query_executor = query_executor
        self.semantic_guard = semantic_guard

    # ------------------------------------------------------------------
    # Resolution and query execution
    # ------------------------------------------------------------------
    def _resolve(self, metric_name: str) -> tuple[MetricDefinition | None, MetricResolution]:
        resolution = self.resolver.resolve(metric_name)
        return resolution.metric, resolution

    def _fetch(
        self,
        metric: MetricDefinition,
        start: date,
        end: date,
        dimensions: list[str] | None = None,
        filters=None,
        aggregation: str | None = None,
    ) -> tuple[CompiledQuery, list[_Row], set[date]]:
        compiled = self.compiler.compile_metric(metric, start, end, dimensions, filters)
        if self.query_executor is None:
            from ..observability.setup import span

            with span(
                "db.query",
                db__system=self.session.get_bind().dialect.name,
                db__tables=",".join(sorted({metric.source_table, *compiled.joined_tables})),
                db__path="semantic_session",
            ):
                row_source = self.session.execute(compiled.statement, compiled.params).mappings()
        else:
            from ..security.errors import ExecutionFailure, GuardRejection
            from ..security.semantic_guard import SemanticQueryGuard

            guard = self.semantic_guard
            if guard is None:
                guard = SemanticQueryGuard(self.resolver, self.dimension_registry)
                self.semantic_guard = guard
            try:
                trusted = guard.validate(compiled)
                execution = self.query_executor.execute_select(trusted)
            except (GuardRejection, ExecutionFailure) as exc:
                code = getattr(getattr(exc, "code", None), "value", "query_rejected")
                raise QueryCompileError(f"secure semantic query boundary rejected the request ({code})") from exc
            if execution.truncated:
                raise QueryCompileError("query result exceeded the configured row limit")
            row_source = execution.rows

        rows: list[_Row] = []
        periods: set[date] = set()
        for row in row_source:
            period_value = row["period"]
            if isinstance(period_value, datetime):
                period = period_value.date()
            elif isinstance(period_value, date):
                period = period_value
            else:
                period = parse_period_value(period_value)
            dimensions_map = {name: str(row[name]) for name in compiled.dimensions}
            labels = {
                name: str(row[f"{name}__label"]) for name in compiled.dimensions if f"{name}__label" in row.keys()
            }
            rows.append(
                _Row(
                    period=period,
                    dimensions=dimensions_map,
                    labels=labels,
                    numerator=_decimal(row["numerator_value"]),
                    denominator=_decimal(row["denominator_value"]),
                )
            )
            periods.add(period)
        return compiled, rows, periods

    # ------------------------------------------------------------------
    # Metric arithmetic
    # ------------------------------------------------------------------
    @staticmethod
    def _ratio_value(metric: MetricDefinition, numerator: Decimal, denominator: Decimal | None) -> Decimal | None:
        if denominator is None or denominator == 0:
            return None
        return _rate(numerator / denominator)

    @staticmethod
    def _effective_aggregation(metric: MetricDefinition, requested: str | None) -> str:
        if metric.metric_type == "ratio":
            return "ratio"
        if metric.additivity == "semi_additive":
            return "last"
        return requested or "sum"

    def _aggregate_rows(
        self,
        metric: MetricDefinition,
        rows: list[_Row],
        months: list[date],
        aggregation: str | None = None,
    ) -> tuple[Decimal | None, Decimal | None, Decimal | None, AnalysisStatus, str | None]:
        """Aggregate one group's monthly rows. Returns value, numerator, denominator, status, reason."""
        rows_by_month = {row.period: row for row in rows}
        missing = [month for month in months if month not in rows_by_month]
        if missing:
            return (
                None,
                None,
                None,
                AnalysisStatus.INSUFFICIENT_DATA,
                "missing months: " + ", ".join(item.isoformat() for item in missing),
            )
        ordered = [rows_by_month[month] for month in months]
        effective = self._effective_aggregation(metric, aggregation)

        if metric.metric_type == "ratio":
            if any(row.denominator is None or row.denominator == 0 for row in ordered):
                return None, None, None, AnalysisStatus.NOT_CALCULABLE, "denominator is zero or missing"
            numerators = [row.numerator or ZERO for row in ordered]
            denominator_agg = metric.denominator_aggregation
            denominators = [row.denominator or ZERO for row in ordered]
            numerator = sum(numerators, ZERO)
            if denominator_agg == "avg":
                denominator = sum(denominators, ZERO) / len(denominators)
            elif denominator_agg == "last":
                denominator = denominators[-1]
            else:
                denominator = sum(denominators, ZERO)
            value = self._ratio_value(metric, numerator, denominator)
            status = AnalysisStatus.OK if value is not None else AnalysisStatus.NOT_CALCULABLE
            return (
                _value(value),
                _value(numerator),
                _value(denominator),
                status,
                None if value is not None else "denominator is zero",
            )

        if effective == "last":
            row = ordered[-1]
            return _value(row.numerator), _value(row.numerator), None, AnalysisStatus.OK, None

        values = [row.numerator for row in ordered if row.numerator is not None]
        if not values:
            return None, None, None, AnalysisStatus.INSUFFICIENT_DATA, "no numeric values"
        if effective == "sum":
            aggregate = sum(values, ZERO)
        elif effective == "avg":
            aggregate = sum(values, ZERO) / len(values)
        elif effective == "count":
            aggregate = Decimal(len(values))
        else:
            return None, None, None, AnalysisStatus.INVALID_REQUEST, f"unsupported aggregation: {effective}"
        return _value(aggregate), _value(aggregate), None, AnalysisStatus.OK, None

    @staticmethod
    def _point_status(
        metric: MetricDefinition, value: Decimal | None, denominator: Decimal | None
    ) -> tuple[AnalysisStatus, str | None]:
        if value is not None:
            return AnalysisStatus.OK, None
        if metric.metric_type == "ratio" and (denominator is None or denominator == 0):
            return AnalysisStatus.NOT_CALCULABLE, "denominator is zero"
        return AnalysisStatus.INSUFFICIENT_DATA, "no numeric value"

    def _rollup(
        self,
        metric: MetricDefinition,
        rows: list[_Row],
        grain: str,
        start: date,
        end: date,
        aggregation: str | None = None,
    ) -> list[MetricPoint]:
        groups: dict[tuple[Any, tuple[tuple[str, str], ...]], list[_Row]] = defaultdict(list)
        for row in rows:
            groups[(bucket_key(row.period, grain), _dimension_key(row.dimensions))].append(row)

        points: list[MetricPoint] = []
        for (key, _), group in sorted(groups.items(), key=lambda item: str(item[0][0])):
            ordered = sorted(group, key=lambda row: row.period)
            months = sorted({row.period for row in ordered})
            value, numerator, denominator, status, reason = self._aggregate_rows(metric, ordered, months, aggregation)
            first = ordered[0]
            points.append(
                MetricPoint(
                    period=bucket_label(key, grain, start, end),
                    dimensions=dict(first.dimensions),
                    dimension_labels=dict(first.labels),
                    value=value,
                    numerator=numerator,
                    denominator=denominator,
                    unit=metric.unit,
                    status=status,
                    reason=reason,
                )
            )
        return points

    # ------------------------------------------------------------------
    # Public services
    # ------------------------------------------------------------------
    def query_metric(self, request: MetricQueryRequest) -> MetricQueryResult:
        metric, resolution = self._resolve(request.metric)
        if metric is None:
            return MetricQueryResult(
                status=self._resolution_status(resolution),
                metric=request.metric,
                label=request.metric,
                unit="",
                source_table="",
                aggregation="",
                time_grain=request.grain.value,
                start=request.start,
                end=request.end,
                reason=resolution.message or "metric resolution failed",
            )

        aggregation = self._effective_aggregation(metric, request.aggregation)
        allowed = set(metric.allowed_aggregations)
        if metric.metric_type != "ratio" and aggregation not in allowed:
            return MetricQueryResult(
                status=AnalysisStatus.INVALID_REQUEST,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                source_table=metric.source_table,
                aggregation=aggregation,
                time_grain=request.grain.value,
                start=request.start,
                end=request.end,
                reason=f"aggregation {aggregation!r} is not allowed for {metric.name}; allowed: {sorted(allowed)}",
            )

        try:
            compiled, rows, periods = self._fetch(
                metric, request.start, request.end, request.dimensions, request.filters, aggregation
            )
        except QueryCompileError as exc:
            return MetricQueryResult(
                status=AnalysisStatus.INVALID_REQUEST,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                source_table=metric.source_table,
                aggregation=aggregation,
                time_grain=request.grain.value,
                start=request.start,
                end=request.end,
                reason=str(exc),
            )

        explain = compiled.explain(request.grain.value, aggregation)
        if not rows:
            return MetricQueryResult(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                source_table=metric.source_table,
                aggregation=aggregation,
                time_grain=request.grain.value,
                start=request.start,
                end=request.end,
                reason="no rows match the requested metric, time range and filters",
                explain=explain,
            )

        expected = month_sequence(request.start, request.end)
        missing = [month for month in expected if month not in periods]
        if missing:
            return MetricQueryResult(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                source_table=metric.source_table,
                aggregation=aggregation,
                time_grain=request.grain.value,
                start=request.start,
                end=request.end,
                reason="requested time range is incomplete",
                missing_periods=[month.isoformat() for month in missing],
                explain=explain,
            )

        points = self._rollup(metric, rows, request.grain.value, request.start, request.end, aggregation)
        if not points:
            status = AnalysisStatus.INSUFFICIENT_DATA
        elif all(point.value is None for point in points):
            status = AnalysisStatus.NOT_CALCULABLE
        else:
            status = AnalysisStatus.OK
        return MetricQueryResult(
            status=status,
            metric=metric.name,
            label=metric.label,
            unit=metric.unit,
            source_table=metric.source_table,
            aggregation=aggregation,
            time_grain=request.grain.value,
            start=request.start,
            end=request.end,
            points=points,
            reason=None if status == AnalysisStatus.OK else "one or more groups are not calculable",
            explain=explain,
        )

    def compare_periods(self, request: PeriodComparisonRequest) -> PeriodComparisonResponse:
        metric, resolution = self._resolve(request.metric)
        if metric is None:
            return PeriodComparisonResponse(
                status=self._resolution_status(resolution),
                metric=request.metric,
                label=request.metric,
                kind=request.kind,
                unit="",
                current_period="",
                baseline_period="",
                reason=resolution.message or "metric resolution failed",
            )

        if request.kind == "mom":
            current_months = [request.period]
            baseline_months = [shift_month(request.period, -1)]
        elif request.kind == "yoy":
            current_months = [request.period]
            baseline_months = [same_month_last_year(request.period)]
        elif request.kind == "qoq":
            current_months = quarter_months(request.year, request.quarter)
            baseline_start = previous_quarter_start(current_months[0])
            baseline_months = quarter_months(baseline_start.year, (baseline_start.month - 1) // 3 + 1)
        elif request.kind == "yoy_year":
            current_months = [date(request.year, month, 1) for month in range(1, 13)]
            baseline_months = [date(request.year - 1, month, 1) for month in range(1, 13)]
        else:
            current_months = quarter_months(request.year, request.quarter)
            baseline_months = quarter_months(request.year - 1, request.quarter)

        current_start, current_end = current_months[0], current_months[-1]
        baseline_start, baseline_end = baseline_months[0], baseline_months[-1]
        current_label = self._comparison_label(request.kind, current_start)
        baseline_label = self._comparison_label(request.kind, baseline_start)

        try:
            current_compiled, current_rows, current_present = self._fetch(
                metric, current_start, current_end, request.dimensions, request.filters
            )
            _, baseline_rows, baseline_present = self._fetch(
                metric, baseline_start, baseline_end, request.dimensions, request.filters
            )
        except QueryCompileError as exc:
            return PeriodComparisonResponse(
                status=AnalysisStatus.INVALID_REQUEST,
                metric=metric.name,
                label=metric.label,
                kind=request.kind,
                unit=metric.unit,
                current_period=current_label,
                baseline_period=baseline_label,
                reason=str(exc),
            )

        current_missing = [month for month in current_months if month not in current_present]
        baseline_missing = [month for month in baseline_months if month not in baseline_present]
        if current_missing or baseline_missing:
            reasons = []
            if current_missing:
                reasons.append("current period missing: " + ", ".join(item.isoformat() for item in current_missing))
            if baseline_missing:
                reasons.append("baseline period missing: " + ", ".join(item.isoformat() for item in baseline_missing))
            return PeriodComparisonResponse(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                kind=request.kind,
                unit=metric.unit,
                current_period=current_label,
                baseline_period=baseline_label,
                reason="; ".join(reasons),
                explain=current_compiled.explain("month", metric.aggregation),
            )

        current_map = self._group_rows(current_rows)
        baseline_map = self._group_rows(baseline_rows)
        keys = sorted(set(current_map) | set(baseline_map))
        items: list[PeriodComparisonItem] = []
        for key in keys:
            dimensions = dict(key)
            current_group = current_map.get(key)
            baseline_group = baseline_map.get(key)
            if current_group is None or baseline_group is None:
                missing_side = "current" if current_group is None else "baseline"
                items.append(
                    PeriodComparisonItem(
                        dimensions=dimensions,
                        current_period=current_label,
                        baseline_period=baseline_label,
                        status=AnalysisStatus.INSUFFICIENT_DATA,
                        reason=f"{missing_side} group has no rows",
                    )
                )
                continue
            current_value, _, _, current_status, current_reason = self._aggregate_rows(
                metric, current_group, current_months
            )
            baseline_value, _, _, baseline_status, baseline_reason = self._aggregate_rows(
                metric, baseline_group, baseline_months
            )
            if current_status != AnalysisStatus.OK or baseline_status != AnalysisStatus.OK:
                items.append(
                    PeriodComparisonItem(
                        dimensions=dimensions,
                        current_period=current_label,
                        baseline_period=baseline_label,
                        current_value=current_value,
                        baseline_value=baseline_value,
                        status=current_status if current_status != AnalysisStatus.OK else baseline_status,
                        reason=current_reason or baseline_reason,
                    )
                )
                continue
            if baseline_value is None:
                status = AnalysisStatus.NOT_CALCULABLE
                absolute = None
                rate = None
                reason = "baseline value is missing"
            elif baseline_value == 0:
                status = AnalysisStatus.NOT_CALCULABLE
                absolute = _value(current_value - baseline_value)
                rate = None
                reason = "baseline value is zero; change rate is undefined"
            else:
                status = AnalysisStatus.OK
                absolute = _value(current_value - baseline_value)
                rate = _rate(absolute / baseline_value)
                reason = None
            items.append(
                PeriodComparisonItem(
                    dimensions=dimensions,
                    current_period=current_label,
                    baseline_period=baseline_label,
                    current_value=current_value,
                    baseline_value=baseline_value,
                    absolute_change=absolute,
                    change_rate=rate,
                    status=status,
                    reason=reason,
                )
            )

        if not items:
            status = AnalysisStatus.INSUFFICIENT_DATA
        elif all(item.status == AnalysisStatus.OK for item in items):
            status = AnalysisStatus.OK
        elif any(item.status == AnalysisStatus.OK for item in items):
            status = AnalysisStatus.OK
        elif all(item.status == AnalysisStatus.NOT_CALCULABLE for item in items):
            status = AnalysisStatus.NOT_CALCULABLE
        else:
            status = AnalysisStatus.INSUFFICIENT_DATA
        return PeriodComparisonResponse(
            status=status,
            metric=metric.name,
            label=metric.label,
            kind=request.kind,
            unit=metric.unit,
            current_period=current_label,
            baseline_period=baseline_label,
            items=items,
            explain=current_compiled.explain("month", metric.aggregation),
        )

    def get_top_n(self, request: TopNRequest) -> TopNResponse:
        metric, resolution = self._resolve(request.metric)
        dimension = self.dimension_registry.resolve(request.dimension)
        if metric is None or dimension is None or dimension.kind == "time" or dimension.name not in metric.dimensions:
            reason = (
                resolution.message
                if metric is None
                else f"dimension {request.dimension!r} is not supported by {request.metric}"
            )
            return TopNResponse(
                status=self._resolution_status(resolution) if metric is None else AnalysisStatus.INVALID_REQUEST,
                metric=request.metric,
                label=metric.label if metric else request.metric,
                dimension=request.dimension,
                unit=metric.unit if metric else "",
                start=request.start,
                end=request.end,
                reason=reason,
            )

        try:
            compiled, rows, periods = self._fetch(metric, request.start, request.end, [dimension.name], request.filters)
        except QueryCompileError as exc:
            return TopNResponse(
                status=AnalysisStatus.INVALID_REQUEST,
                metric=metric.name,
                label=metric.label,
                dimension=dimension.name,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                reason=str(exc),
            )
        missing = [month for month in month_sequence(request.start, request.end) if month not in periods]
        if missing:
            return TopNResponse(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                dimension=dimension.name,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                reason="requested time range is incomplete",
            )
        points = self._rollup(
            metric,
            rows,
            "total",
            request.start,
            request.end,
            metric.allowed_aggregations[-1] if metric.metric_type == "ratio" else None,
        )
        points = [point for point in points if point.value is not None]
        if not points:
            return TopNResponse(
                status=AnalysisStatus.NOT_CALCULABLE if rows else AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                dimension=dimension.name,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                reason="no calculable values",
                explain=compiled.explain("total", metric.aggregation),
            )
        points.sort(key=lambda point: point.value, reverse=not request.ascending)
        total = None
        if metric.metric_type == "additive":
            total = sum((point.value for point in points if point.value is not None), ZERO)
        items = []
        for index, point in enumerate(points[: request.n], start=1):
            share = None
            if total is not None and total != 0:
                share = _rate(point.value / total)
            items.append(
                TopNItem(
                    rank=index,
                    dimension_value=point.dimensions.get(dimension.name, ""),
                    dimension_label=point.dimension_labels.get(dimension.name),
                    value=point.value,
                    numerator=point.numerator,
                    denominator=point.denominator,
                    share=share,
                    unit=metric.unit,
                )
            )
        return TopNResponse(
            status=AnalysisStatus.OK,
            metric=metric.name,
            label=metric.label,
            dimension=dimension.name,
            unit=metric.unit,
            start=request.start,
            end=request.end,
            items=items,
            explain=compiled.explain("total", metric.aggregation),
        )

    def analyze_contribution(self, request: ContributionRequest) -> ContributionResponse:
        metric, resolution = self._resolve(request.metric)
        dimension = self.dimension_registry.resolve(request.dimension)
        if metric is None or dimension is None or dimension.kind == "time" or dimension.name not in metric.dimensions:
            reason = (
                resolution.message
                if metric is None
                else f"dimension {request.dimension!r} is not supported by {request.metric}"
            )
            return ContributionResponse(
                status=self._resolution_status(resolution) if metric is None else AnalysisStatus.INVALID_REQUEST,
                metric=request.metric,
                label=metric.label if metric else request.metric,
                dimension=request.dimension,
                unit=metric.unit if metric else "",
                current_start=request.current_start,
                current_end=request.current_end,
                baseline_start=request.baseline_start,
                baseline_end=request.baseline_end,
                reason=reason,
                allocatable=False,
            )
        if metric.metric_type != "additive":
            return ContributionResponse(
                status=AnalysisStatus.NOT_APPLICABLE,
                metric=metric.name,
                label=metric.label,
                dimension=dimension.name,
                unit=metric.unit,
                current_start=request.current_start,
                current_end=request.current_end,
                baseline_start=request.baseline_start,
                baseline_end=request.baseline_end,
                reason="contribution allocation requires an additive metric; ratios and semi-additive metrics are not allocatable",
                allocatable=False,
            )

        current_months = month_sequence(request.current_start, request.current_end)
        baseline_months = month_sequence(request.baseline_start, request.baseline_end)
        try:
            _, current_rows, current_present = self._fetch(
                metric, request.current_start, request.current_end, [dimension.name], request.filters
            )
            _, baseline_rows, baseline_present = self._fetch(
                metric, request.baseline_start, request.baseline_end, [dimension.name], request.filters
            )
        except QueryCompileError as exc:
            return ContributionResponse(
                status=AnalysisStatus.INVALID_REQUEST,
                metric=metric.name,
                label=metric.label,
                dimension=dimension.name,
                unit=metric.unit,
                current_start=request.current_start,
                current_end=request.current_end,
                baseline_start=request.baseline_start,
                baseline_end=request.baseline_end,
                reason=str(exc),
                allocatable=False,
            )
        current_missing = [month for month in current_months if month not in current_present]
        baseline_missing = [month for month in baseline_months if month not in baseline_present]
        if current_missing or baseline_missing:
            return ContributionResponse(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                dimension=dimension.name,
                unit=metric.unit,
                current_start=request.current_start,
                current_end=request.current_end,
                baseline_start=request.baseline_start,
                baseline_end=request.baseline_end,
                reason="one or both time ranges are incomplete",
                allocatable=False,
            )

        current_map = self._group_rows(current_rows)
        baseline_map = self._group_rows(baseline_rows)
        keys = sorted(set(current_map) | set(baseline_map))
        if not keys:
            return ContributionResponse(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                dimension=dimension.name,
                unit=metric.unit,
                current_start=request.current_start,
                current_end=request.current_end,
                baseline_start=request.baseline_start,
                baseline_end=request.baseline_end,
                reason="no rows match the requested ranges and filters",
                allocatable=False,
            )
        current_values: dict[tuple[tuple[str, str], ...], Decimal] = {}
        baseline_values: dict[tuple[tuple[str, str], ...], Decimal] = {}
        labels: dict[tuple[tuple[str, str], ...], str] = {}
        incomplete = False
        for key in keys:
            current_group = current_map.get(key)
            baseline_group = baseline_map.get(key)
            if current_group is None or baseline_group is None:
                incomplete = True
                continue
            current_value, _, _, current_status, _ = self._aggregate_rows(metric, current_group, current_months)
            baseline_value, _, _, baseline_status, _ = self._aggregate_rows(metric, baseline_group, baseline_months)
            if current_status != AnalysisStatus.OK or baseline_status != AnalysisStatus.OK:
                incomplete = True
                continue
            current_values[key] = current_value
            baseline_values[key] = baseline_value
            labels[key] = next(iter(current_group[0].labels.values()), "")

        total_current = sum(current_values.values(), ZERO) if not incomplete else None
        total_baseline = sum(baseline_values.values(), ZERO) if not incomplete else None
        total_change = (
            _value(total_current - total_baseline) if total_current is not None and total_baseline is not None else None
        )
        items: list[ContributionItem] = []
        if total_change is not None:
            for key in keys:
                if key not in current_values or key not in baseline_values:
                    items.append(
                        ContributionItem(
                            dimension_value=dict(key).get(dimension.name, ""),
                            dimension_label=labels.get(key),
                            direction="flat",
                        )
                    )
                    continue
                change = _value(current_values[key] - baseline_values[key])
                contribution_rate = _rate(change / total_change) if total_change != 0 else None
                direction = "increase" if change > 0 else "decrease" if change < 0 else "flat"
                items.append(
                    ContributionItem(
                        dimension_value=dict(key).get(dimension.name, ""),
                        dimension_label=labels.get(key),
                        current_value=_value(current_values[key]),
                        baseline_value=_value(baseline_values[key]),
                        absolute_change=change,
                        contribution=change,
                        contribution_rate=contribution_rate,
                        direction=direction,
                    )
                )
            items.sort(
                key=lambda item: abs(item.contribution) if item.contribution is not None else Decimal("-1"),
                reverse=True,
            )
            if request.top_n is not None:
                items = items[: request.top_n]
        sum_contributions = (
            sum((item.contribution for item in items if item.contribution is not None), ZERO)
            if total_change is not None
            else None
        )
        residual = (
            _value(total_change - sum_contributions)
            if total_change is not None and sum_contributions is not None
            else None
        )
        if incomplete:
            status = AnalysisStatus.INSUFFICIENT_DATA
            reason = "at least one dimension group is missing in the current or baseline range"
        elif total_change == 0:
            status = AnalysisStatus.NOT_CALCULABLE
            reason = "total change is zero; contribution rate is undefined"
        else:
            status = AnalysisStatus.OK
            reason = None
        return ContributionResponse(
            status=status,
            metric=metric.name,
            label=metric.label,
            dimension=dimension.name,
            unit=metric.unit,
            current_start=request.current_start,
            current_end=request.current_end,
            baseline_start=request.baseline_start,
            baseline_end=request.baseline_end,
            total_current=_value(total_current) if total_current is not None else None,
            total_baseline=_value(total_baseline) if total_baseline is not None else None,
            total_change=total_change,
            denominator=total_change,
            allocatable=not incomplete and total_change not in (None, ZERO),
            items=items,
            sum_contributions=sum_contributions,
            residual=residual,
            reason=reason,
        )

    def analyze_trend(self, request: TrendRequest) -> TrendResponse:
        metric, resolution = self._resolve(request.metric)
        if metric is None:
            return TrendResponse(
                status=self._resolution_status(resolution),
                metric=request.metric,
                label=request.metric,
                unit="",
                start=request.start,
                end=request.end,
                rule="trend requires a resolved metric",
                reason=resolution.message,
            )
        try:
            _, rows, periods = self._fetch(metric, request.start, request.end, [], request.filters)
        except QueryCompileError as exc:
            return TrendResponse(
                status=AnalysisStatus.INVALID_REQUEST,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                rule="trend requires a valid query",
                reason=str(exc),
            )
        missing = [month for month in month_sequence(request.start, request.end) if month not in periods]
        if missing:
            return TrendResponse(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                rule="trend requires a complete time range",
                reason="missing months: " + ", ".join(item.isoformat() for item in missing),
            )
        points = self._rollup(metric, rows, request.grain.value, request.start, request.end)
        if len(points) < 3:
            return TrendResponse(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                points=[
                    TrendPoint(period=point.period, value=point.value, change=None, change_rate=None)
                    for point in points
                ],
                rule="trend requires at least 3 time buckets",
                reason="fewer than 3 time buckets",
            )
        if any(point.value is None for point in points):
            return TrendResponse(
                status=AnalysisStatus.NOT_CALCULABLE,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                points=[TrendPoint(period=point.period, value=point.value) for point in points],
                rule="trend requires a numeric value in every bucket",
                reason="one or more buckets have zero denominator or no value",
            )

        trend_points: list[TrendPoint] = []
        changes: list[Decimal] = []
        for index, point in enumerate(points):
            if index == 0:
                trend_points.append(TrendPoint(period=point.period, value=point.value))
                continue
            previous = points[index - 1].value
            change = point.value - previous if point.value is not None and previous is not None else None
            rate = _rate(change / previous) if change is not None and previous not in (None, 0) else None
            if change is not None:
                changes.append(change)
            trend_points.append(TrendPoint(period=point.period, value=point.value, change=change, change_rate=rate))

        values = [point.value for point in points if point.value is not None]
        positive_steps = sum(1 for item in changes if item > 0)
        negative_steps = sum(1 for item in changes if item < 0)
        direction_changes = 0
        for previous, current in zip(changes, changes[1:]):
            if previous == 0 or current == 0:
                continue
            if (previous > 0) != (current > 0):
                direction_changes += 1
        first, last = values[0], values[-1]
        total_rate = _rate((last - first) / first) if first != 0 else None
        mean = sum(values, ZERO) / len(values)
        cv = float(statistics.pstdev([float(value) for value in values]) / float(mean)) if mean != 0 else None
        if total_rate is not None and total_rate >= Decimal("0.05") and positive_steps >= negative_steps:
            direction = "growth"
        elif total_rate is not None and total_rate <= Decimal("-0.05") and negative_steps >= positive_steps:
            direction = "decline"
        elif direction_changes >= 2 or (cv is not None and cv > 0.25):
            direction = "volatile"
        else:
            direction = "flat"
        rule = (
            "growth/decline if first-to-last rate >= 5% or <= -5% and the dominant step direction matches; "
            "otherwise volatile if sign changes >= 2 or coefficient of variation > 0.25; otherwise flat"
        )
        return TrendResponse(
            status=AnalysisStatus.OK,
            metric=metric.name,
            label=metric.label,
            unit=metric.unit,
            start=request.start,
            end=request.end,
            direction=direction,
            points=trend_points,
            rule=rule,
            stats={
                "point_count": len(points),
                "first_value": _value(first),
                "last_value": _value(last),
                "total_change": _value(last - first),
                "total_change_rate": total_rate,
                "positive_steps": positive_steps,
                "negative_steps": negative_steps,
                "direction_changes": direction_changes,
                "coefficient_of_variation": None if cv is None else round(cv, 6),
            },
        )

    def detect_anomalies(self, request: AnomalyRequest) -> AnomalyResponse:
        metric, resolution = self._resolve(request.metric)
        threshold = Decimal(str(request.z_threshold if request.method == "zscore" else request.threshold))
        if metric is None:
            return AnomalyResponse(
                status=self._resolution_status(resolution),
                metric=request.metric,
                label=request.metric,
                unit="",
                start=request.start,
                end=request.end,
                method=request.method,
                rule="anomaly detection requires a resolved metric",
                threshold=threshold,
                reason=resolution.message,
            )
        try:
            _, rows, periods = self._fetch(metric, request.start, request.end, [], request.filters)
        except QueryCompileError as exc:
            return AnomalyResponse(
                status=AnalysisStatus.INVALID_REQUEST,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                method=request.method,
                rule="anomaly detection requires a valid query",
                threshold=threshold,
                reason=str(exc),
            )
        missing = [month for month in month_sequence(request.start, request.end) if month not in periods]
        if missing:
            return AnomalyResponse(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                method=request.method,
                rule="anomaly detection requires a complete time range",
                threshold=threshold,
                reason="missing months: " + ", ".join(item.isoformat() for item in missing),
            )
        points = self._rollup(metric, rows, "month", request.start, request.end)
        if any(point.value is None for point in points):
            return AnomalyResponse(
                status=AnalysisStatus.NOT_CALCULABLE,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                method=request.method,
                rule="anomaly detection requires a numeric value in every month",
                threshold=threshold,
                reason="one or more months have zero denominator or no value",
            )
        if request.method == "mom_threshold":
            rule = "abs((current - previous) / previous) >= threshold"
            if len(points) < 2:
                return AnomalyResponse(
                    status=AnalysisStatus.INSUFFICIENT_DATA,
                    metric=metric.name,
                    label=metric.label,
                    unit=metric.unit,
                    start=request.start,
                    end=request.end,
                    method=request.method,
                    rule=rule,
                    threshold=threshold,
                    reason="at least 2 months are required",
                )
            anomalies: list[AnomalyPoint] = []
            evaluated = 0
            for previous, current in zip(points, points[1:]):
                if previous.value in (None, 0):
                    continue
                evaluated += 1
                change = current.value - previous.value
                rate = change / previous.value
                if abs(rate) >= threshold:
                    anomalies.append(
                        AnomalyPoint(
                            period=current.period,
                            value=_value(current.value),
                            baseline_value=_value(previous.value),
                            change_rate=_rate(rate),
                            direction="up" if rate > 0 else "down",
                            rule=rule,
                            threshold=threshold,
                        )
                    )
            status = AnalysisStatus.OK if evaluated else AnalysisStatus.NOT_CALCULABLE
            return AnomalyResponse(
                status=status,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                method=request.method,
                points=anomalies,
                rule=rule,
                threshold=threshold,
                reason=None if evaluated else "previous value is zero for every month",
            )

        rule = "abs((current - rolling_mean) / rolling_stddev) >= z_threshold"
        if len(points) < request.min_history + 1:
            return AnomalyResponse(
                status=AnalysisStatus.INSUFFICIENT_DATA,
                metric=metric.name,
                label=metric.label,
                unit=metric.unit,
                start=request.start,
                end=request.end,
                method=request.method,
                rule=rule,
                threshold=threshold,
                reason=f"at least {request.min_history + 1} months are required",
            )
        anomalies = []
        evaluated = 0
        for index in range(request.min_history, len(points)):
            history = [point.value for point in points[:index] if point.value is not None]
            mean = sum(history, ZERO) / len(history)
            variance = sum((value - mean) ** 2 for value in history) / len(history)
            stddev = variance.sqrt()
            current = points[index]
            if stddev == 0:
                continue
            evaluated += 1
            zscore = (current.value - mean) / stddev
            if abs(zscore) >= threshold:
                anomalies.append(
                    AnomalyPoint(
                        period=current.period,
                        value=_value(current.value),
                        baseline_value=_value(mean),
                        zscore=_rate(zscore),
                        direction="up" if zscore > 0 else "down",
                        rule=rule,
                        threshold=threshold,
                    )
                )
        status = AnalysisStatus.OK if evaluated else AnalysisStatus.NOT_CALCULABLE
        return AnomalyResponse(
            status=status,
            metric=metric.name,
            label=metric.label,
            unit=metric.unit,
            start=request.start,
            end=request.end,
            method=request.method,
            points=anomalies,
            rule=rule,
            threshold=threshold,
            reason=None if evaluated else "history has zero variance for every evaluable month",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _resolution_status(resolution: MetricResolution) -> AnalysisStatus:
        if resolution.status == "ambiguous":
            return AnalysisStatus.AMBIGUOUS
        return AnalysisStatus.UNSUPPORTED

    @staticmethod
    def _comparison_label(kind: str, period: date) -> str:
        if kind in {"qoq", "yoy_quarter"}:
            return quarter_label(period)
        if kind == "yoy_year":
            return str(period.year)
        return period.strftime("%Y-%m")

    @staticmethod
    def _group_rows(rows: list[_Row]) -> dict[tuple[tuple[str, str], ...], list[_Row]]:
        grouped: dict[tuple[tuple[str, str], ...], list[_Row]] = defaultdict(list)
        for row in rows:
            grouped[_dimension_key(row.dimensions)].append(row)
        for value in grouped.values():
            value.sort(key=lambda row: row.period)
        return dict(grouped)


# ----------------------------------------------------------------------
# Stable service functions for Phase 3/4 tools
# ----------------------------------------------------------------------
def query_metric(
    session: Session,
    request: MetricQueryRequest,
    *,
    query_executor=None,
    semantic_guard=None,
) -> MetricQueryResult:
    return AnalysisEngine(
        session,
        query_executor=query_executor,
        semantic_guard=semantic_guard,
    ).query_metric(request)


def compare_periods(
    session: Session,
    request: PeriodComparisonRequest,
    *,
    query_executor=None,
    semantic_guard=None,
) -> PeriodComparisonResponse:
    return AnalysisEngine(
        session,
        query_executor=query_executor,
        semantic_guard=semantic_guard,
    ).compare_periods(request)


def get_top_n(
    session: Session,
    request: TopNRequest,
    *,
    query_executor=None,
    semantic_guard=None,
) -> TopNResponse:
    return AnalysisEngine(
        session,
        query_executor=query_executor,
        semantic_guard=semantic_guard,
    ).get_top_n(request)


def analyze_contribution(
    session: Session,
    request: ContributionRequest,
    *,
    query_executor=None,
    semantic_guard=None,
) -> ContributionResponse:
    return AnalysisEngine(
        session,
        query_executor=query_executor,
        semantic_guard=semantic_guard,
    ).analyze_contribution(request)


def analyze_trend(
    session: Session,
    request: TrendRequest,
    *,
    query_executor=None,
    semantic_guard=None,
) -> TrendResponse:
    return AnalysisEngine(
        session,
        query_executor=query_executor,
        semantic_guard=semantic_guard,
    ).analyze_trend(request)


def detect_anomalies(
    session: Session,
    request: AnomalyRequest,
    *,
    query_executor=None,
    semantic_guard=None,
) -> AnomalyResponse:
    return AnalysisEngine(
        session,
        query_executor=query_executor,
        semantic_guard=semantic_guard,
    ).detect_anomalies(request)
