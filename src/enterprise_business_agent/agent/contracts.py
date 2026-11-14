from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentStatus(str, Enum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    NO_EVIDENCE = "no_evidence"
    NO_DATA = "no_data"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIMEOUT = "timeout"
    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_TIMEOUT = "llm_timeout"
    INVALID_ACTION = "invalid_action"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    TOOL_FAILURE = "tool_failure"
    CANCELLED = "cancelled"


class EvidenceDraft(BaseModel):
    """Phase 5 placeholder: enough to trace a tool result, not a full ledger."""

    evidence_id: str
    tool_call_id: str
    metric: str | None = None
    time_range_start: str | None = None
    time_range_end: str | None = None
    baseline_period: str | None = None
    baseline_time_range_start: str | None = None
    baseline_time_range_end: str | None = None
    filters: list[dict[str, Any]] = Field(default_factory=list)
    result_summary: str
    source: str | None = None
    traceable: bool = False
    missing_fields: list[str] = Field(default_factory=list)


class ToolCallRecord(BaseModel):
    call_id: str
    tool: str
    arguments: dict[str, Any]
    status: str
    summary: str | None = None
    duration_ms: float = 0.0
    evidence_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    reused: bool = False
    row_count: int | None = None
    displayed_count: int | None = None
    truncated: bool | None = None


class ModelToolMessage(BaseModel):
    """The tool message the runtime actually handed to the LLM client.

    ``content`` is captured at the moment the message is appended to the
    conversation, not rebuilt afterwards. ``trusted`` marks messages whose
    values may be used for number verification: the tool succeeded, the call
    produced evidence, and every evidence record is traceable.
    """

    tool_call_id: str
    tool: str
    requested_tool: str | None = None
    status: str
    reused: bool = False
    trusted: bool = False
    evidence_ids: list[str] = Field(default_factory=list)
    arguments: dict[str, Any] = Field(default_factory=dict)
    content: str
    content_sha256: str
    content_bytes: int
    visible_data: dict[str, Any] | None = None
    row_count: int | None = None
    displayed_count: int | None = None
    truncated: bool | None = None
    created_at: datetime = Field(default_factory=datetime.now)


class AgentStep(BaseModel):
    index: int
    action: Literal["call_tool", "call_tool_blocked", "finish", "stop"]
    tool: str | None = None
    reason: str | None = None
    tool_call_id: str | None = None
    blocked_reason: str | None = None
    remaining_seconds: float | None = None
    remaining_tool_calls: int | None = None
    retry_count: int | None = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class AgentStateSnapshot(BaseModel):
    goal: str
    executed_steps: list[AgentStep] = Field(default_factory=list)
    observations: list[ToolCallRecord] = Field(default_factory=list)
    verified_findings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls_used: int = 0
    max_tool_calls: int = 0
    remaining_budget: int = 0
    duplicate_tool_calls: int = 0
    budget_stop_reason: str | None = None
    stop_reason: str | None = None
    failure_category: str | None = None
    tool_messages: list[ModelToolMessage] = Field(default_factory=list)
    relation_issues: list[dict[str, Any]] = Field(default_factory=list)


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2_000)
    max_tool_calls: int | None = Field(default=None, ge=1, le=20)
    max_seconds: float | None = Field(default=None, gt=0, le=300)


class AgentRunResponse(BaseModel):
    run_id: str
    request_id: str
    question: str
    status: AgentStatus
    stop_reason: str | None = None
    planner: str = "langgraph"
    planner_model: str | None = None
    llm_provider: str | None = None
    prompt_version: str | None = None
    principal_id: str
    role: str
    final_answer: str | None = None
    verified_findings: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    blocked_tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    tool_messages: list[ModelToolMessage] = Field(default_factory=list)
    relation_issues: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[EvidenceDraft] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    report_id: str | None = None
    report_version: int | None = None
    report_status: str | None = None
    report_validation_passed: bool | None = None
    approval_id: str | None = None
    approval_state: str | None = None
    approval_required: bool | None = None
    approval_fingerprint: str | None = None
    state: AgentStateSnapshot
    started_at: datetime
    finished_at: datetime
    llm_latency_ms: float | None = None
    llm_tokens: dict[str, int] = Field(default_factory=dict)
    execution_seconds: float = 0.0
    lifecycle_limit_seconds: float | None = None
    resume_count: int = 0
    last_resumed_at: str | None = None
