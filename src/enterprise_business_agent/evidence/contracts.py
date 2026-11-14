from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EvidenceStatus = Literal["valid", "insufficient_data", "not_calculable", "failed", "rejected"]
EvidenceType = Literal[
    "metric_value",
    "comparison",
    "ranking",
    "contribution",
    "trend",
    "anomaly",
    "metric_definition",
    "catalog",
]
FindingType = Literal[
    "fact",
    "change",
    "anomaly",
    "contribution",
    "correlation",
    "hypothesis",
    "recommendation",
]
FindingSeverity = Literal["primary", "secondary", "contributing", "context"]
Certainty = Literal["fact", "inference", "hypothesis"]


class EvidenceRecord(BaseModel):
    """A persisted, replayable fact produced by one tool execution.

    ``result`` is the tool's own JSON payload, stored verbatim. The collector
    never rewrites values, so a report can always be checked against what the
    deterministic analysis engine returned.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    run_id: str
    tool_call_id: str
    evidence_type: EvidenceType
    domain: str | None = None
    metric_code: str | None = None
    metric_definition_version: str | None = None
    time_range_start: str | None = None
    time_range_end: str | None = None
    baseline_period: str | None = None
    baseline_time_range_start: str | None = None
    baseline_time_range_end: str | None = None
    grain: str | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    dimensions: dict[str, str] = Field(default_factory=dict)
    query_parameters: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    result_summary: str | None = None
    unit: str | None = None
    source_tables: list[str] = Field(default_factory=list)
    calculation_method: str | None = None
    status: EvidenceStatus
    error_code: str | None = None
    error_message: str | None = None
    traceable: bool = False
    fingerprint: str
    created_at: datetime

    @property
    def is_valid(self) -> bool:
        return self.status == "valid"


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    run_id: str
    title: str
    domain: str | None = None
    finding_type: FindingType
    severity: FindingSeverity = "secondary"
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_basis: str
    explanation: str | None = None
    limitations: list[str] = Field(default_factory=list)
    suggested_validation: list[str] = Field(default_factory=list)
    created_at: datetime


class ValueReference(BaseModel):
    """A number shown in a report, bound to a concrete evidence field."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    field: str
    value: str
    unit: str | None = None


class Conclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str
    finding_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    certainty: Certainty
    value_refs: list[ValueReference] = Field(default_factory=list)


class MetricCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: str
    label: str
    period: str
    value: ValueReference
    change: ValueReference | None = None
    status: EvidenceStatus


class DomainSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain: str
    narrative: str
    finding_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    rationale: str
    finding_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    expected_direction: str
    risk: str
    validation_metric: str
    requires_approval: bool = False


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    tool_call_id: str
    evidence_type: str
    metric: str | None = None
    status: str
    source_tables: list[str] = Field(default_factory=list)


class DataScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric_codes: list[str] = Field(default_factory=list)
    metric_definition_versions: list[str] = Field(default_factory=list)
    time_ranges: list[dict[str, str | None]] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)
    grains: list[str] = Field(default_factory=list)
    source_tables: list[str] = Field(default_factory=list)


class ReportInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    run_id: str
    version: int
    generated_at: str
    question: str
    principal_id: str
    role: str
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None


class ReportDocument(BaseModel):
    """Structured report contract. Every number is a value reference."""

    model_config = ConfigDict(extra="forbid")

    report_info: ReportInfo
    objective: str
    executive_summary: str = ""
    conclusions: list[Conclusion] = Field(default_factory=list)
    key_metrics: list[MetricCard] = Field(default_factory=list)
    domain_analysis: list[DomainSection] = Field(default_factory=list)
    anomalies_and_contributions: list[Conclusion] = Field(default_factory=list)
    cross_domain: list[Conclusion] = Field(default_factory=list)
    evidence_references: list[EvidenceReference] = Field(default_factory=list)
    risks_and_uncertainty: list[str] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    next_validation: list[str] = Field(default_factory=list)
    data_scope: DataScope
    narrative_mode: Literal["deterministic", "llm", "partial"] = "deterministic"
    warnings: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    location: str | None = None
    evidence_id: str | None = None
    category: Literal["business_result", "tool_parameter", "date_component", "unsupported_number"] | None = None


class NumberClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str
    category: Literal["business_result", "tool_parameter", "date_component", "unsupported_number"]
    allowed: bool
    location: str | None = None


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    checks: list[str] = Field(default_factory=list)
    issues: list[ValidationIssue] = Field(default_factory=list)
    unverified_numbers: list[str] = Field(default_factory=list)
    numbers: list[NumberClassification] = Field(default_factory=list)


class StoredReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str
    run_id: str
    version: int
    status: Literal["validated", "rejected"]
    report: ReportDocument
    validation: ValidationResult
    created_at: datetime
