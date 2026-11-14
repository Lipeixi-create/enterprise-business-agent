from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from .time_ranges import month_start, parse_period_end_value, parse_period_value, quarter_start


class AnalysisStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_CALCULABLE = "not_calculable"
    UNSUPPORTED = "unsupported"
    AMBIGUOUS = "ambiguous"
    INVALID_REQUEST = "invalid_request"
    FORBIDDEN = "forbidden"
    NOT_APPLICABLE = "not_applicable"
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_TIMEOUT = "llm_timeout"
    GUARD_REJECTED = "guard_rejected"
    EXECUTION_ERROR = "execution_error"
    TIMEOUT = "timeout"


class TimeGrain(str, Enum):
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"
    TOTAL = "total"


class DimensionFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimension: str
    operator: Literal["eq", "ne", "in", "gt", "gte", "lt", "lte"]
    value: str | int | float | date | list[str | int | float]

    @field_validator("value", mode="before")
    @classmethod
    def _normalize_list(cls, value):
        if isinstance(value, (list, tuple)):
            return list(value)
        return value


class _RangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def _parse_period(cls, value, info: ValidationInfo):
        if value is None:
            return None
        return parse_period_end_value(value) if info.field_name == "end" else parse_period_value(value)

    @model_validator(mode="after")
    def _normalize_range(self):
        self.start = month_start(self.start)
        self.end = month_start(self.end)
        if self.start > self.end:
            raise ValueError("start must not be later than end")
        return self


class TimeRange(_RangeRequest):
    pass


class MetricQueryRequest(_RangeRequest):
    metric: str
    grain: TimeGrain = TimeGrain.MONTH
    dimensions: list[str] = Field(default_factory=list)
    filters: list[DimensionFilter] = Field(default_factory=list)
    aggregation: Literal["sum", "avg", "last", "count", "ratio"] | None = None


class PeriodComparisonRequest(_RangeRequest):
    """Comparison request.

    For mom/yoy, `period` selects the current month. For qoq/yoy_quarter, either
    `period` or `year` + `quarter` selects the current quarter. start/end are
    ignored by comparisons, but kept optional for a uniform request model.
    """

    start: date = date(2024, 1, 1)
    end: date = date(2024, 1, 1)
    metric: str
    kind: Literal["mom", "yoy", "qoq", "yoy_quarter", "yoy_year"]
    period: date | None = None
    year: int | None = Field(default=None, ge=2000, le=2100)
    quarter: int | None = Field(default=None, ge=1, le=4)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[DimensionFilter] = Field(default_factory=list)

    @field_validator("period", mode="before")
    @classmethod
    def _parse_period(cls, value):
        return None if value is None else parse_period_value(value)

    @model_validator(mode="after")
    def _resolve_period(self):
        if self.kind in {"mom", "yoy"}:
            if self.period is None:
                raise ValueError("period is required for mom/yoy")
            self.period = month_start(self.period)
            return self
        if self.kind == "yoy_year":
            if self.period is not None:
                self.year = self.period.year
            if self.year is None:
                raise ValueError("year or period is required for yoy_year")
            self.period = date(self.year, 1, 1)
            return self
        if self.period is not None:
            self.period = quarter_start(self.period)
            self.year = self.period.year
            self.quarter = (self.period.month - 1) // 3 + 1
            return self
        if self.year is None or self.quarter is None:
            raise ValueError("period or year+quarter is required for qoq/yoy_quarter")
        self.period = quarter_start(date(self.year, (self.quarter - 1) * 3 + 1, 1))
        return self


class TopNRequest(_RangeRequest):
    metric: str
    dimension: str
    n: int = Field(default=5, ge=1, le=100)
    filters: list[DimensionFilter] = Field(default_factory=list)
    ascending: bool = False


class ContributionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    dimension: str
    current_start: date
    current_end: date
    baseline_start: date
    baseline_end: date
    filters: list[DimensionFilter] = Field(default_factory=list)
    top_n: int | None = Field(default=None, ge=1, le=100)

    @field_validator("current_start", "current_end", "baseline_start", "baseline_end", mode="before")
    @classmethod
    def _parse_period(cls, value, info: ValidationInfo):
        if info.field_name.endswith("_end"):
            return parse_period_end_value(value)
        return parse_period_value(value)

    @model_validator(mode="after")
    def _normalize_range(self):
        self.current_start = month_start(self.current_start)
        self.current_end = month_start(self.current_end)
        self.baseline_start = month_start(self.baseline_start)
        self.baseline_end = month_start(self.baseline_end)
        if self.current_start > self.current_end:
            raise ValueError("current_start must not be later than current_end")
        if self.baseline_start > self.baseline_end:
            raise ValueError("baseline_start must not be later than baseline_end")
        return self


