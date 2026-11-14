from __future__ import annotations

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import date
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.exc import OperationalError

from ..analysis.contracts import (
    AnalysisStatus,
    AnomalyRequest,
    ContributionRequest,
    DimensionFilter,
    MetricQueryRequest,
    PeriodComparisonRequest,
    TimeGrain,
    TopNRequest,
    TrendRequest,
)
from ..analysis.engine import (
    analyze_contribution,
    analyze_trend,
    compare_periods,
    detect_anomalies,
    get_top_n,
    query_metric,
)
from ..analysis.time_ranges import month_start, shift_month
from ..config import Settings
from ..metric_resolver import DimensionRegistry, MetricResolver
from ..security import (
    AuthorizationError,
    AuthorizationPolicy,
    Principal,
    SafeQueryExecutor,
    SemanticQueryGuard,
)
from .contracts import EvidenceDraft


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolErrorInfo(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    status: str
    summary: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    error: ToolErrorInfo | None = None
    duration_ms: float = 0.0


# The model sees a bounded, structured view of each result. The limit keeps
# prompts predictable while still showing every row for typical dimension
# queries (products, departments, regions) instead of only a summary.
MODEL_VISIBLE_ROW_LIMIT = 20
_DATA_TOOL_NAMES = frozenset(
    {
        "query_metric",
        "compare_periods",
        "top_n",
        "contribution",
        "trend",
        "anomaly_scan",
    }
)
_ROW_KEYS: dict[str, tuple[str, ...]] = {
    "query_metric": (
        "period",
        "dimensions",
        "dimension_labels",
        "value",
        "numerator",
        "denominator",
        "unit",
        "status",
        "reason",
    ),
    "trend": ("period", "value", "change", "change_rate"),
    "anomaly_scan": (
        "period",
        "value",
        "baseline_value",
        "change_rate",
        "zscore",
        "direction",
        "rule",
        "threshold",
        "reason",
    ),
    "top_n": (
        "rank",
        "dimension_value",
        "dimension_label",
        "value",
        "numerator",
        "denominator",
        "share",
        "unit",
    ),
    "compare_periods": (
        "dimensions",
        "current_period",
        "baseline_period",
        "current_value",
        "baseline_value",
        "absolute_change",
        "change_rate",
        "status",
        "reason",
    ),
    "contribution": (
        "dimension_value",
        "dimension_label",
        "current_value",
        "baseline_value",
        "absolute_change",
        "contribution",
        "contribution_rate",
        "direction",
    ),
    "list_metrics": ("name", "label", "domain", "source_table", "unit"),
}


def model_visible_data(
    name: str,
    result: ToolResult,
    *,
    limit: int = MODEL_VISIBLE_ROW_LIMIT,
) -> dict[str, Any] | None:
    """Build the bounded, model-visible detail for a tool result.

    This is the single source of truth for both a normal tool message and a
    reused result after semantic dedupe. It never queries the database and
    never bypasses the result's authorization; it only serializes data that
    the tool already returned.
    """

    data = result.data or {}
    if name == "describe_metric":
        details = {
            key: data.get(key)
            for key in (
                "name",
                "label",
                "synonyms",
                "source_table",
                "aggregation",
                "additivity",
                "unit",
                "available_dimensions",
            )
            if key in data
        }
        return {
            "tool": name,
            "details": details,
            "row_count": 0,
            "displayed_count": 0,
            "truncated": False,
            "remaining_count": 0,
            "display_limit": limit,
            "has_more": False,
            "next_step": "口径信息不包含业务数据明细。",
        }

    if name == "list_metrics":
        rows = data.get("metrics") or []
    elif name in {"query_metric", "trend", "anomaly_scan"}:
        rows = data.get("points") or []
    elif name in {"top_n", "compare_periods", "contribution"}:
        rows = data.get("items") or []
    else:
        return None
    if not isinstance(rows, list):
        return None

    keys = _ROW_KEYS[name]
    normalized_rows = [
        {key: row.get(key) for key in keys if isinstance(row, dict) and key in row}
        for row in rows
        if isinstance(row, dict)
    ]
    row_count = len(normalized_rows)
    displayed = normalized_rows[: max(0, int(limit))]
    truncated = row_count > len(displayed)
    explain = data.get("explain") if isinstance(data.get("explain"), dict) else {}
    source_table = data.get("source_table") or explain.get("source_table")
    dimension_keys: list[str] = []
    if name == "query_metric":
        dimension_keys = sorted({str(key) for row in normalized_rows for key in (row.get("dimensions") or {})})
    elif name == "top_n" and data.get("dimension"):
        dimension_keys = [str(data["dimension"])]

    view: dict[str, Any] = {
        "tool": name,
        "metric": data.get("metric"),
        "label": data.get("label"),
        "unit": data.get("unit"),
        "source_table": source_table,
        "row_count": row_count,
        "displayed_count": len(displayed),
        "truncated": truncated,
        "remaining_count": max(0, row_count - len(displayed)),
        "display_limit": limit,
        "has_more": truncated,
        "rows": displayed,
    }
    if dimension_keys:
        view["dimensions"] = dimension_keys
    if name == "top_n" and data.get("dimension"):
        view["dimension"] = data["dimension"]
    if name in {"query_metric", "trend", "anomaly_scan"}:
        view["grain"] = data.get("time_grain") or data.get("grain")
    if data.get("start") is not None or data.get("end") is not None:
        view["time_range"] = {
            "start": data.get("start"),
            "end": data.get("end"),
        }
    if name in {"compare_periods"}:
        view["kind"] = data.get("kind")
        view["current_period"] = data.get("current_period")
        view["baseline_period"] = data.get("baseline_period")
    if name == "contribution":
        view["totals"] = {
            "current": data.get("total_current"),
            "baseline": data.get("total_baseline"),
            "change": data.get("total_change"),
            "sum_contributions": data.get("sum_contributions"),
            "residual": data.get("residual"),
        }
    if truncated:
        if name == "top_n":
            view["next_step"] = (
                f"当前仅展示前 {len(displayed)} 条，共 {row_count} 条；"
                "请增加过滤条件或缩小时间范围后分批查询，不要假设未展示成员不存在。"
            )
        else:
            view["next_step"] = (
                f"当前仅展示前 {len(displayed)} 条，共 {row_count} 条；"
                "请使用更窄的时间范围、增加过滤条件或降低粒度后重新查询，"
                "不要假设未展示数据不存在。"
            )
    else:
        view["next_step"] = "已展示当前结果的完整明细。"
    return view


class ToolResultCache(Protocol):
    """Durable completed-call cache used to make resume idempotent."""

    def get_tool_result(self, run_id: str, call_key: str) -> dict[str, Any] | None: ...

    def put_tool_result(
        self,
        *,
        run_id: str,
        call_key: str,
        tool_name: str,
        call_id: str,
        status: str,
        result: dict[str, Any],
    ) -> None: ...


class EmptyArgs(ToolArgs):
    pass


class MetricArgs(ToolArgs):
    metric: str = Field(min_length=1)


class DescribeMetricArgs(MetricArgs):
    pass


class RangeArgs(ToolArgs):
    start: date = Field(description="起始日期 YYYY-MM-DD；单月查询用该月 1 日")
    end: date = Field(description="结束日期 YYYY-MM-DD；单月查询也用该月 1 日")
    filters: list[DimensionFilter] = Field(default_factory=list)


class MetricQueryArgs(RangeArgs):
    metric: str = Field(min_length=1, description="canonical 指标英文名，如 sales_amount")
    dimensions: list[str] = Field(
        default_factory=list,
        description="分组维度；已被 filters 限定为单值的维度不要重复分组",
    )
    grain: TimeGrain = Field(
        default=TimeGrain.MONTH,
        description=(
            "时间粒度，默认 month；跨多个月的季度/年度区间保留 month，"
            "只有单月或用户明确要求区间合计时用 total；趋势分析用 trend 工具"
        ),
    )


class ComparisonArgs(ToolArgs):
    metric: str = Field(min_length=1, description="canonical 指标英文名")
    kind: str = Field(
        pattern="^(mom|yoy|qoq|yoy_quarter|yoy_year)$",
        description=(
            "mom 环比、yoy 月度同比、qoq 季度环比、"
            "yoy_quarter 指定季度相对上年同季度、yoy_year 完整年度总额同比；"
            "用户问“第 N 季度同比”必须使用 yoy_quarter，不能用 yoy_year"
        ),
    )
    period: date | None = Field(
        default=None,
        description="mom/yoy 使用当期月首日期 YYYY-MM-01",
    )
    year: int | None = Field(default=None, ge=2000, le=2100)
    quarter: int | None = Field(default=None, ge=1, le=4)
    filters: list[DimensionFilter] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_period_arguments(self) -> "ComparisonArgs":
        if self.kind == "yoy_year" and self.quarter is not None:
            raise ValueError("yoy_year 不接受 quarter；季度同比请使用 kind=yoy_quarter")
        if self.kind in {"qoq", "yoy_quarter"} and (self.year is None or self.quarter is None):
            raise ValueError(f"{self.kind} 需要同时提供 year 和 quarter")
        return self


class TopNArgs(RangeArgs):
    metric: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    n: int = Field(default=5, ge=1, le=50)
    ascending: bool = False


class ContributionArgs(ToolArgs):
    metric: str = Field(min_length=1)
    dimension: str = Field(min_length=1)
    current_start: date
    current_end: date
    baseline_start: date
    baseline_end: date
    filters: list[DimensionFilter] = Field(default_factory=list)
    top_n: int | None = Field(default=None, ge=1, le=50)

    @model_validator(mode="after")
    def _check_periods(self) -> "ContributionArgs":
        if self.current_end < self.current_start:
            raise ValueError("current_end 不能早于 current_start")
        if self.baseline_end < self.baseline_start:
            raise ValueError("baseline_end 不能早于 baseline_start")
        if self.baseline_end >= self.current_start:
            raise ValueError("基准期与当前期重叠：baseline_end 必须早于 current_start")
        return self


class TrendArgs(RangeArgs):
    metric: str = Field(min_length=1)
    grain: TimeGrain = TimeGrain.MONTH


class AnomalyArgs(RangeArgs):
    metric: str = Field(min_length=1)
    method: str = Field(default="mom_threshold", pattern="^(mom_threshold|zscore)$")
    threshold: float = Field(default=0.2, gt=0)
    z_threshold: float = Field(default=2.0, gt=0)


class AnalysisGateway:
    """The only database boundary used by Agent tools.

    Every data tool compiles a trusted semantic query and executes it through
    SemanticQueryGuard + SafeQueryExecutor. Tools never receive a Session,
    Connection or raw SQL string.
    """

    def __init__(
        self,
        session_factory,
        settings: Settings,
        *,
        query_executor=None,
        semantic_guard: SemanticQueryGuard | None = None,
    ):
        self.session_factory = session_factory
        self.settings = settings
        self.metric_resolver = MetricResolver()
        self.dimension_registry = DimensionRegistry()
        if query_executor is None:
            bind = getattr(session_factory, "kw", {}).get("bind")
            if bind is None:
                raise ValueError("AnalysisGateway requires a sessionmaker bound to an Engine")
            query_executor = SafeQueryExecutor(
                bind,
                max_rows=settings.query_max_rows,
                timeout_ms=settings.query_timeout_ms,
            )
        self.query_executor = query_executor
        self.semantic_guard = semantic_guard or SemanticQueryGuard(
            self.metric_resolver,
            self.dimension_registry,
        )

    def _session(self):
        return self.session_factory()

    def _query(self, function, session, request):
        return function(
            session,
            request,
            query_executor=self.query_executor,
            semantic_guard=self.semantic_guard,
        )

    def list_metrics(self, args: EmptyArgs) -> ToolResult:
        items = [
            {
                "name": metric.name,
                "label": metric.label,
                "domain": getattr(metric, "domain", None),
                "source_table": getattr(metric, "source_table", None),
                "unit": getattr(metric, "unit", None),
            }
            for metric in self.metric_resolver.metrics
        ]
        return ToolResult(
            status=AnalysisStatus.OK.value,
            summary=f"语义层共有 {len(items)} 个指标。",
            data={"metrics": items},
        )

    def describe_metric(self, args: DescribeMetricArgs) -> ToolResult:
        resolution = self.metric_resolver.resolve(args.metric)
        if resolution.status != "resolved" or resolution.metric is None:
            return ToolResult(
                status=AnalysisStatus.UNSUPPORTED.value,
                error=ToolErrorInfo(
                    code="unknown_metric",
                    message=resolution.message or "metric is not defined",
                ),
            )
        metric = resolution.metric
        data = {
            "name": metric.name,
            "label": metric.label,
            "synonyms": list(metric.synonyms),
            "source_table": getattr(metric, "source_table", None),
            "aggregation": getattr(metric, "aggregation", None),
            "additivity": str(getattr(metric, "additivity", "")),
            "unit": getattr(metric, "unit", None),
            "available_dimensions": list(getattr(metric, "dimensions", []) or []),
        }
        return ToolResult(
            status=AnalysisStatus.OK.value,
            summary=f"{metric.label}（{metric.name}）口径来源：{data['source_table']}。",
            data=data,
        )

    def query_metric(self, args: MetricQueryArgs) -> ToolResult:
        with self._session() as session:
            result = self._query(
                query_metric,
                session,
                MetricQueryRequest(
                    metric=args.metric,
                    start=args.start,
                    end=args.end,
                    dimensions=args.dimensions,
                    grain=args.grain,
                    filters=args.filters,
                ),
            )
        return self._wrap(
            result,
            metric=args.metric,
            start=args.start,
            end=args.end,
            filters=args.filters,
            summary=self._metric_summary(result),
        )

    def compare_periods(self, args: ComparisonArgs) -> ToolResult:
        with self._session() as session:
            result = self._query(
                compare_periods,
                session,
                PeriodComparisonRequest(
                    metric=args.metric,
                    kind=args.kind,
                    period=args.period,
                    year=args.year,
                    quarter=args.quarter,
                    filters=args.filters,
                ),
            )
        summary = None
        if result.items:
            item = result.items[0]
            summary = (
                f"{result.label}：{item.current_period} {item.current_value} vs "
                f"{item.baseline_period} {item.baseline_value}，变化率 {item.change_rate}。"
            )
        start, end, baseline_start, baseline_end, baseline_period = _comparison_ranges(args)
        return self._wrap(
            result,
            metric=args.metric,
            start=start,
            end=end,
            baseline_period=getattr(result, "baseline_period", None) or baseline_period,
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            filters=args.filters,
            summary=summary,
        )

    def top_n(self, args: TopNArgs) -> ToolResult:
        with self._session() as session:
            result = self._query(
                get_top_n,
                session,
                TopNRequest(
                    metric=args.metric,
                    dimension=args.dimension,
                    start=args.start,
                    end=args.end,
                    n=args.n,
                    ascending=args.ascending,
                    filters=args.filters,
                ),
            )
        shown = result.items[:MODEL_VISIBLE_ROW_LIMIT]
        names = "；".join(f"{item.dimension_value}={item.value}" for item in shown)
        if len(result.items) > len(shown):
            names += f"；另有 {len(result.items) - len(shown)} 条未展示"
        return self._wrap(
            result,
            metric=args.metric,
            start=args.start,
            end=args.end,
            filters=args.filters,
            summary=f"{result.label} Top {len(result.items)}（展示 {len(shown)} 条）：{names}"
            if result.items
            else None,
        )

    def contribution(self, args: ContributionArgs) -> ToolResult:
        with self._session() as session:
            result = self._query(
                analyze_contribution,
                session,
                ContributionRequest(
                    metric=args.metric,
                    dimension=args.dimension,
                    current_start=args.current_start,
                    current_end=args.current_end,
                    baseline_start=args.baseline_start,
                    baseline_end=args.baseline_end,
                    filters=args.filters,
                    top_n=args.top_n,
                ),
            )
        shown = result.items[:MODEL_VISIBLE_ROW_LIMIT]
        names = "；".join(f"{item.dimension_value}={item.contribution}" for item in shown)
        if len(result.items) > len(shown):
            names += f"；另有 {len(result.items) - len(shown)} 条未展示"
        return self._wrap(
            result,
            metric=args.metric,
            start=args.current_start,
            end=args.current_end,
            baseline_period=(f"{args.baseline_start:%Y-%m}..{args.baseline_end:%Y-%m}"),
            baseline_start=args.baseline_start,
            baseline_end=args.baseline_end,
            filters=args.filters,
            summary=f"{result.label}贡献（展示 {len(shown)}/{len(result.items)} 条）：{names}"
            if result.items
            else None,
        )

    def trend(self, args: TrendArgs) -> ToolResult:
        with self._session() as session:
            result = self._query(
                analyze_trend,
                session,
                TrendRequest(
                    metric=args.metric,
                    start=args.start,
                    end=args.end,
                    grain=args.grain,
                    filters=args.filters,
                ),
            )
        return self._wrap(
            result,
            metric=args.metric,
            start=args.start,
            end=args.end,
            filters=args.filters,
            summary=f"{result.label}趋势方向：{result.direction}",
        )

    def anomaly(self, args: AnomalyArgs) -> ToolResult:
        with self._session() as session:
            result = self._query(
                detect_anomalies,
                session,
                AnomalyRequest(
                    metric=args.metric,
                    start=args.start,
                    end=args.end,
                    filters=args.filters,
                    method=args.method,
                    threshold=args.threshold,
                    z_threshold=args.z_threshold,
                ),
            )
        return self._wrap(
            result,
            metric=args.metric,
            start=args.start,
            end=args.end,
            filters=args.filters,
            summary=f"{result.label}检出 {len(result.points)} 个异常点。",
        )

    @staticmethod
    def _metric_summary(result) -> str:
        if not result.points:
            return f"{result.label}没有返回数据。"
        if all(point.value is None for point in result.points):
            return f"{result.label}返回 {len(result.points)} 个数据点，但所有数值均不可计算。"
        first = result.points[0]
        last = result.points[-1]

        def value_text(value) -> str:
            return "不可计算" if value is None else str(value)

        dimension_keys = sorted({key for point in result.points for key in (getattr(point, "dimensions", None) or {})})
        if dimension_keys:
            scope = first.period if first.period == last.period else f"{first.period} 至 {last.period}"
            detail = f"首条值 {value_text(first.value)}"
            if len(result.points) > 1:
                detail += f"，末条值 {value_text(last.value)}"
            return (
                f"{result.label}返回 {len(result.points)} 个数据点（{scope}），"
                f"按 {'、'.join(dimension_keys)} 拆分；{detail}。"
            )
        if len(result.points) == 1:
            return f"{result.label}在 {first.period} 为 {value_text(first.value)}。"
        return (
            f"{result.label}从 {first.period} 到 {last.period} 共 {len(result.points)} 个点；"
            f"首值 {value_text(first.value)}，末值 {value_text(last.value)}。"
        )

    def _wrap(
        self,
        result,
        *,
        metric: str,
        start: date | None = None,
        end: date | None = None,
        baseline_period: str | None = None,
        baseline_start: date | None = None,
        baseline_end: date | None = None,
        filters: list[DimensionFilter] | None = None,
        summary: str | None,
    ) -> ToolResult:
        status = result.status.value if isinstance(result.status, AnalysisStatus) else str(result.status)
        data = result.model_dump(mode="json")
        definition = self.metric_resolver.get(metric)
        reason = getattr(result, "reason", None)
        error = None
        if status not in {"ok", "no_data", "not_calculable", "insufficient_data"} and reason:
            error = ToolErrorInfo(code=status, message=str(reason))
        return ToolResult(
            status=status,
            summary=summary,
            data=data,
            error=error,
            evidence=[
                _evidence_from_result(
                    result,
                    metric=metric,
                    start=start,
                    end=end,
                    baseline_period=baseline_period,
                    baseline_start=baseline_start,
                    baseline_end=baseline_end,
                    filters=filters or [],
                    summary=summary or f"{metric} 返回状态 {status}",
                    source_fallback=definition.source_table if definition else None,
                )
            ],
        )


def _comparison_ranges(
    args: ComparisonArgs,
) -> tuple[date | None, date | None, date | None, date | None, str | None]:
    if args.kind in {"mom", "yoy"}:
        if args.period is None:
            return None, None, None, None, None
        current = month_start(args.period)
        offset = -1 if args.kind == "mom" else -12
        baseline = shift_month(current, offset)
        return current, current, baseline, baseline, f"{baseline:%Y-%m}"
    if args.kind == "yoy_year":
        if args.year is None:
            return None, None, None, None, None
        return (
            date(args.year, 1, 1),
            date(args.year, 12, 1),
            date(args.year - 1, 1, 1),
            date(args.year - 1, 12, 1),
            str(args.year - 1),
        )
    if args.year is None or args.quarter is None:
        return None, None, None, None, None
    start = date(args.year, (args.quarter - 1) * 3 + 1, 1)
    end = date(args.year, args.quarter * 3, 1)
    if args.kind == "qoq":
        baseline_start = shift_month(start, -3)
        baseline_end = shift_month(end, -3)
    else:
        baseline_start = date(args.year - 1, start.month, 1)
        baseline_end = date(args.year - 1, end.month, 1)
    label = f"{baseline_start.year}Q{(baseline_start.month - 1) // 3 + 1}"
    return start, end, baseline_start, baseline_end, label


def _evidence_from_result(
    result,
    *,
    metric: str,
    start: date | None,
    end: date | None,
    baseline_period: str | None,
    baseline_start: date | None,
    baseline_end: date | None,
    filters: list[DimensionFilter],
    summary: str,
    source_fallback: str | None = None,
) -> EvidenceDraft:
    explain = getattr(result, "explain", None)
    source = getattr(explain, "source_table", None) if explain is not None else None
    source = source or getattr(result, "source_table", None) or source_fallback
    missing: list[str] = []
    if source is None:
        missing.append("source_table")
    if start is None:
        missing.append("time_range_start")
    if end is None:
        missing.append("time_range_end")
    has_baseline = baseline_period is not None or baseline_start is not None or baseline_end is not None
    if has_baseline:
        if baseline_period is None:
            missing.append("baseline_period")
        if baseline_start is None:
            missing.append("baseline_time_range_start")
        if baseline_end is None:
            missing.append("baseline_time_range_end")
    return EvidenceDraft(
        evidence_id="",
        tool_call_id="",
        metric=metric,
        time_range_start=start.isoformat() if start else None,
        time_range_end=end.isoformat() if end else None,
        baseline_period=baseline_period,
        baseline_time_range_start=baseline_start.isoformat() if baseline_start else None,
        baseline_time_range_end=baseline_end.isoformat() if baseline_end else None,
        filters=[item.model_dump(mode="json") for item in filters],
        result_summary=summary,
        source=source,
        traceable=source is not None and not missing,
        missing_fields=missing,
    )


Handler = Callable[[AnalysisGateway, Any], ToolResult]


class ToolDefinition(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler
    required_scope: str = "metrics:read"
    timeout_seconds: float | None = None
    data_access: bool = True

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args_model.model_json_schema(),
        }


def build_default_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="list_metrics",
            description="列出语义层中的 canonical 指标名称、标签和业务域。不查询业务数据。",
            args_model=EmptyArgs,
            handler=lambda gateway, args: gateway.list_metrics(args),
            data_access=False,
        ),
        ToolDefinition(
            name="describe_metric",
            description="查询一个指标的口径、来源表、聚合方式和可用维度。",
            args_model=DescribeMetricArgs,
            handler=lambda gateway, args: gateway.describe_metric(args),
            data_access=False,
        ),
        ToolDefinition(
            name="query_metric",
            description=(
                "按时间范围、维度和过滤条件查询一个指标。返回确定性聚合结果和可追溯证据。"
                "grain 默认 month；跨多个月的季度/年度区间且用户未明确说“合计/累计/总额”时"
                "保留 month 明细，只有单月或明确要求区间合计时用 total。"
                "排名请用 top_n，趋势请用 trend，异常扫描请用 anomaly_scan。"
                "所有值必须来自本工具，不得由模型自行计算。"
            ),
            args_model=MetricQueryArgs,
            handler=lambda gateway, args: gateway.query_metric(args),
        ),
        ToolDefinition(
            name="compare_periods",
            description=(
                "对指标做环比、同比、季度环比或年度同比。"
                "季度同比用 yoy_quarter 并同时给 year/quarter；yoy_year 只表示完整年度总额同比。"
            ),
            args_model=ComparisonArgs,
            handler=lambda gateway, args: gateway.compare_periods(args),
        ),
        ToolDefinition(
            name="top_n",
            description=(
                "按维度对指标排名，返回 Top N 或 Bottom N。"
                "用户问最高/最低/前 N/全部排名时使用；ascending=true 表示最低在前；"
                "n 必须显式提供，需要全部成员时 n 取成员数。"
            ),
            args_model=TopNArgs,
            handler=lambda gateway, args: gateway.top_n(args),
        ),
        ToolDefinition(
            name="contribution",
            description="计算当前期相对基准期的维度增量贡献；仅适用于可加指标。",
            args_model=ContributionArgs,
            handler=lambda gateway, args: gateway.contribution(args),
        ),
        ToolDefinition(
            name="trend",
            description=(
                "分析指标在时间范围内的趋势方向。用户问逐月/逐季趋势、走势或上升下降时使用；不要用 query_metric 代替。"
            ),
            args_model=TrendArgs,
            handler=lambda gateway, args: gateway.trend(args),
        ),
        ToolDefinition(
            name="anomaly_scan",
            description=(
                "用确定性规则扫描指标异常点（环比阈值或 Z 分数）。"
                "用户提到异常月份、离群点或 Z 分数时使用；Z 分数用 method=zscore。"
            ),
            args_model=AnomalyArgs,
            handler=lambda gateway, args: gateway.anomaly(args),
        ),
    ]


