from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from bisect import bisect_right
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from ..llm import LLMToolCall, LLMToolResponse, ModelCallBudgetExceeded
from ..security import Principal
from ..security.auth import ROLE_SCOPES
from .graph import AgentRuntime, _derived_candidates, _matches_derived
from .tools import ToolRegistry

# Tools whose successful execution reads business data. Reused (deduped) calls
# are not executions and are counted separately.
_DATA_QUERY_TOOLS = frozenset(
    {
        "query_metric",
        "compare_periods",
        "top_n",
        "contribution",
        "trend",
        "anomaly_scan",
    }
)


class PlannedCall(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class QueryCheck(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    path: str
    min: float | None = None
    max: float | None = None
    equals: str | None = None


class AgentEvalCase(BaseModel):
    id: str
    question: str
    plan: list[PlannedCall]
    query_check: QueryCheck
    conclusion_terms: list[str] = Field(default_factory=list)
    expected_status: str = "completed"


class EvalCaseResult(BaseModel):
    id: str
    question: str
    planning_correct: bool
    query_correct: bool
    conclusion_correct: bool
    status: str
    actual_plan: list[PlannedCall] = Field(default_factory=list)
    final_answer: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


DEFAULT_REFUSAL_TERMS = [
    "无法",
    "不能",
    "无权",
    "没有权限",
    "权限不足",
    "未提供",
    "不支持",
    "无法访问",
    "未授权",
]


class LiveEvalCase(BaseModel):
    """A human-checkable case for a real LLM run.

    ``query_check`` is executed independently against the same deterministic
    database; its expected range is a measured fact, not a success target.
    ``plan_alternatives`` allows more than one defensible tool plan (for
    example, describing a metric before choosing between two candidates); the
    best matching alternative is scored.
    """

    id: str
    category: str
    question: str
    domain: str = ""
    required_calls: list[PlannedCall] = Field(default_factory=list)
    plan_alternatives: list[list[PlannedCall]] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    query_check: QueryCheck | None = None
    expect_canonical_status: str = "ok"
    conclusion_terms: list[str] = Field(default_factory=list)
    allowed_statuses: list[str] = Field(
        default_factory=lambda: ["completed", "completed_with_warnings"]
    )
    expect_evidence: bool = True
    expect_refusal: bool = False
    refusal_terms: list[str] = Field(default_factory=list)
    expect_number: bool = True
    min_answer_chars: int = 30
    role: str = "analyst"
    # Only set this for questions whose wording fixes the sequence ("先…再…").
    # Any order is otherwise acceptable because tool plans are not ordered by
    # the runtime contract.
    require_order: bool = False
    scored: bool = True
    human_check: str = ""


class LiveEvalResult(BaseModel):
    id: str
    category: str
    question: str
    domain: str = ""
    scored: bool
    planning_correct: bool
    tool_selection_correct: bool
    tool_argument_correct: bool
    plan_order_correct: bool | None = None
    extra_tool_calls: int = 0
    repeated_tool_calls: int = 0
    duplicate_tool_calls: int = 0
    query_correct: bool | None
    evidence_sufficient: bool
    conclusion_overreach: bool
    task_completed: bool
    passed: bool
    status: str
    llm_provider: str | None = None
    llm_model: str | None = None
    prompt_version: str | None = None
    final_answer: str | None = None
    actual_plan: list[PlannedCall] = Field(default_factory=list)
    matching_summaries: list[str] = Field(default_factory=list)
    canonical_summary: str | None = None
    observed: Any = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    failure_reasons: list[str] = Field(default_factory=list)
    tool_calls: int = 0
    attempted_tool_calls: int = 0
    successful_data_tool_calls: int = 0
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_trace: list[dict[str, Any]] = Field(default_factory=list)
    tool_messages: list[dict[str, Any]] = Field(default_factory=list)
    relation_issues: list[dict[str, Any]] = Field(default_factory=list)
    relation_validation_passed: bool | None = None
    budget_stop_reason: str | None = None
    failure_category: str | None = None
    evidence_count: int = 0
    traceable_evidence: int = 0
    llm_latency_ms: float | None = None
    llm_tokens: dict[str, int] = Field(default_factory=dict)
    duration_ms: float = 0.0
    model_calls: int = 0
    evidence_citation_completeness: float | None = None
    finding_citation_correct: bool | None = None
    numeric_validation_passed: bool | None = None
    numeric_issue_count: int = 0
    report_structure_completeness: float | None = None
    report_narrative_mode: str | None = None
    evidence_chain_valid: bool | None = None
    evidence_chain_issue_count: int = 0
    evidence_chain_issues: list[dict[str, Any]] = Field(default_factory=list)
    number_accuracy: bool | None = None
    answer_number_provenance: bool | None = None
    unsourced_numbers: list[str] = Field(default_factory=list)
    cited_evidence_ids: list[str] = Field(default_factory=list)
    answer_citations_valid: bool | None = None
    safety_intercepted: bool | None = None
    refusal_correct: bool | None = None
    narrative_complete: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class ScriptedPlanLLM:
    """TEST FAKE: replays a fixed tool plan; it is not a real model.

    It is used only to exercise the LangGraph control loop and deterministic
    query checks. Results must never be reported as real LLM effectiveness.
    """

    model = "fake-offline-plan-llm"

    def __init__(self, plan: list[PlannedCall]):
        self.plan = plan
        self.index = 0

    def complete_with_tools(
        self,
        *,
        system,
        messages,
        tools,
        timeout_seconds: float | None = None,
        max_tokens: int | None = None,
    ):
        if self.index < len(self.plan):
            call = self.plan[self.index]
            self.index += 1
            return LLMToolResponse(
                text=f"执行计划步骤 {self.index}",
                tool_calls=[
                    LLMToolCall(
                        call_id=f"fake-{self.index}",
                        name=call.tool,
                        arguments=call.arguments,
                    )
                ],
                stop_reason="tool_use",
                model=self.model,
            )
        summaries = [
            json.loads(item["content"]).get("summary")
            for item in messages
            if item.get("role") == "tool"
        ]
        text = "；".join(value for value in summaries if value)
        return LLMToolResponse(
            text=f"根据工具结果：{text}",
            tool_calls=[],
            stop_reason="end_turn",
            model=self.model,
        )


def load_agent_cases(path: str | Path) -> list[AgentEvalCase]:
    cases: list[AgentEvalCase] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                cases.append(AgentEvalCase.model_validate_json(line))
    return cases


def _lookup(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def run_agent_case(
    case: AgentEvalCase,
    *,
    runtime: AgentRuntime,
    registry: ToolRegistry,
    principal: Principal,
) -> EvalCaseResult:
    llm = ScriptedPlanLLM(case.plan)
    runtime.llm = llm
    result = runtime.run(case.question, principal)

    actual = [PlannedCall(tool=item.tool, arguments=item.arguments) for item in result.tool_calls]
    planning_correct = actual == case.plan

    check = registry.execute(
        case.query_check.tool,
        case.query_check.arguments,
        principal,
        call_id=f"eval-{case.id}",
    )
    query_correct = False
    observed: Any = None
    if check.status == "ok":
        try:
            observed = _lookup(check.data, case.query_check.path)
            if case.query_check.equals is not None:
                query_correct = str(observed) == case.query_check.equals
            else:
                numeric = float(observed)
                query_correct = (
                    (case.query_check.min is None or numeric >= case.query_check.min)
                    and (case.query_check.max is None or numeric <= case.query_check.max)
                )
        except (KeyError, IndexError, TypeError, ValueError):
            query_correct = False

    answer = result.final_answer or ""
    conclusion_correct = (
        bool(answer)
        and all(term in answer for term in case.conclusion_terms)
        and not any("无法在工具证据中直接核对" in item for item in result.warnings)
    )
    return EvalCaseResult(
        id=case.id,
        question=case.question,
        planning_correct=planning_correct,
        query_correct=query_correct,
        conclusion_correct=conclusion_correct,
        status=result.status.value,
        actual_plan=actual,
        final_answer=result.final_answer,
        detail={
            "expected_status": case.expected_status,
            "status_correct": result.status.value == case.expected_status,
            "query_status": check.status,
            "observed": observed,
            "tool_calls": len(result.tool_calls),
            "planner_is_fake": True,
        },
    )


def load_live_agent_cases(path: str | Path) -> list[LiveEvalCase]:
    cases: list[LiveEvalCase] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                cases.append(LiveEvalCase.model_validate_json(line))
    return cases


def _args_match(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(
            key in actual and _args_match(actual[key], value)
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        remaining = list(actual)
        for item in expected:
            index = next(
                (i for i, candidate in enumerate(remaining) if _args_match(candidate, item)),
                None,
            )
            if index is None:
                return False
            remaining.pop(index)
        return True
    return actual == expected


def _canonical_check(
    check: QueryCheck,
    registry: ToolRegistry,
    principal: Principal,
    *,
    expected_status: str = "ok",
) -> tuple[bool, Any, str | None, str | None]:
    result = registry.execute(
        check.tool,
        check.arguments,
        principal,
        call_id=f"canonical-{check.tool}",
    )
    if expected_status != "ok":
        correct = result.status == expected_status
        return (
            correct,
            None,
            result.summary,
            None if correct else f"canonical status={result.status}, expected {expected_status}",
        )
    if result.status != "ok":
        return False, None, result.summary, f"canonical query status={result.status}"
    try:
        observed = _lookup(result.data, check.path)
        if check.equals is not None:
            correct = str(observed) == check.equals
        else:
            numeric = float(observed)
            correct = (
                (check.min is None or numeric >= check.min)
                and (check.max is None or numeric <= check.max)
            )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return False, None, result.summary, f"canonical check failed: {type(exc).__name__}"
    return correct, observed, result.summary, None


class _RecordingRegistry:
    """Delegates to the real registry and keeps each ToolResult for the report."""

    def __init__(self, inner: ToolRegistry):
        self._inner = inner
        self.results: dict[str, tuple[str, dict[str, Any], Any]] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, name, arguments, principal, *, call_id, timeout_seconds=None):
        result = self._inner.execute(
            name,
            arguments,
            principal,
            call_id=call_id,
            timeout_seconds=timeout_seconds,
        )
        self.results[call_id] = (name, dict(arguments), result)
        return result


class _CountingLLM:
    """Counts real model calls without changing the response.

    A call refused by the run's local cap never reached the provider, so it is
    not counted; otherwise the archive would report one more call than was sent.
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self.calls = 0
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def complete_with_tools(self, **kwargs):
        started = time.monotonic()
        self.calls += 1
        budget_seconds = kwargs.get("timeout_seconds")
        try:
            response = self._inner.complete_with_tools(**kwargs)
        except ModelCallBudgetExceeded:
            self.calls -= 1
            raise
        except Exception as exc:
            # A timed-out or failed call still consumed wall time; record it so
            # the archive can explain where a run's latency went.
            self.events.append(
                {
                    "call": self.calls,
                    "status": type(exc).__name__,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "budget_seconds": budget_seconds,
                    # A raised call reached the provider at least once unless
                    # the client marked it budget-limited before sending.
                    "attempts": getattr(exc, "attempts", 1),
                    "budget_limited": bool(getattr(exc, "budget_limited", False)),
                }
            )
            raise
        self.events.append(
            {
                "call": self.calls,
                "status": "ok",
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "budget_seconds": budget_seconds,
                "latency_ms": getattr(response, "latency_ms", None),
                "input_tokens": getattr(response, "input_tokens", None),
                "output_tokens": getattr(response, "output_tokens", None),
                "attempts": getattr(response, "attempts", None),
                "budget_limited": False,
                "tools": [
                    call.name for call in (getattr(response, "tool_calls", None) or [])
                ],
                "has_text": bool(getattr(response, "text", None)),
            }
        )
        return response


_EVAL_ENGINE: Engine | None = None


def _eval_engine() -> Engine:
    """A private SQLite file used only to build eval reports in-process."""

    global _EVAL_ENGINE
    if _EVAL_ENGINE is None:
        directory = Path(tempfile.mkdtemp(prefix="eba-phase7-eval-"))
        _EVAL_ENGINE = create_engine(f"sqlite+pysqlite:///{directory / 'reports.db'}")
    return _EVAL_ENGINE


_REPORT_BLOCKS = (
    "objective",
    "executive_summary",
    "conclusions",
    "key_metrics",
    "evidence_references",
    "risks_and_uncertainty",
    "recommendations",
    "next_validation",
    "data_scope",
)


def _report_dimensions(
    *,
    recorded: dict[str, tuple[str, dict[str, Any], Any]],
    run_id: str,
    case_id: str,
    question: str,
    principal: Principal,
    provider: str | None,
    model: str | None,
    prompt_version: str | None,
    stop_reason: str | None,
    archived_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the deterministic report for this run and score its structure.

    Narrative generation is disabled: the eval measures evidence/finding
    citation and numeric validation, not the optional model-written prose.
    """

    if not recorded:
        return {
            "evidence_citation_completeness": None,
            "finding_citation_correct": None,
            "numeric_validation_passed": None,
            "numeric_issue_count": 0,
            "report_structure_completeness": None,
            "report_narrative_mode": None,
            "evidence_chain_valid": None,
            "evidence_chain_issue_count": 0,
            "evidence_chain_issues": [],
        }

    from ..evidence.pipeline import Phase5Pipeline

    pipeline = Phase5Pipeline(_eval_engine(), narrative_enabled=False)
    for call_id, (name, arguments, result) in recorded.items():
        pipeline.record_tool_result(
            run_id=run_id,
            tool_call_id=call_id,
            tool=name,
            arguments=arguments,
            result=result,
        )
    pipeline.finalize(
        run_id=run_id,
        question=question,
        principal_id=principal.principal_id,
        role=principal.role,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        stop_reason=stop_reason,
    )
    stored = pipeline.report_store.latest(run_id)
    if stored is None:
        return {
            "evidence_citation_completeness": None,
            "finding_citation_correct": None,
            "numeric_validation_passed": None,
            "numeric_issue_count": 0,
            "report_structure_completeness": None,
            "report_narrative_mode": None,
            "evidence_chain_valid": None,
            "evidence_chain_issue_count": 0,
            "evidence_chain_issues": [],
        }

    evidences = pipeline.ledger.list(run_id)
    findings = pipeline.finding_store.list(run_id)
    evidence_ids = {item.evidence_id for item in evidences}
    finding_ids = {item.finding_id for item in findings}
    document = stored.report

    cited: set[str] = set()
    for conclusion in (
        document.conclusions
        + document.anomalies_and_contributions
        + document.cross_domain
    ):
        cited.update(conclusion.evidence_ids)
    for section in document.domain_analysis:
        cited.update(section.evidence_ids)
    for card in document.key_metrics:
        cited.add(card.value.evidence_id)
        if card.change is not None:
            cited.add(card.change.evidence_id)

    completeness = (
        round(len(cited & evidence_ids) / len(evidence_ids), 4) if evidence_ids else None
    )

    finding_correct = True
    for finding in findings:
        if not finding.evidence_ids or not set(finding.evidence_ids) <= evidence_ids:
            finding_correct = False
            break
    if finding_correct:
        for conclusion in (
            document.conclusions
            + document.anomalies_and_contributions
            + document.cross_domain
        ):
            if not set(conclusion.finding_ids) <= finding_ids:
                finding_correct = False
                break
            if not set(conclusion.evidence_ids) <= evidence_ids:
                finding_correct = False
                break

    filled = 0
    values = {
        "objective": bool(document.objective.strip()),
        "executive_summary": bool(document.executive_summary.strip()),
        "conclusions": bool(document.conclusions),
        "key_metrics": bool(document.key_metrics),
        "evidence_references": bool(document.evidence_references),
        "risks_and_uncertainty": bool(document.risks_and_uncertainty),
        "recommendations": bool(document.recommendations),
        "next_validation": bool(document.next_validation),
        "data_scope": bool(
            document.data_scope.metric_codes
            or document.data_scope.time_ranges
            or document.data_scope.source_tables
        ),
    }
    # Recommendations only apply when the report raises something actionable.
    # A single-value lookup should not be marked incomplete for correctly
    # declining to invent advice.
    actionable = bool(
        len(document.conclusions) > 1
        or document.anomalies_and_contributions
        or document.cross_domain
    )
    applicable = {name: True for name in _REPORT_BLOCKS}
    applicable["recommendations"] = actionable
    for name in _REPORT_BLOCKS:
        if applicable[name] and values[name]:
            filled += 1
    denominator = sum(1 for name in _REPORT_BLOCKS if applicable[name])

    chain_issues = _evidence_chain_issues(
        run_id=run_id,
        case_id=case_id,
        evidences=evidences,
        findings=findings,
        recorded=recorded,
        archived_messages=archived_messages,
    )

    return {
        "evidence_citation_completeness": completeness,
        "finding_citation_correct": finding_correct,
        "numeric_validation_passed": stored.validation.passed,
        "numeric_issue_count": len(stored.validation.issues),
        "report_structure_completeness": round(filled / denominator, 4),
        "report_structure_blocks": values,
        "report_structure_applicable": applicable,
        "report_narrative_mode": document.narrative_mode,
        "evidence_chain_valid": not chain_issues,
        "evidence_chain_issue_count": len(chain_issues),
        "evidence_chain_issues": chain_issues,
    }


def _evidence_chain_issues(
    *,
    run_id: str,
    case_id: str,
    evidences: list[Any],
    findings: list[Any],
    recorded: dict[str, tuple[str, dict[str, Any], Any]],
    archived_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Check that every evidence/finding/message belongs to this run and case.

    A report can reference an ID that exists in the ledger but was produced by
    another run. Subset checks alone cannot see that, so this validates the
    run/case/tool-call join explicitly.
    """

    issues: list[dict[str, Any]] = []
    known_evidence = {item.evidence_id for item in evidences}
    known_calls = set(recorded)

    def resolve_evidence_id(value: str) -> str:
        # Tool messages may carry the pre-persistence call-local ID
        # ("call-1-e1"); the ledger prefixes it with the run. Normalizing here
        # keeps the join strict without rejecting the same run's records.
        if value in known_evidence:
            return value
        prefixed = f"{run_id[:8]}-{value}"
        return prefixed if prefixed in known_evidence else value

    for evidence in evidences:
        if evidence.run_id != run_id:
            issues.append(
                {
                    "code": "cross_run_evidence",
                    "evidence_id": evidence.evidence_id,
                    "actual_run_id": evidence.run_id,
                    "expected_run_id": run_id,
                }
            )
        if evidence.tool_call_id not in known_calls:
            issues.append(
                {
                    "code": "unknown_tool_call",
                    "evidence_id": evidence.evidence_id,
                    "tool_call_id": evidence.tool_call_id,
                }
            )

    for finding in findings:
        if finding.run_id != run_id:
            issues.append(
                {
                    "code": "cross_run_finding",
                    "finding_id": finding.finding_id,
                    "actual_run_id": finding.run_id,
                    "expected_run_id": run_id,
                }
            )
        unknown = sorted(
            {resolve_evidence_id(value) for value in finding.evidence_ids}
            - known_evidence
        )
        if unknown:
            issues.append(
                {
                    "code": "finding_unknown_evidence",
                    "finding_id": finding.finding_id,
                    "evidence_ids": unknown,
                }
            )

    for message in archived_messages:
        if message.get("run_id") != run_id:
            issues.append(
                {
                    "code": "cross_run_message",
                    "tool_call_id": message.get("tool_call_id"),
                    "actual_run_id": message.get("run_id"),
                    "expected_run_id": run_id,
                }
            )
        if message.get("case_id") != case_id:
            issues.append(
                {
                    "code": "cross_case_message",
                    "tool_call_id": message.get("tool_call_id"),
                    "actual_case_id": message.get("case_id"),
                    "expected_case_id": case_id,
                }
            )
        unknown = sorted(
            {
                resolve_evidence_id(value)
                for value in message.get("evidence_ids") or []
            }
            - known_evidence
        )
        if unknown:
            issues.append(
                {
                    "code": "message_unknown_evidence",
                    "tool_call_id": message.get("tool_call_id"),
                    "evidence_ids": unknown,
                }
            )
    return issues


_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*\s*(?:%|亿|万|千)?")
_UNITS = {"亿": Decimal(100_000_000), "万": Decimal(10_000), "千": Decimal(1_000)}
# Semicolons separate clauses, not sentences: a value can follow its metric
# across a semicolon ("库存天数：P001=3；P002=5"). Sentence boundaries stay
# hard so a term cannot reach into an unrelated conclusion.
_SEGMENT_BOUNDARY = "。！？\n"
_CLAUSE_BOUNDARY_RE = re.compile(r"[，、；;：:]|,(?!\d)")
# Clauses that continue the value of a metric named just before them. A figure
# in one of these clauses may inherit that metric; a clause naming another
# metric first (e.g. "净利润为 ...") does not match and never inherits.
_CONTINUATION_PREFIXES = (
    "首条值",
    "末条值",
    "首值",
    "末值",
    "第一条",
    "最后一条",
    "其中",
    "分别",
    "依次",
    "合计",
    "总计",
    "小计",
    "约为",
    "同比",
    "环比",
    "增长",
    "下降",
    "上升",
    "变化",
    "增幅",
    "降幅",
    "增速",
    "较",
    "相比",
    "为",
    "是",
    "即",
    "对应",
    "值",
)
_LABEL_SUFFIXES = ("：", ":", "为", "是", "如下", "分别", "依次")
_BULLET_PREFIX_RE = re.compile(r"^(?:[-*+]\s*|\d+[.、)]\s*)")
# ``P001=3`` is one member of a list introduced by a metric label. Only an
# ASCII key counts: a Chinese phrase followed by ``=`` could just as well be a
# second metric and must not be crossed while looking for the target term.
_LIST_MEMBER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{0,19}\s*=")
# A bullet line whose label looks like another metric is a competing figure,
# not another member of the list above it, so it cannot be crossed.
_GENERIC_VALUE_LABELS = (
    "总额",
    "合计",
    "总计",
    "小计",
    "数值",
    "当前值",
    "基线值",
    "平均值",
    "均值",
    "变化率",
    "变化量",
    "变化",
    "增长",
    "下降",
    "上升",
    "环比",
    "同比",
    "增幅",
    "降幅",
    "增速",
    "差额",
    "差异",
)
_METRIC_LABEL_HINTS = (
    "率",
    "额",
    "数",
    "量",
    "成本",
    "收入",
    "利润",
    "费用",
    "占比",
    "天数",
    "单价",
    "金额",
    "规模",
)
# Columns such as "贡献变化" or "变化额" are derived from the table's metric,
# so a figure under them belongs to the same metric context as the header.
_DERIVED_COLUMN_HINTS = ("贡献", "变化", "增减", "差额", "差异")
# Below this, a number in prose is usually a count, an ordinal or a date part
# rather than a business value, so it is not treated as an unsourced figure.
_BUSINESS_NUMBER_FLOOR = Decimal("1000")
_CITATION_RE = re.compile(
    r"\[\s*(?:E|EV|ev|evidence)[-_ ]?\d+\s*\]", re.IGNORECASE
)
_EVIDENCE_ID_RE = re.compile(r"\b[0-9a-f]{6,}(?:-[A-Za-z0-9]+)+-e\d+\b")
# Call ids look like ``call_-7121727578372566844-e1``. Their digits are not
# business values, so they must be blanked before any number extraction;
# otherwise the leading ``-`` makes the id parse as a huge negative number.
_IDENTIFIER_RE = re.compile(
    r"\b(?:call|evidence|ev|run|tool|trace|span)[_-][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*",
    re.IGNORECASE,
)


def _mask_identifiers(text: str) -> str:
    """Blank out identifier spans while preserving length and offsets."""

    if not text:
        return text or ""
    return _IDENTIFIER_RE.sub(lambda match: " " * len(match.group(0)), text)


def _inside_identifier(text: str, start: int, end: int) -> bool:
    """True when a numeric match is really the digit part of an identifier."""

    if start > 0 and text[start - 1] == "_":
        return True
    return bool(re.match(r"-e\d", text[end : end + 4]))


def _parse_number(raw: str) -> tuple[Decimal, bool] | None:
    """Return ``(value, is_percent)`` for one matched number token."""

    raw = raw.strip()
    is_percent = False
    unit: Decimal | None = None
    if raw.endswith("%"):
        is_percent = True
        raw = raw[:-1].strip()
    else:
        for candidate in _UNITS:
            if raw.endswith(candidate):
                unit = _UNITS[candidate]
                raw = raw[: -len(candidate)].strip()
                break
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    if unit is not None:
        value *= unit
    return value, is_percent


def _number_tokens(text: str) -> list[tuple[int, int, Decimal, bool]]:
    tokens: list[tuple[int, int, Decimal, bool]] = []
    masked = _mask_identifiers(text or "")
    for match in _NUMBER_RE.finditer(masked):
        if _inside_identifier(masked, match.start(), match.end()):
            continue
        parsed = _parse_number(match.group(0))
        if parsed is None:
            continue
        value, is_percent = parsed
        tokens.append((match.start(), match.end(), value, is_percent))
    return tokens


def _numbers_in(text: str) -> list[Decimal]:
    return [value for _, _, value, _ in _number_tokens(text)]


def _segments(text: str) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char in _SEGMENT_BOUNDARY:
            segments.append((start, index))
            start = index + 1
    segments.append((start, len(text)))
    return segments


def _clause_spans(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split one sentence into clauses, keeping every offset intact.

    A bare comma inside ``1,000,000`` is not a separator; only a comma that is
    followed by a non-digit counts.
    """

    spans: list[tuple[int, int]] = []
    clause_start = start
    for match in _CLAUSE_BOUNDARY_RE.finditer(text, start, end):
        spans.append((clause_start, match.start()))
        clause_start = match.end()
    spans.append((clause_start, end))
    return spans


def _clause_body(text: str, span: tuple[int, int]) -> str:
    return text[span[0] : span[1]].strip()


def _is_value_continuation(fragment: str) -> bool:
    """True when a clause carries a value for the metric named before it."""

    body = _BULLET_PREFIX_RE.sub("", fragment.strip()).lstrip("：:")
    body = body.strip()
    if not body:
        return False
    if body.startswith(_CONTINUATION_PREFIXES):
        return True
    return bool(_NUMBER_RE.match(body))


def _is_label(fragment: str) -> bool:
    return fragment.strip().rstrip("*_ ").endswith(_LABEL_SUFFIXES)


def _is_heading(fragment: str) -> bool:
    """True for a Markdown heading, including a fully bold line.

    A heading may contain a date ("2025年12月各渠道转化率"); those digits are
    not business values, so the "no figures" test alone would reject it and
    lose the metric context for the bullets below.
    """

    body = fragment.strip()
    if body.startswith("#"):
        return True
    return body.startswith("**") and body.endswith("**") and len(body) > 4


def _has_list_member(fragment: str) -> bool:
    body = _BULLET_PREFIX_RE.sub("", fragment.strip())
    return bool(_LIST_MEMBER_RE.search(body))


def _is_list_line(fragment: str) -> bool:
    """True for one line of a list under a heading, e.g. ``- P001 3``."""

    body = fragment.strip()
    if not body:
        return True
    bullet = _BULLET_PREFIX_RE.match(body)
    if bullet:
        return not _clause_names_metric(body[bullet.end() :])
    return bool(re.match(r"[A-Za-z][A-Za-z0-9_.\-]*\s*[:=]?\s*[-\d]", body))


def _clause_names_metric(fragment: str) -> bool:
    """True when a clause's label reads like a metric rather than a member."""

    body = _BULLET_PREFIX_RE.sub("", fragment.strip())
    member = _LIST_MEMBER_RE.search(body)
    if member:
        body = body[: member.start()]
    label = re.split(r"\d", body, maxsplit=1)[0].strip().rstrip("：:")
    if label.startswith(_GENERIC_VALUE_LABELS):
        return False
    return any(hint in label for hint in _METRIC_LABEL_HINTS)


_RATE_LABEL_HINTS = ("变化率", "变化", "同比", "环比", "增长", "下降", "增幅", "降幅", "增速")


def _is_rate_label(fragment: str) -> bool:
    body = _BULLET_PREFIX_RE.sub("", fragment.strip())
    label = re.split(r"\d", body, maxsplit=1)[0].strip()
    return any(hint in label for hint in _RATE_LABEL_HINTS)


def _is_bare_value(fragment: str) -> bool:
    """True when a clause is only a figure (plus an optional unit)."""

    body = _BULLET_PREFIX_RE.sub("", fragment.strip()).strip("*_ ").rstrip("*_ ")
    return bool(re.fullmatch(r"[-\d.,%]+(?:\s*[A-Za-z%元万亿件天个]+)?", body))


def _nearby_heading_names(
    text: str,
    segments: list[tuple[int, int]],
    index: int,
    terms: list[str],
) -> bool:
    """True when the current or a nearby heading names the target metric."""

    if any(term in text[segments[index][0] : segments[index][1]] for term in terms):
        return True
    seen = 0
    for position in range(index - 1, -1, -1):
        low, high = segments[position]
        fragment = text[low:high]
        if not fragment.strip():
            continue
        if any(term in fragment for term in terms) and (
            _is_label(fragment) or _is_heading(fragment) or not _number_tokens(fragment)
        ):
            return True
        seen += 1
        if seen >= 2:
            break
    return False


def _prior_segment_names_metric(
    text: str,
    segments: list[tuple[int, int]],
    index: int,
    terms: list[str],
) -> bool:
    """True when one of the two preceding non-empty lines names the metric.

    A rate bullet can follow several metric bullets ("2025...毛利", "2024...毛利",
    "同比变化率：..."). Unlike a heading, those bullets carry figures, so the
    stricter heading check would reject the rate's own metric context.
    """

    seen = 0
    for position in range(index - 1, -1, -1):
        low, high = segments[position]
        fragment = text[low:high]
        if not fragment.strip():
            continue
        if any(term in fragment for term in terms):
            return True
        seen += 1
        if seen >= 2:
            break
    return False


def _is_derived_column(cell: str) -> bool:
    return any(hint in cell for hint in _DERIVED_COLUMN_HINTS)


def _pipe_cells(fragment: str) -> list[tuple[str, int, int]] | None:
    """Split a Markdown table row into ``(cell, start, end)`` positions."""

    stripped = fragment.strip()
    if not stripped.startswith("|") or "|" not in stripped[1:]:
        return None
    parts = fragment.split("|")
    cells: list[tuple[str, int, int]] = []
    offset = len(parts[0]) + 1
    for part in parts[1:]:
        cells.append((part, offset, offset + len(part)))
        offset += len(part) + 1
    if cells and not cells[-1][0].strip():
        cells = cells[:-1]
    return cells if len(cells) >= 2 else None


_TABLE_SEPARATOR_RE = re.compile(r"^\|?[\s:|-]+\|?$")


def _table_header_cells(
    text: str, segments: list[tuple[int, int]], index: int
) -> list[tuple[str, int, int]] | None:
    """Return the header cells of the table that ``index`` belongs to.

    A Markdown table splits into one segment per row, so the metric term lives
    in the header while the value lives several rows below it. Walking back to
    the header (skipping the ``|---|---|`` separator) is the only way a table
    row can be attributed to the right metric. The walk stops at the first
    segment that is not a table row, so prose outside the table is unaffected.
    """

    row = _pipe_cells(text[segments[index][0] : segments[index][1]])
    if row is None:
        return None
    saw_separator = False
    for neighbour in range(index - 1, -1, -1):
        low, high = segments[neighbour]
        fragment = text[low:high]
        cells = _pipe_cells(fragment)
        if cells is None:
            return None
        if _TABLE_SEPARATOR_RE.match(fragment.strip()):
            saw_separator = True
            continue
        if saw_separator:
            # The row immediately above |---| is the header, even when it
            # contains digits such as "Q3 销售额".
            return cells
        if not _number_tokens(fragment):
            return cells
    return None


def _derived_table_inherits_metric(
    text: str,
    segments: list[tuple[int, int]],
    index: int,
    terms: list[str],
) -> bool:
    """True when a derived table column belongs to the narrative metric.

    A contribution table often names "降幅贡献" rather than repeating
    "销售额". The metric lives in the introducing narrative, with a heading
    such as "各区域降幅贡献" in between. Table rows, blank lines and
    label-only headings are transparent; the first ordinary sentence decides
    the table's metric context.
    """

    for position in range(index - 1, -1, -1):
        low, high = segments[position]
        fragment = text[low:high]
        if not fragment.strip():
            continue
        if _pipe_cells(fragment) is not None:
            continue
        if any(term in fragment for term in terms):
            return True
        if _is_label(fragment):
            continue
        return False
    return False


def _terms_apply(
    text: str,
    segments: list[tuple[int, int]],
    index: int,
    terms: list[str],
    token_start: int | None = None,
) -> bool:
    """True when a term names the number in its own sentence.

    A figure may inherit the metric from an earlier clause/sentence when it is
    a value continuation ("库存天数……；首条值 26.713990") or when the earlier
    clause is a pure label ("销售额："). It may not jump over a clause that
    carries another figure with its own label, so a second metric's value
    sitting next to the term cannot vouch for the target. Headings keep the
    older rule: a neighbouring sentence with no figure, or ending in a colon,
    still introduces the list or table below it.

    Markdown table rows are handled separately: the header row supplies the
    metric term for every data row in the block. When the header names more
    than one of the target terms the number must sit in a matching column, so
    a two-metric table still cannot pass with the figures swapped.
    """

    if not terms:
        return True
    start, end = segments[index]
    header = _table_header_cells(text, segments, index)
    if header:
        row = _pipe_cells(text[start:end]) or []
        relative = token_start - start if token_start is not None else None
        column = next(
            (
                position
                for position, (_, low, high) in enumerate(row)
                if relative is not None and low <= relative < high
            ),
            None,
        )
        term_columns = [
            column
            for column, (cell, _, _) in enumerate(header)
            if any(term in cell for term in terms)
        ]
        if term_columns:
            # The figure has to sit under the metric's own column; otherwise a
            # second metric in the same table could vouch for it. A derived
            # column ("贡献变化", "变化额") is part of the same metric context.
            return (
                column is None
                or column in term_columns
                or (column is not None and _is_derived_column(header[column][0]))
            )
        # No header names the metric: a derived column inherits it from the
        # narrative that introduced the table, not from a neighbouring table.
        if (
            column is not None
            and _is_derived_column(header[column][0])
            and _derived_table_inherits_metric(text, segments, index, terms)
        ):
            return True

    spans = _clause_spans(text, start, end)
    # Clause spans are absolute offsets; a relative pointer would fall before
    # the first span for every sentence that is not at the top of the answer.
    pointer = token_start if token_start is not None else start
    token_clause = len(spans) - 1
    for position, (low, high) in enumerate(spans):
        if low <= pointer < high or low == pointer:
            token_clause = position
            break
    if any(term in _clause_body(text, spans[token_clause]) for term in terms):
        return True
    if _clause_names_metric(_clause_body(text, spans[token_clause])):
        # The figure is labelled with a different metric: an earlier heading
        # must not adopt it. A change/rate label is the one exception: under a
        # heading that names the metric ("营收同比"), the rate belongs to it.
        if not (
            _is_rate_label(_clause_body(text, spans[token_clause]))
            and (
                _nearby_heading_names(text, segments, index, terms)
                or _prior_segment_names_metric(text, segments, index, terms)
            )
        ):
            return False
    if (
        token_clause > 0
        and _is_rate_label(_clause_body(text, spans[token_clause - 1]))
        and _prior_segment_names_metric(text, segments, index, terms)
    ):
        return True
    if (
        token_clause > 0
        and _is_bare_value(_clause_body(text, spans[token_clause]))
        and _is_rate_label(_clause_body(text, spans[token_clause - 1]))
        and _nearby_heading_names(text, segments, index, terms)
    ):
        return True

    inherits = _is_value_continuation(_clause_body(text, spans[token_clause]))
    member_chain = _has_list_member(_clause_body(text, spans[token_clause]))
    for position in range(token_clause - 1, -1, -1):
        fragment = _clause_body(text, spans[position])
        if any(term in fragment for term in terms):
            label = (
                _is_label(fragment)
                or text[spans[position][1] : spans[position][1] + 1] in {"：", ":"}
                or _has_list_member(fragment)
            )
            return inherits or label
        if _number_tokens(fragment) and not (
            _is_value_continuation(fragment) or (member_chain and _has_list_member(fragment))
        ):
            break
    if token_clause + 1 < len(spans):
        following = _clause_body(text, spans[token_clause + 1])
        if any(term in following for term in terms) and not _number_tokens(following):
            return True

    for neighbour in range(index - 1, -1, -1):
        low, high = segments[neighbour]
        fragment = text[low:high]
        if any(term in fragment for term in terms):
            # A heading may introduce a table or list, so it carries no value
            # of its own; anything else with figures belongs to another metric.
            if (
                not _number_tokens(fragment)
                or _is_label(fragment)
                or _is_heading(fragment)
                or fragment.rstrip().endswith(("：", ":"))
            ):
                return True
            break
        if not _is_list_line(fragment):
            break
    if index + 1 < len(segments):
        low, high = segments[index + 1]
        fragment = text[low:high]
        if any(term in fragment for term in terms) and (
            not _number_tokens(fragment)
            or _is_label(fragment)
            or _is_heading(fragment)
            or fragment.rstrip().endswith(("：", ":"))
        ):
            return True
    if _section_heading_names(text, segments, index, terms):
        return True
    return False


def _section_heading_names(
    text: str,
    segments: list[tuple[int, int]],
    index: int,
    terms: list[str],
) -> bool:
    """True when the section heading above a table/bullet names the metric.

    Monthly tables often label columns by period rather than by metric. The
    metric then comes from the heading that introduces the table, so walking
    back over the header/separator/bullet lines is not enough; keep going until
    a heading or metric-labelled label is reached.
    """

    seen = 0
    for position in range(index - 1, -1, -1):
        low, high = segments[position]
        fragment = text[low:high].strip()
        if not fragment:
            continue
        if any(term in fragment for term in terms):
            return True
        if fragment.startswith("|"):
            continue
        label_like = (
            _is_heading(fragment)
            or _is_label(fragment)
            or fragment.rstrip().endswith(("：", ":"))
        )
        bullet_like = bool(re.match(r"^(?:[-*•]|\d+[.)、])\s*", fragment))
        if label_like or bullet_like:
            if any(hint in fragment for hint in _METRIC_LABEL_HINTS):
                return False
            seen += 1
            if seen >= 12:
                break
            continue
        seen += 1
        if seen >= 12:
            break
    return False


def _number_matches(
    answer: str,
    expected: Any,
    *,
    tolerance: float = 0.005,
    terms: list[str] | None = None,
) -> bool:
    """Check that the expected value appears near the metric it belongs to.

    Matching any number anywhere in the answer would let a coincidentally
    repeated figure pass, so a term list restricts the match to the sentence
    (or its neighbours) that names the metric.
    """

    try:
        target = Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False
    targets = [target]
    if abs(target) <= Decimal("1.5"):
        targets.append(target * 100)
    tolerance_value = Decimal(str(tolerance))
    segments = _segments(answer)
    starts = [start for start, _ in segments]
    for start, _, candidate, _ in _number_tokens(answer):
        index = bisect_right(starts, start) - 1
        if not _terms_apply(
            answer, segments, max(index, 0), terms or [], token_start=start
        ):
            continue
        for item in targets:
            if item == 0:
                if abs(candidate) <= Decimal("0.000001"):
                    return True
                continue
            if abs(candidate - item) / abs(item) <= tolerance_value:
                return True
    return False


def _value_is_sourced(
    value: Decimal, sources: list[Decimal], *, tolerance: float = 0.005
) -> bool:
    tolerance_value = Decimal(str(tolerance))
    for source in sources:
        for candidate in (
            source,
            source * 100,
            source / 100,
            abs(source) * 100,
        ):
            if candidate == 0:
                if abs(value) <= Decimal("0.000001"):
                    return True
                continue
            if abs(value - candidate) / abs(candidate) <= tolerance_value:
                return True
    return False


_ARCHIVE_CONTENT_LIMIT = 16_384
_ARCHIVE_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|secret|access[_-]?token|refresh[_-]?token)\b"
    r"(\s*[:=]\s*)(\"?)([^\s\",}]{1,200})"
)


def _redact_archive_text(text: str) -> str:
    """Scrub key-like assignments from an archived message.

    Tool messages contain business rows, not credentials, but the archive is a
    durable file and must not become a place where a future tool leaks one.
    """

    return _ARCHIVE_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}***",
        text,
    )


def _archive_tool_messages(
    run_id: str, case_id: str, messages: list[Any]
) -> list[dict[str, Any]]:
    archived: list[dict[str, Any]] = []
    for message in messages or []:
        content = getattr(message, "content", "") or ""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        redacted = _redact_archive_text(content)
        encoded = redacted.encode("utf-8")
        truncated = len(encoded) > _ARCHIVE_CONTENT_LIMIT
        if truncated:
            redacted = encoded[:_ARCHIVE_CONTENT_LIMIT].decode("utf-8", "ignore")
            encoded = redacted.encode("utf-8")
        archived.append(
            {
                "run_id": run_id,
                "case_id": case_id,
                "tool_call_id": message.tool_call_id,
                "tool": message.tool,
                "requested_tool": message.requested_tool,
                "status": message.status,
                "reused": message.reused,
                "trusted": message.trusted,
                "evidence_ids": list(message.evidence_ids),
                "arguments": dict(message.arguments),
                "content": redacted,
                "content_sha256": digest,
                "content_bytes": int(message.content_bytes),
                "content_truncated": truncated,
                "visible_data": message.visible_data,
                "row_count": message.row_count,
                "displayed_count": message.displayed_count,
                "truncated": message.truncated,
                "created_at": message.created_at.isoformat(),
            }
        )
    return archived


def _source_numbers(
    recorded: dict[str, tuple[str, dict[str, Any], Any]],
    messages: list[Any] | None = None,
) -> list[Decimal]:
    """Numbers the model actually saw, used for provenance checks.

    When the runtime captured the tool messages, only rows present in a
    trusted, model-visible message count. Rows that were never displayed (a
    truncated 21st row, for example) are deliberately excluded: the model
    could not have read them. The recorded ``ToolResult`` fallback exists for
    callers that pass ``None`` because they build records without a runtime; an
    empty captured list is never a reason to fall back to unshown rows.
    """

    values: list[Decimal] = []
    if messages is not None:
        for message in messages:
            if not getattr(message, "trusted", False):
                continue
            payload = getattr(message, "visible_data", None) or {}
            values.extend(
                _numbers_in(json.dumps(payload, ensure_ascii=False, default=str))
            )
        return values
    for _, _, result in recorded.values():
        payload = {"summary": result.summary, "data": result.data}
        values.extend(
            _numbers_in(json.dumps(payload, ensure_ascii=False, default=str))
        )
    return values


def _is_calendar_year(text: str, start: int, end: int, value: Decimal) -> bool:
    """True for 1900-2100 integers written as a year or date component."""

    if value != value.to_integral_value():
        return False
    if not (Decimal(1900) <= value <= Decimal(2100)):
        return False
    after = text[end : end + 1]
    before = text[start - 1 : start]
    if after in {"年", "-", "/", "."}:
        return True
    return before in {"-", "/"} and bool(re.match(r"\d{1,2}(\D|$)", text[end : end + 3]))


def _unsourced_numbers(
    answer: str, sources: list[Decimal]
) -> list[str]:
    """Business-scale numbers in the answer that no tool result produced.

    A stated difference, sum or ratio of returned figures is verifiable from
    the evidence, so it counts as sourced even though no single row contains
    it. The derivation sources are capped to keep the pairwise work bounded.
    """

    unique_sources = list(dict.fromkeys(sources))[:200]
    derived = _derived_candidates(unique_sources) if unique_sources else []
    unsourced: list[str] = []
    for start, end, value, is_percent in _number_tokens(answer):
        if not is_percent and abs(value) < _BUSINESS_NUMBER_FLOOR:
            continue
        if _is_calendar_year(answer, start, end, value):
            continue
        if _value_is_sourced(value, sources) or _matches_derived(value, derived):
            continue
        raw = answer[start:end].strip()
        if raw and raw not in unsourced:
            unsourced.append(raw)
    return unsourced


def _cited_evidence_ids(answer: str) -> list[str]:
    found = [match.group(0) for match in _CITATION_RE.finditer(answer or "")]
    found += [match.group(0) for match in _EVIDENCE_ID_RE.finditer(answer or "")]
    return list(dict.fromkeys(found))


class PlanMatch(BaseModel):
    """How an actual tool plan compares with one acceptable plan."""

    selection: bool
    arguments: bool
    order: bool
    extra_calls: int = 0
    repeated_calls: int = 0


def _call_signature(item: PlannedCall) -> str:
    return json.dumps(
        {"tool": item.tool, "args": item.arguments},
        sort_keys=True,
        default=str,
    )


def _repeated_calls(actual: list[PlannedCall]) -> int:
    """Count identical repeat calls beyond the first, per tool+arguments."""

    counts = Counter(_call_signature(item) for item in actual)
    return sum(count - 1 for count in counts.values() if count > 1)


def _match_alternative(
    plan: list[PlannedCall], actual: list[PlannedCall]
) -> PlanMatch:
    required_names = Counter(item.tool for item in plan)
    actual_names = Counter(item.tool for item in actual)
    selection = all(actual_names[name] >= count for name, count in required_names.items())

    used: set[int] = set()
    matched: list[int] = []
    for required in plan:
        index = next(
            (
                i
                for i, candidate in enumerate(actual)
                if i not in used
                and candidate.tool == required.tool
                and _args_match(candidate.arguments, required.arguments)
            ),
            None,
        )
        if index is None:
            continue
        used.add(index)
        matched.append(index)

    arguments = len(matched) == len(plan)
    # A matched call can only be used once, so an identical repeat shows up as
    # extra_calls instead of satisfying a second requirement.
    order = arguments and matched == sorted(matched)
    return PlanMatch(
        selection=selection,
        arguments=arguments,
        order=order,
        extra_calls=len(actual) - len(used),
        repeated_calls=_repeated_calls(actual),
    )


def _best_plan(case: LiveEvalCase, actual: list[PlannedCall]) -> PlanMatch:
    alternatives = case.plan_alternatives or (
        [case.required_calls] if case.required_calls else []
    )
    if not alternatives:
        return PlanMatch(
            selection=True,
            arguments=True,
            order=True,
            extra_calls=0,
            repeated_calls=_repeated_calls(actual),
        )
    best: PlanMatch | None = None
    for plan in alternatives:
        candidate = _match_alternative(plan, actual)
        if best is None or (
            candidate.selection,
            candidate.arguments,
            candidate.order,
        ) > (best.selection, best.arguments, best.order):
            best = candidate
    return best or PlanMatch(
        selection=False, arguments=False, order=False, repeated_calls=0
    )


def _truncated_warning(warnings: list[str]) -> bool:
    return any("截断" in item or "不完整" in item for item in warnings)


def _numeric(value: Any) -> Decimal | None:
    """Return a finite Decimal, or None when the value is not a number.

    A canonical ``direction`` result ("volatile", "growth") is not a number, so
    numeric accuracy must be recorded as not applicable instead of false.
    """

    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


_FAILURE_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("tool_selection", "tool_selection_correct", False),
    ("tool_arguments", "tool_argument_correct", False),
    ("plan_order", "plan_order_correct", False),
    ("query", "query_correct", False),
    ("task_completion", "task_completed", False),
    ("evidence", "evidence_sufficient", False),
    ("conclusion_overreach", "conclusion_overreach", True),
    ("number_accuracy", "number_accuracy", False),
    ("evidence_chain", "evidence_chain_valid", False),
    ("refusal", "refusal_correct", False),
    ("narrative", "narrative_complete", False),
    ("citations", "answer_citations_valid", False),
)


