from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


MetricType = Literal["additive", "semi_additive", "ratio"]
Additivity = Literal["additive", "semi_additive", "non_additive"]
AggregationKind = Literal["sum", "avg", "last", "ratio", "count"]
TimeGrain = Literal["month", "quarter", "year", "total"]


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    label: str
    synonyms: list[str] = Field(default_factory=list)
    domain: str
    description: str
    metric_type: MetricType
    additivity: Additivity
    source_table: str
    source_fields: list[str] = Field(default_factory=list)
    expression: str
    numerator: str
    denominator: str | None = None
    numerator_aggregation: AggregationKind = "sum"
    denominator_aggregation: AggregationKind = "sum"
    aggregation: str
    allowed_aggregations: list[AggregationKind] = Field(default_factory=list)
    formula: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    time_grains: list[TimeGrain] = Field(default_factory=lambda: ["month", "quarter", "year", "total"])
    unit: str
    nullable_when: str | None = None
    zero_denominator: Literal["null", "error"] = "null"
    owner: str = "unknown"
    version: str = "2.0.0"


class DimensionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    label: str
    kind: Literal["time", "categorical"]
    synonyms: list[str] = Field(default_factory=list)
    time_grains: list[TimeGrain] = Field(default_factory=list)
    table: str | None = None
    fact_column: str | None = None
    key_column: str | None = None
    code_column: str | None = None
    name_column: str | None = None
    values: list[dict[str, str]] = Field(default_factory=list)


class EventAssertion(BaseModel):
    name: str
    passed: bool
    detail: str
    observed: dict[str, float | int | str | None] = Field(default_factory=dict)


class DataQualityReport(BaseModel):
    passed: bool
    checks: list[str]
    errors: list[str] = Field(default_factory=list)
    row_counts: dict[str, int] = Field(default_factory=dict)
    period_start: date | None = None
    period_end: date | None = None
    sample_metric_values: dict[str, Decimal | float | int | None] = Field(default_factory=dict)
    event_assertions: list[EventAssertion] = Field(default_factory=list)