def _set_property_enum(
    node: Any,
    property_name: str,
    values: list[str],
    *,
    items: bool = False,
) -> None:
    """Inject a dynamic enum into a Pydantic-generated JSON schema.

    Pydantic owns the runtime validation. This only narrows the schema shown
    to the model; an out-of-enum value is still rejected by model_validate.
    """

    if not values:
        return
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            target = properties.get(property_name)
            if isinstance(target, dict):
                if items:
                    item_schema = target.get("items")
                    if isinstance(item_schema, dict):
                        item_schema["enum"] = values
                else:
                    target["enum"] = values
        for value in node.values():
            _set_property_enum(value, property_name, values, items=items)
    elif isinstance(node, list):
        for value in node:
            _set_property_enum(value, property_name, values, items=items)


class ToolRegistry:
    def __init__(
        self,
        gateway: AnalysisGateway,
        authorization: AuthorizationPolicy,
        settings: Settings,
        definitions: list[ToolDefinition] | None = None,
    ):
        self.gateway = gateway
        self.authorization = authorization
        self.settings = settings
        self._definitions = {item.name: item for item in (definitions or build_default_tools())}

    def definitions(self) -> dict[str, ToolDefinition]:
        return dict(self._definitions)

    def specs(self, principal: Principal) -> list[dict[str, Any]]:
        return [
            self._enrich_spec(definition.spec(), principal)
            for definition in self._definitions.values()
            if self.authorization.tool_allowed(principal, definition.name, definition.required_scope)
        ]

    def context(self, principal: Principal) -> str:
        """Prompt-side catalog derived from the same trusted semantic layer.

        The model still cannot rely on this text for facts: every argument is
        validated again by Pydantic and AuthorizationPolicy, and every value
        comes from a read-only tool execution.
        """

        metrics = [
            metric
            for metric in self.gateway.metric_resolver.metrics
            if self.authorization.metric_allowed(
                principal,
                metric.name,
                domain=metric.domain,
                source=metric.source_table,
            )
        ]
        metric_lines = []
        for metric in metrics:
            dimensions = ", ".join(metric.dimensions) or "none"
            metric_lines.append(f"- {metric.name} ({metric.label}, domain={metric.domain}, dimensions={dimensions})")
        regions = []
        region_dimension = self.gateway.dimension_registry.get("region")
        if region_dimension is not None:
            regions = [
                f"{item.get('code')}={item.get('label')}" for item in region_dimension.values if item.get("code")
            ]
        return (
            "可用 canonical 指标（metric 参数必须使用左侧英文名）：\n"
            + ("\n".join(metric_lines) or "- 无")
            + "\n区域参数必须使用 code，不能用中文名称："
            + (", ".join(regions) or "无区域白名单")
            + "\n日期统一使用 YYYY-MM-DD；单月查询的 start/end 都用该月 1 日。"
            + "\n如果 filters 已限定 region=EC，不要再把 region 放入 dimensions。"
            + "\n跨多个月的季度/年度区间保留默认 grain=month；只有单月或用户明确要求"
            "区间合计时才使用 total；趋势分析请用 trend 工具。"
            + "\n按月序列用 grain=month 且不要传时间维度（period/date/month 都不是合法 dimension）。"
            + "\n过滤 operator 只允许 eq/ne/in/gt/gte/lt/lte；不要使用 SQL 或 '='。"
            + "\n只引用工具返回的原始数值；不要自行换算百分比或做二次计算。"
        )

    def _enrich_spec(self, spec: dict[str, Any], principal: Principal) -> dict[str, Any]:
        metric_names = [
            metric.name
            for metric in self.gateway.metric_resolver.metrics
            if self.authorization.metric_allowed(
                principal,
                metric.name,
                domain=metric.domain,
                source=metric.source_table,
            )
        ]
        dimension_names = [item.name for item in self.gateway.dimension_registry.dimensions]
        schema = spec.get("input_schema")
        if isinstance(schema, dict):
            _set_property_enum(schema, "metric", metric_names)
            _set_property_enum(schema, "dimension", dimension_names)
            _set_property_enum(schema, "dimensions", dimension_names, items=True)
            _set_property_enum(
                schema,
                "operator",
                ["eq", "ne", "in", "gt", "gte", "lt", "lte"],
            )
            _set_property_enum(
                schema,
                "kind",
                ["mom", "yoy", "qoq", "yoy_quarter", "yoy_year"],
            )
            _set_property_enum(schema, "grain", ["month", "quarter", "year", "total"])
        return spec

    def authorize_call(self, name: str, arguments: dict[str, Any], principal: Principal) -> ToolResult | None:
        """Return an authorization failure without executing the tool.

        A resumed run may replay a cached tool result. The cache is keyed by
        tool and arguments, not by principal, so the result must be re-checked
        against the current principal before it is used.
        """

        _, _, failure = self._prepare(name, arguments, principal)
        return failure

    def _prepare(
        self, name: str, arguments: dict[str, Any], principal: Principal
    ) -> tuple[ToolDefinition | None, BaseModel | None, ToolResult | None]:
        definition = self._definitions.get(name)
        if definition is None:
            return (
                None,
                None,
                ToolResult(
                    status="invalid_action",
                    error=ToolErrorInfo(code="unknown_tool", message=f"tool {name!r} is not registered"),
                ),
            )
        try:
            self.authorization.require_tool(principal, definition.required_scope, name)
        except AuthorizationError as exc:
            return (
                definition,
                None,
                ToolResult(
                    status="permission_denied",
                    error=ToolErrorInfo(code="permission_denied", message=str(exc)),
                ),
            )
        try:
            args = definition.args_model.model_validate(arguments)
        except ValidationError as exc:
            return (
                definition,
                None,
                ToolResult(
                    status="invalid_arguments",
                    error=ToolErrorInfo(
                        code="invalid_arguments",
                        message="tool arguments do not match the schema",
                        retryable=False,
                    ),
                    data={"validation_errors": exc.errors(include_url=False, include_context=False)},
                ),
            )

        metric = getattr(args, "metric", None)
        metric_definition = self.gateway.metric_resolver.get(metric) if metric is not None else None
        metric_domain = getattr(metric_definition, "domain", None)
        metric_source = getattr(metric_definition, "source_table", None)
        try:
            if metric is not None and definition.data_access:
                dimensions = list(getattr(args, "dimensions", []) or [])
                singular_dimension = getattr(args, "dimension", None)
                if singular_dimension:
                    dimensions.append(singular_dimension)
                self.authorization.require_request(
                    principal,
                    metric=metric,
                    dimensions=dimensions,
                    filters=getattr(args, "filters", []) or [],
                    metric_domain=metric_domain,
                    metric_source=metric_source,
                )
            elif metric is not None:
                self.authorization.require_metric(
                    principal,
                    metric,
                    domain=metric_domain,
                    source=metric_source,
                )
        except AuthorizationError as exc:
            return (
                definition,
                None,
                ToolResult(
                    status="permission_denied",
                    error=ToolErrorInfo(code="permission_denied", message=str(exc)),
                ),
            )
        return definition, args, None

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        principal: Principal,
        *,
        call_id: str,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        started = time.monotonic()
        definition, args, failure = self._prepare(name, arguments, principal)
        if failure is not None:
            failure.duration_ms = round((time.monotonic() - started) * 1000, 3)
            return failure
        assert definition is not None and args is not None

        limit = min(
            timeout_seconds or definition.timeout_seconds or self.settings.agent_tool_timeout_seconds,
            self.settings.agent_tool_timeout_seconds,
        )
        result = self._run_with_retries(definition, args, limit)
        if name == "list_metrics" and result.status == "ok":
            metrics = [
                item
                for item in result.data.get("metrics", [])
                if self.authorization.metric_allowed(
                    principal,
                    str(item.get("name")),
                    domain=item.get("domain"),
                    source=item.get("source_table"),
                )
            ]
            result.data["metrics"] = metrics
            result.summary = f"语义层中当前角色可用指标共 {len(metrics)} 个。"
        result.duration_ms = round((time.monotonic() - started) * 1000, 3)
        for index, evidence in enumerate(result.evidence, start=1):
            evidence.evidence_id = f"{call_id}-e{index}"
            evidence.tool_call_id = call_id
        return result

    def _run_with_retries(self, definition: ToolDefinition, args: BaseModel, seconds: float) -> ToolResult:
        # Every tool is read-only, so retrying a timeout/execution error is safe.
        attempts = self.settings.agent_tool_max_retries + 1
        result: ToolResult | None = None
        for _ in range(attempts):
            result = self._run_with_timeout(definition, args, seconds)
            if result.status not in {
                "timeout",
                "execution_error",
                "database_unavailable",
            }:
                break
        assert result is not None
        return result

    def _run_with_timeout(self, definition: ToolDefinition, args: BaseModel, seconds: float) -> ToolResult:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eba-tool")
        # Copy the caller's context so tracing identifiers set for the run and
        # tool call stay attached to spans created inside the tool thread.
        context = contextvars.copy_context()
        future = executor.submit(context.run, definition.handler, self.gateway, args)
        try:
            return future.result(timeout=seconds)
        except FutureTimeout:
            future.cancel()
            return ToolResult(
                status="timeout",
                error=ToolErrorInfo(
                    code="tool_timeout",
                    message=f"tool {definition.name!r} exceeded {seconds:.2f}s",
                    retryable=True,
                ),
            )
        except OperationalError as exc:
            return ToolResult(
                status="database_unavailable",
                error=ToolErrorInfo(
                    code="database_unavailable",
                    message=(f"{type(exc).__name__}: database temporarily unavailable"),
                    retryable=True,
                ),
            )
        except Exception as exc:  # tool failures must become structured data
            return ToolResult(
                status="execution_error",
                error=ToolErrorInfo(
                    code="tool_execution_error",
                    message=f"{type(exc).__name__}: tool execution failed",
                    retryable=True,
                ),
            )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