def _failure_reasons(values: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for label, field, bad_when_true in _FAILURE_FIELDS:
        value = values.get(field)
        if value is None:
            continue
        if bad_when_true:
            if value:
                reasons.append(label)
        elif value is False:
            reasons.append(label)
    return reasons


def run_live_agent_case(
    case: LiveEvalCase,
    *,
    runtime: AgentRuntime,
    registry: ToolRegistry,
    principal: Principal,
    max_seconds: float | None = None,
) -> LiveEvalResult:
    """Run one case against the configured LLM and record each eval dimension.

    No fake client is substituted. If the runtime is not backed by a real
    client, the result is a structured failure and must not be reported as a
    live acceptance result. A scripted planner is only used by offline tests
    that explicitly install one.
    """

    effective_principal = principal
    if case.role and case.role != principal.role:
        effective_principal = Principal(
            f"key:eval-{case.role}",
            case.role,
            ROLE_SCOPES.get(case.role, frozenset()),
        )

    run_id = f"eval-{case.id}-{uuid4().hex[:8]}"
    recorder = _RecordingRegistry(registry)
    counter = _CountingLLM(runtime.llm) if runtime.llm is not None else None
    original_llm = runtime.llm
    original_registry = runtime.registry
    if counter is not None:
        runtime.llm = counter
    runtime.registry = recorder
    started = time.monotonic()
    try:
        result = runtime.run(
            case.question,
            effective_principal,
            run_id=run_id,
            max_seconds=max_seconds,
        )
    finally:
        runtime.llm = original_llm
        runtime.registry = original_registry
    duration_ms = round((time.monotonic() - started) * 1000, 3)
    actual = [
        PlannedCall(tool=item.tool, arguments=item.arguments)
        for item in result.tool_calls
    ]
    # Per-decision trace: the loop's own steps, the LLM calls behind them and
    # the tool calls with their evidence ids. This is what makes a real run
    # diagnosable without replaying the tools offline afterwards.
    decisions = [
        {
            "step": step.index,
            "action": step.action,
            "tool": step.tool,
            "reason": step.reason,
            "tool_call_id": step.tool_call_id,
            "blocked_reason": step.blocked_reason,
            "remaining_seconds": step.remaining_seconds,
            "remaining_tool_calls": step.remaining_tool_calls,
            "retry_count": step.retry_count,
            "created_at": step.created_at,
        }
        for step in result.steps
    ]
    tool_trace = [
        {
            "call_id": item.call_id,
            "tool": item.tool,
            "arguments": item.arguments,
            "status": item.status,
            "duration_ms": item.duration_ms,
            "evidence_ids": item.evidence_ids,
            "error_code": item.error_code,
            "summary": item.summary,
            "reused": item.reused,
            "row_count": item.row_count,
            "displayed_count": item.displayed_count,
            "truncated": item.truncated,
            "blocked": False,
        }
        for item in result.tool_calls
    ]
    tool_trace.extend(
        {
            "call_id": item.call_id,
            "tool": item.tool,
            "arguments": item.arguments,
            "status": item.status,
            "duration_ms": item.duration_ms,
            "evidence_ids": item.evidence_ids,
            "error_code": item.error_code,
            "summary": item.summary,
            "reused": item.reused,
            "row_count": item.row_count,
            "displayed_count": item.displayed_count,
            "truncated": item.truncated,
            "blocked": True,
            "blocked_reason": item.summary,
        }
        for item in result.blocked_tool_calls
    )

    plan = _best_plan(case, actual)
    selection_correct = plan.selection
    tool_argument_correct = plan.arguments
    plan_order_correct = plan.order if case.require_order else None
    forbidden_hit = any(item.tool in set(case.forbidden_tools) for item in actual)
    if forbidden_hit:
        selection_correct = False

    check = case.query_check
    query_correct: bool | None = None
    observed: Any = None
    canonical_summary: str | None = None
    canonical_error: str | None = None
    if check is not None:
        canonical_ok, observed, canonical_summary, canonical_error = _canonical_check(
            check,
            registry,
            effective_principal,
            expected_status=case.expect_canonical_status,
        )
        actual_match = any(
            item.tool == check.tool and _args_match(item.arguments, check.arguments)
            for item in actual
        )
        query_correct = canonical_ok and actual_match

    evidence_count = len(result.evidence)
    traceable = sum(1 for item in result.evidence if item.traceable)
    if case.expect_evidence:
        evidence_sufficient = evidence_count > 0 and traceable == evidence_count
    else:
        evidence_sufficient = True

    unverified_warning = any(
        "无法在工具证据中直接核对" in item for item in result.warnings
    )
    unsupported_relation = any(
        "业务关系不一致" in item for item in result.warnings
    )
    unsupported_claim = (
        case.expect_evidence and evidence_count == 0 and bool(result.final_answer)
    )
    conclusion_overreach = (
        unverified_warning or unsupported_relation or unsupported_claim
    )
    answer = result.final_answer or ""
    missing_terms = [term for term in case.conclusion_terms if term not in answer]
    task_completed = result.status.value in case.allowed_statuses

    report_metrics: dict[str, Any] = {}
    report_error: str | None = None
    archived_messages = _archive_tool_messages(
        run_id, case.id, result.tool_messages
    )
    try:
        report_metrics = _report_dimensions(
            recorded=recorder.results,
            run_id=run_id,
            case_id=case.id,
            question=case.question,
            principal=effective_principal,
            provider=result.llm_provider,
            model=getattr(runtime.llm, "model", None) if runtime.llm else None,
            prompt_version=result.prompt_version,
            stop_reason=result.stop_reason,
            archived_messages=archived_messages,
        )
    except Exception as exc:  # pragma: no cover - report build must not hide the run
        report_error = f"{type(exc).__name__}: {exc}"

    sources = _source_numbers(recorder.results, result.tool_messages)
    unsourced = _unsourced_numbers(answer, sources) if answer else []
    answer_number_provenance: bool | None = None
    if answer and sources:
        answer_number_provenance = not unsourced

    known_evidence_ids = {item.evidence_id for item in result.evidence}
    cited_ids = _cited_evidence_ids(answer)
    answer_citations_valid: bool | None = None
    if cited_ids:
        answer_citations_valid = all(item in known_evidence_ids for item in cited_ids)

    number_accuracy: bool | None = None
    if (
        case.expect_number
        and _numeric(observed) is not None
        and not case.expect_refusal
    ):
        number_accuracy = _number_matches(
            answer, observed, terms=case.conclusion_terms
        )

    refusal_terms = case.refusal_terms or DEFAULT_REFUSAL_TERMS
    refused = any(term in answer for term in refusal_terms)
    # An authorization failure stops the run before the model gets another
    # turn, so there may be no prose answer at all. That stop *is* the
    # refusal: record it from the structured error rather than requiring text.
    policy_blocked = any(
        item.get("code") == "permission_denied" for item in result.errors
    )
    refusal_correct: bool | None = None
    safety_intercepted: bool | None = None
    if case.expect_refusal:
        refusal_correct = refused or policy_blocked
        safety_intercepted = (refused or policy_blocked) and not forbidden_hit

    narrative_complete = (
        len(answer.strip()) >= case.min_answer_chars
        and not _truncated_warning(result.warnings)
    )

    passed = (
        selection_correct
        and tool_argument_correct
        and plan_order_correct is not False
        and query_correct is not False
        and evidence_sufficient
        and not conclusion_overreach
        and task_completed
        and answer_citations_valid is not False
        and report_metrics.get("evidence_chain_valid") is not False
    )
    if case.expect_refusal:
        passed = passed and bool(refusal_correct)
    else:
        # A truncated or empty answer is not a completed analysis. Refusals are
        # exempt because an authorization stop may legitimately carry no prose.
        passed = passed and narrative_complete
    if number_accuracy is False:
        passed = False

    failure_reasons: list[str] = []
    if not passed:
        failure_reasons = _failure_reasons(
            {
                "tool_selection_correct": selection_correct,
                "tool_argument_correct": tool_argument_correct,
                "plan_order_correct": plan_order_correct,
                "query_correct": query_correct,
                "task_completed": task_completed,
                "evidence_sufficient": evidence_sufficient,
                "conclusion_overreach": conclusion_overreach,
                "number_accuracy": number_accuracy,
                "evidence_chain_valid": report_metrics.get("evidence_chain_valid"),
                "refusal_correct": refusal_correct,
                "narrative_complete": (
                    None if case.expect_refusal else narrative_complete
                ),
                "answer_citations_valid": answer_citations_valid,
            }
        )

    return LiveEvalResult(
        id=case.id,
        category=case.category,
        question=case.question,
        domain=case.domain or case.category,
        scored=case.scored,
        planning_correct=selection_correct,
        tool_selection_correct=selection_correct,
        tool_argument_correct=tool_argument_correct,
        plan_order_correct=plan_order_correct,
        extra_tool_calls=plan.extra_calls,
        repeated_tool_calls=plan.repeated_calls,
        duplicate_tool_calls=len(result.blocked_tool_calls),
        query_correct=query_correct,
        evidence_sufficient=evidence_sufficient,
        conclusion_overreach=conclusion_overreach,
        task_completed=task_completed,
        passed=passed,
        status=result.status.value,
        llm_provider=result.llm_provider,
        llm_model=getattr(runtime.llm, "model", None) if runtime.llm else None,
        prompt_version=result.prompt_version,
        final_answer=result.final_answer,
        actual_plan=actual,
        matching_summaries=[
            item.summary for item in result.tool_calls if item.summary is not None
        ],
        canonical_summary=canonical_summary,
        observed=observed,
        warnings=result.warnings,
        errors=result.errors,
        failure_reasons=failure_reasons,
        tool_calls=len(result.tool_calls),
        attempted_tool_calls=len(result.tool_calls) + len(result.blocked_tool_calls),
        successful_data_tool_calls=sum(
            1
            for item in result.tool_calls
            if item.tool in _DATA_QUERY_TOOLS and item.status == "ok"
        ),
        decisions=decisions,
        llm_calls=list(counter.events) if counter is not None else [],
        tool_call_trace=tool_trace,
        tool_messages=archived_messages,
        relation_issues=list(result.relation_issues),
        relation_validation_passed=not result.relation_issues,
        budget_stop_reason=result.state.budget_stop_reason,
        failure_category=result.state.failure_category,
        evidence_count=evidence_count,
        traceable_evidence=traceable,
        llm_latency_ms=result.llm_latency_ms,
        llm_tokens=result.llm_tokens,
        duration_ms=duration_ms,
        model_calls=counter.calls if counter is not None else 0,
        evidence_citation_completeness=report_metrics.get(
            "evidence_citation_completeness"
        ),
        finding_citation_correct=report_metrics.get("finding_citation_correct"),
        numeric_validation_passed=report_metrics.get("numeric_validation_passed"),
        numeric_issue_count=int(report_metrics.get("numeric_issue_count") or 0),
        report_structure_completeness=report_metrics.get(
            "report_structure_completeness"
        ),
        report_narrative_mode=report_metrics.get("report_narrative_mode"),
        evidence_chain_valid=report_metrics.get("evidence_chain_valid"),
        evidence_chain_issue_count=int(
            report_metrics.get("evidence_chain_issue_count") or 0
        ),
        evidence_chain_issues=list(report_metrics.get("evidence_chain_issues") or []),
        number_accuracy=number_accuracy,
        answer_number_provenance=answer_number_provenance,
        unsourced_numbers=unsourced,
        cited_evidence_ids=cited_ids,
        answer_citations_valid=answer_citations_valid,
        safety_intercepted=safety_intercepted,
        refusal_correct=refusal_correct,
        narrative_complete=narrative_complete,
        detail={
            "human_check": case.human_check,
            "role": effective_principal.role,
            "require_order": case.require_order,
            "required_calls": [item.model_dump() for item in case.required_calls],
            "plan_alternatives": [
                [item.model_dump() for item in plan]
                for plan in case.plan_alternatives
            ],
            "matched_calls": [item.model_dump() for item in actual],
            "canonical_error": canonical_error,
            "expected_canonical_status": case.expect_canonical_status,
            "policy_blocked": policy_blocked,
            "report_structure_blocks": report_metrics.get("report_structure_blocks"),
            "unverified_number_warning": unverified_warning,
            "unsupported_relation": unsupported_relation,
            "relation_issues": list(result.relation_issues),
            "relation_validation_passed": not result.relation_issues,
            "evidence_chain_valid": report_metrics.get("evidence_chain_valid"),
            "evidence_chain_issues": list(
                report_metrics.get("evidence_chain_issues") or []
            ),
            "tool_messages_archived": len(archived_messages),
            "tool_messages_content_truncated": sum(
                1 for item in archived_messages if item["content_truncated"]
            ),
            "unsupported_claim": unsupported_claim,
            "missing_conclusion_terms": missing_terms,
            "allowed_statuses": case.allowed_statuses,
            "expect_evidence": case.expect_evidence,
            "forbidden_tool_called": forbidden_hit,
            "extra_tool_calls": plan.extra_calls,
            "repeated_tool_calls": plan.repeated_calls,
            "unsourced_numbers": unsourced,
            "cited_evidence_ids": cited_ids,
            "known_evidence_ids": sorted(known_evidence_ids),
            "report_error": report_error,
            "truncated_warning": _truncated_warning(result.warnings),
        },
    )


def _rate(part: int, total: int) -> float | None:
    return round(part / total, 4) if total else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + 0.999999)))
    return round(ordered[index], 3)


