from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..analysis.contracts import (
    AnalysisStatus,
    AnomalyResponse,
    ContributionResponse,
    DimensionFilter,
    MetricQueryResult,
    PeriodComparisonResponse,
    TimeGrain,
    TopNResponse,
    TrendResponse,
)
from ..analysis.time_ranges import parse_period_end_value, parse_period_value
from ..llm.schemas import IntentPlan
from ..metric_resolver import ResolutionCandidate


class ResultType(str, Enum):
    METRIC_QUERY = "metric_query"
    COMPARISON = "comparison"
    TOP_N = "top_n"
    CONTRIBUTION = "contribution"
    TREND = "trend"
    ANOMALY = "anomaly"
    INVESTIGATION = "investigation"
    SQL_QUERY = "sql_query"


class ErrorInfo(BaseModel):
    code: str
    message: str
    stage: str = "pipeline"
    detail: dict[str, Any] = Field(default_factory=dict)


class NaturalLanguageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2_000)
    start: date | None = None
    end: date | None = None
    as_of: date | None = None
    max_rows: int | None = Field(default=None, ge=1, le=1_000)
    allow_sql_fallback: bool = False

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must not be blank")
        return value

    @field_validator("start", "as_of", mode="before")
    @classmethod
    def _parse_start(cls, value):
        return None if value in (None, "") else parse_period_value(value)

    @field_validator("end", mode="before")
    @classmethod
    def _parse_end(cls, value):
        return None if value in (None, "") else parse_period_end_value(value)


class SQLQueryResult(BaseModel):
    status: AnalysisStatus
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    sql_hash: str
    tables: list[str] = Field(default_factory=list)
    explanation: str | None = None


class Observation(BaseModel):
    name: str
    status: AnalysisStatus
    metric: str | None = None
    label: str | None = None
    period: str | None = None
    baseline_period: str | None = None
    value: Decimal | None = None
    baseline_value: Decimal | None = None
    change_rate: Decimal | None = None
    unit: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    sql_hash: str | None = None


class InvestigationResponse(BaseModel):
    status: AnalysisStatus
    question: str
    scope: dict[str, Any] = Field(default_factory=dict)
    observations: list[Observation] = Field(default_factory=list)
    inference: str
    inference_type: Literal["observational"] = "observational"
    limitations: list[str] = Field(default_factory=list)
    disclaimers: list[str] = Field(default_factory=list)


AnalysisResult = (
    MetricQueryResult
    | PeriodComparisonResponse
    | TopNResponse
    | ContributionResponse
    | TrendResponse
    | AnomalyResponse
    | InvestigationResponse
    | SQLQueryResult
)


class NaturalLanguageResponse(BaseModel):
    request_id: str
    question: str
    status: AnalysisStatus
    planner: Literal["rule", "llm", "none"] = "none"
    planner_model: str | None = None
    intent: IntentPlan | None = None
    metric: str | None = None
    metric_label: str | None = None
    result_type: ResultType | None = None
    result: AnalysisResult | None = None
    candidates: list[ResolutionCandidate] = Field(default_factory=list)
    explanation: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    data_sources: list[str] = Field(default_factory=list)
    sql_hash: str | None = None
    llm_latency_ms: float | None = None
    llm_tokens: dict[str, int] = Field(default_factory=dict)


class StructuredQueryRequest(BaseModel):
    """Structured metric query exposed by the HTTP API.

    This deliberately reuses the Phase 2 request shape and never accepts raw SQL.
    """

    model_config = ConfigDict(extra="forbid")
    metric: str
    start: date | None = None
    end: date | None = None
    grain: TimeGrain = TimeGrain.MONTH
    dimensions: list[str] = Field(default_factory=list)
    filters: list[DimensionFilter] = Field(default_factory=list)
    aggregation: Literal["sum", "avg", "last", "count", "ratio"] | None = None

    @field_validator("start", mode="before")
    @classmethod
    def _parse_start(cls, value):
        return None if value in (None, "") else parse_period_value(value)

    @field_validator("end", mode="before")
    @classmethod
    def _parse_end(cls, value):
        return None if value in (None, "") else parse_period_end_value(value)
