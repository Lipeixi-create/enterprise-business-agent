from __future__ import annotations

from pydantic import BaseModel, Field

from ..analysis.contracts import AnalysisStatus, MetricQueryResult
from ..nl.contracts import ErrorInfo


class StructuredMetricQueryResponse(BaseModel):
    request_id: str
    status: AnalysisStatus
    result: MetricQueryResult | None = None
    error: ErrorInfo | None = None


class HealthResponse(BaseModel):
    status: str
    request_id: str
    database: str
    dialect: str
    readonly: bool = True
    llm_configured: bool = False
    agent_available: bool = False
    auth_enabled: bool = False
    anonymous_allowed: bool = True
    version: str
    details: dict[str, str] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    request_id: str
    status: str = "error"
    error: ErrorInfo