_BOOL_METRICS = (
    ("tool_selection_accuracy", "tool_selection_correct"),
    ("tool_argument_accuracy", "tool_argument_correct"),
    ("plan_order_accuracy", "plan_order_correct"),
    ("query_accuracy", "query_correct"),
    ("task_completion_rate", "task_completed"),
    ("finding_citation_correctness", "finding_citation_correct"),
    ("numeric_validator_pass_rate", "numeric_validation_passed"),
    ("number_accuracy", "number_accuracy"),
    ("answer_number_provenance_rate", "answer_number_provenance"),
    ("answer_citation_validity", "answer_citations_valid"),
    ("evidence_chain_validity", "evidence_chain_valid"),
    ("safety_interception_rate", "safety_intercepted"),
    ("error_refusal_rate", "refusal_correct"),
    ("narrative_completeness", "narrative_complete"),
)
_RATE_METRICS = (
    ("evidence_citation_completeness", "evidence_citation_completeness"),
    ("report_structure_completeness", "report_structure_completeness"),
)


def _metric_block(records: list[dict[str, Any]]) -> dict[str, Any]:
    block: dict[str, Any] = {"cases": len(records)}
    passed = sum(1 for item in records if item.get("passed"))
    block["passed"] = passed
    block["failed"] = len(records) - passed
    block["pass_rate"] = _rate(passed, len(records))
    for label, field in _BOOL_METRICS:
        values = [item.get(field) for item in records if item.get(field) is not None]
        block[label] = _rate(sum(1 for value in values if value), len(values))
        block[f"{label}_evaluated"] = len(values)
    for label, field in _RATE_METRICS:
        values = [
            float(item[field]) for item in records if item.get(field) is not None
        ]
        block[label] = round(sum(values) / len(values), 4) if values else None
        block[f"{label}_evaluated"] = len(values)
    durations = [float(item.get("duration_ms") or 0) for item in records]
    block.update(
        {
            "p50_latency_ms": _percentile(durations, 0.5),
            "p95_latency_ms": _percentile(durations, 0.95),
            "avg_llm_latency_ms": round(
                sum(float(item.get("llm_latency_ms") or 0) for item in records)
                / len(records),
                3,
            )
            if records
            else None,
            "model_calls": sum(int(item.get("model_calls") or 0) for item in records),
            "extra_tool_calls": sum(
                int(item.get("extra_tool_calls") or 0) for item in records
            ),
            "repeated_tool_calls": sum(
                int(item.get("repeated_tool_calls") or 0) for item in records
            ),
            # ``repeated_tool_calls`` is the plan-level repeat count. Runtime
            # dedupe blocks and actual executions are separate numbers and must
            # never be summed into the same field.
            "planned_repeated_tool_calls": sum(
                int(item.get("repeated_tool_calls") or 0) for item in records
            ),
            "attempted_tool_calls": sum(
                int(
                    item.get("attempted_tool_calls")
                    or item.get("tool_calls")
                    or 0
                )
                + (
                    int(item.get("duplicate_tool_calls") or 0)
                    if item.get("attempted_tool_calls") is None
                    else 0
                )
                for item in records
            ),
            "executed_tool_calls": sum(
                int(item.get("tool_calls") or 0) for item in records
            ),
            "semantic_dedupe_blocks": sum(
                int(item.get("duplicate_tool_calls") or 0) for item in records
            ),
            "successful_data_tool_calls": sum(
                int(item.get("successful_data_tool_calls") or 0) for item in records
            ),
            "tool_messages_archived": sum(
                len(item.get("tool_messages") or []) for item in records
            ),
            "tool_messages_content_truncated": sum(
                1
                for item in records
                for message in item.get("tool_messages") or []
                if message.get("content_truncated")
            ),
            "relation_validation_failures": sum(
                1 for item in records if item.get("relation_validation_passed") is False
            ),
        }
    )
    token_records = [
        item
        for item in records
        if isinstance(item.get("llm_tokens"), dict) and item.get("llm_tokens")
    ]
    block["input_tokens"] = sum(
        int((item.get("llm_tokens") or {}).get("input") or 0)
        for item in token_records
    )
    block["output_tokens"] = sum(
        int((item.get("llm_tokens") or {}).get("output") or 0)
        for item in token_records
    )
    block["token_records"] = len(token_records)
    block["token_records_missing"] = len(records) - len(token_records)
    # Tokens are only aggregated from records that actually carry independent
    # token counters; missing records are reported, never inferred.
    block["tokens_complete"] = not block["token_records_missing"]
    block["total_tokens"] = block["input_tokens"] + block["output_tokens"]
    return block


