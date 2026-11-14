from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ..analysis.contracts import DimensionFilter, TimeGrain
from ..analysis.time_ranges import parse_period_end_value, parse_period_value


Operation = Literal["query", "compare", "top_n", "contribution", "trend", "anomaly", "investigation"]
ComparisonKind = Literal["mom", "yoy", "qoq", "yoy_quarter", "yoy_year"]


class IntentPlan(BaseModel):
    """The only structure the LLM may produce before the semantic layer runs."""

    model_config = ConfigDict(extra="forbid")
    operation: Operation = "query"
    metric: str | None = None
    metric_candidates: list[str] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None
    period: date | None = None
    year: int | None = Field(default=None, ge=2000, le=2100)
    quarter: int | None = Field(default=None, ge=1, le=4)
    grain: TimeGrain = TimeGrain.MONTH
    dimensions: list[str] = Field(default_factory=list)
    filters: list[DimensionFilter] = Field(default_factory=list)
    comparison_kind: ComparisonKind | None = None
    dimension: str | None = None
    n: int | None = Field(default=None, ge=1, le=100)
    ascending: bool = False
    method: Literal["mom_threshold", "zscore"] = "mom_threshold"
    threshold: float = Field(default=0.20, gt=0)
    region: str | None = None
    products: list[str] = Field(default_factory=list)
    baseline_start: date | None = None
    baseline_end: date | None = None
    rationale: str | None = None

    @field_validator("start", "period", "baseline_start", mode="before")
    @classmethod
    def _parse_start(cls, value):
        return None if value in (None, "") else parse_period_value(value)

    @field_validator("end", "baseline_end", mode="before")
    @classmethod
    def _parse_end(cls, value):
        return None if value in (None, "") else parse_period_end_value(value)


class CandidateSQLPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str
    params: dict[str, object] = Field(default_factory=dict)
    rationale: str | None = None


class IntentPlanSchema(BaseModel):
    """Wrapper used in prompts/evals so the expected top-level key is explicit."""

    model_config = ConfigDict(extra="forbid")
    plan: IntentPlan


def validated_intent(data: dict) -> IntentPlan:
    return IntentPlan.model_validate(data)