class TrendRequest(_RangeRequest):
    metric: str
    grain: TimeGrain = TimeGrain.MONTH
    filters: list[DimensionFilter] = Field(default_factory=list)


class AnomalyRequest(_RangeRequest):
    metric: str
    method: Literal["mom_threshold", "zscore"] = "mom_threshold"
    threshold: float = Field(default=0.20, gt=0)
    z_threshold: float = Field(default=2.0, gt=0)
    min_history: int = Field(default=3, ge=2)
    filters: list[DimensionFilter] = Field(default_factory=list)


class QueryExplain(BaseModel):
    metric: str
    source_table: str
    expression: str
    numerator: str
    denominator: str | None = None
    aggregation: str
    dimensions: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)
    time_grain: str
    dialect: str
    sql: str
    params: dict[str, object] = Field(default_factory=dict)


class MetricPoint(BaseModel):
    period: str
    dimensions: dict[str, str] = Field(default_factory=dict)
    dimension_labels: dict[str, str] = Field(default_factory=dict)
    value: Decimal | None = None
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    unit: str
    status: AnalysisStatus = AnalysisStatus.OK
    reason: str | None = None


class MetricQueryResult(BaseModel):
    status: AnalysisStatus
    metric: str
    label: str
    unit: str
    source_table: str
    aggregation: str
    time_grain: str
    start: date
    end: date
    points: list[MetricPoint] = Field(default_factory=list)
    reason: str | None = None
    missing_periods: list[str] = Field(default_factory=list)
    explain: QueryExplain | None = None


class PeriodComparisonItem(BaseModel):
    dimensions: dict[str, str] = Field(default_factory=dict)
    current_period: str
    baseline_period: str
    current_value: Decimal | None = None
    baseline_value: Decimal | None = None
    absolute_change: Decimal | None = None
    change_rate: Decimal | None = None
    status: AnalysisStatus
    reason: str | None = None


class PeriodComparisonResponse(BaseModel):
    status: AnalysisStatus
    metric: str
    label: str
    kind: str
    unit: str
    current_period: str
    baseline_period: str
    items: list[PeriodComparisonItem] = Field(default_factory=list)
    reason: str | None = None
    explain: QueryExplain | None = None


class TopNItem(BaseModel):
    rank: int
    dimension_value: str
    dimension_label: str | None = None
    value: Decimal | None = None
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    share: Decimal | None = None
    unit: str


class TopNResponse(BaseModel):
    status: AnalysisStatus
    metric: str
    label: str
    dimension: str
    unit: str
    start: date
    end: date
    items: list[TopNItem] = Field(default_factory=list)
    reason: str | None = None
    explain: QueryExplain | None = None


class ContributionItem(BaseModel):
    dimension_value: str
    dimension_label: str | None = None
    current_value: Decimal | None = None
    baseline_value: Decimal | None = None
    absolute_change: Decimal | None = None
    contribution: Decimal | None = None
    contribution_rate: Decimal | None = None
    direction: Literal["increase", "decrease", "flat"] = "flat"


class ContributionResponse(BaseModel):
    status: AnalysisStatus
    metric: str
    label: str
    dimension: str
    unit: str
    current_start: date
    current_end: date
    baseline_start: date
    baseline_end: date
    total_current: Decimal | None = None
    total_baseline: Decimal | None = None
    total_change: Decimal | None = None
    denominator: Decimal | None = None
    allocatable: bool = False
    items: list[ContributionItem] = Field(default_factory=list)
    sum_contributions: Decimal | None = None
    residual: Decimal | None = None
    reason: str | None = None


class TrendPoint(BaseModel):
    period: str
    value: Decimal | None = None
    change: Decimal | None = None
    change_rate: Decimal | None = None


class TrendResponse(BaseModel):
    status: AnalysisStatus
    metric: str
    label: str
    unit: str
    start: date
    end: date
    direction: Literal["growth", "decline", "volatile", "flat", "unknown"] = "unknown"
    points: list[TrendPoint] = Field(default_factory=list)
    rule: str
    stats: dict[str, object] = Field(default_factory=dict)
    reason: str | None = None


class AnomalyPoint(BaseModel):
    period: str
    value: Decimal
    baseline_value: Decimal | None = None
    change_rate: Decimal | None = None
    zscore: Decimal | None = None
    direction: Literal["up", "down"]
    rule: str
    threshold: Decimal
    reason: str | None = None


class AnomalyResponse(BaseModel):
    status: AnalysisStatus
    metric: str
    label: str
    unit: str
    start: date
    end: date
    method: str
    points: list[AnomalyPoint] = Field(default_factory=list)
    rule: str
    threshold: Decimal
    reason: str | None = None