def summarize_live_results(
    records: list[dict[str, Any]],
    *,
    previous: dict[str, Any] | None = None,
    model: str | None = None,
    provider: str | None = None,
    real_llm: bool = True,
) -> dict[str, Any]:
    """Aggregate per-case records into overall/per-domain/per-category stats."""

    scored = [item for item in records if item.get("scored", True)]
    failed = [item for item in scored if not item.get("passed")]
    failed_cases = []
    for item in failed:
        # Records produced by run_live_agent_case carry the reasons computed
        # against the case definition (including plan order, which only applies
        # to cases that require it). Hand-built records fall back to the field
        # scan so older archives stay readable.
        reasons = list(item.get("failure_reasons") or [])
        if not reasons:
            reasons = _failure_reasons(item)
        failed_cases.append(
            {
                "id": item.get("id"),
                "domain": item.get("domain") or item.get("category"),
                "status": item.get("status"),
                "reasons": reasons,
                "expected": item.get("canonical_summary"),
                "observed": item.get("observed"),
                "answer_excerpt": (item.get("final_answer") or "")[:200],
            }
        )

    domains: dict[str, list[dict[str, Any]]] = {}
    categories: dict[str, list[dict[str, Any]]] = {}
    for item in records:
        domains.setdefault(item.get("domain") or "unspecified", []).append(item)
        categories.setdefault(item.get("category") or "unspecified", []).append(item)

    overall = _metric_block(scored)
    overall.update(
        {
            "selected_cases": len(records),
            "scored_cases": len(scored),
            "passed": len(scored) - len(failed),
            "failed": len(failed),
            "pass_rate": _rate(len(scored) - len(failed), len(scored)),
        }
    )

    history = None
    if previous:
        previous_rate = (previous.get("overall") or {}).get("pass_rate")
        history = {
            "previous_generated_at": previous.get("generated_at"),
            "previous_pass_rate": previous_rate,
            "pass_rate_delta": (
                round(overall["pass_rate"] - previous_rate, 4)
                if overall["pass_rate"] is not None and previous_rate is not None
                else None
            ),
        }

    return {
        "schema_version": "phase7-eval-v1",
        "real_llm": real_llm,
        "fake_client_used": not real_llm,
        "model": model,
        "provider": provider,
        "overall": overall,
        "by_domain": {
            name: _metric_block([item for item in items if item.get("scored", True)])
            for name, items in sorted(domains.items())
        },
        "by_category": {
            name: _metric_block([item for item in items if item.get("scored", True)])
            for name, items in sorted(categories.items())
        },
        "failed_cases": failed_cases,
        "history": history,
    }


__all__ = [
    "AgentEvalCase",
    "EvalCaseResult",
    "LiveEvalCase",
    "LiveEvalResult",
    "PlannedCall",
    "QueryCheck",
    "DEFAULT_REFUSAL_TERMS",
    "ScriptedPlanLLM",
    "load_agent_cases",
    "load_live_agent_cases",
    "run_agent_case",
    "run_live_agent_case",
    "summarize_live_results",
]
