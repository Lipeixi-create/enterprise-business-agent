"""Operational metrics computed from the durable runtime tables.

The numbers are read from the run store, not kept in an in-process counter, so
they survive a restart and cover every worker that shares the database. The
only process-local value is the PostgreSQL connection-error count; the payload
labels it explicitly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from math import ceil
from typing import Any

from sqlalchemy import func, select

from ..agent.checkpoint import CHECKPOINT_METADATA
from ..evidence.approval import APPROVAL_METADATA
from ..evidence.schema import EVIDENCE_METADATA
from .run_store import _EVENTS, _RUNS, _TOOL_RESULTS, PersistentRunStore

_COMPLETED = ("completed", "completed_with_warnings")
_FAILED = (
    "failed",
    "llm_unavailable",
    "llm_timeout",
    "tool_failure",
    "budget_exhausted",
    "timeout",
    "invalid_action",
    "repeated_tool_call",
)
_TERMINAL = _COMPLETED + _FAILED + ("no_evidence", "no_data", "cancelled")
_CHECKPOINTS = CHECKPOINT_METADATA.tables["agent_checkpoints"]
_REPORTS = EVIDENCE_METADATA.tables["agent_reports"]
_EVIDENCE = EVIDENCE_METADATA.tables["agent_evidence"]
_FINDINGS = EVIDENCE_METADATA.tables["agent_findings"]
_APPROVALS = APPROVAL_METADATA.tables["agent_report_approvals"]


def _rate(part: int, total: int) -> float | None:
    return round(part / total, 4) if total else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _attempt_seconds(payload: dict[str, Any]) -> float | None:
    started = _parse_iso(payload.get("started_at"))
    finished = _parse_iso(payload.get("finished_at"))
    if started is None or finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


def _counts_by(connection, table, column) -> dict[str, int]:
    rows = connection.execute(select(column, func.count()).select_from(table).group_by(column)).all()
    return {str(value or "unknown"): int(count) for value, count in rows}


def _numeric_issues(validation: dict[str, Any]) -> tuple[int, int]:
    issues = validation.get("issues") or []
    numeric = 0
    for issue in issues:
        code = str(issue.get("code") or "")
        if code in {
            "unsourced_number",
            "number_out_of_range",
            "numeric_mismatch",
            "unit_mismatch",
        }:
            numeric += 1
    return numeric, len(validation.get("unverified_numbers") or [])


def metrics_snapshot(store: PersistentRunStore, settings: Any) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with store.engine.connect() as connection:
        status_counts = _counts_by(connection, _RUNS, _RUNS.c.status)
        run_rows = connection.execute(
            select(
                _RUNS.c.run_id,
                _RUNS.c.status,
                _RUNS.c.execution_seconds,
                _RUNS.c.resume_count,
                _RUNS.c.payload,
            )
        ).all()
        tool_rows = connection.execute(
            select(
                _TOOL_RESULTS.c.run_id,
                _TOOL_RESULTS.c.status,
                func.count(),
            ).group_by(_TOOL_RESULTS.c.run_id, _TOOL_RESULTS.c.status)
        ).all()
        event_counts = _counts_by(connection, _EVENTS, _EVENTS.c.event_type)
        model_event_rows = connection.execute(
            select(_EVENTS.c.run_id, _EVENTS.c.detail).where(_EVENTS.c.event_type == "model_response")
        ).all()
        evidence_domain_rows = connection.execute(select(_EVIDENCE.c.run_id, _EVIDENCE.c.domain).distinct()).all()
        checkpoint_count = int(connection.execute(select(func.count()).select_from(_CHECKPOINTS)).scalar_one())
        checkpoint_bytes = int(
            connection.execute(
                select(func.coalesce(func.sum(func.length(_CHECKPOINTS.c.checkpoint_value)), 0))
            ).scalar_one()
        )
        report_rows = connection.execute(select(_REPORTS.c.status, _REPORTS.c.validation_json)).all()
        evidence_runs = int(
            connection.execute(
                select(func.count(func.distinct(_EVIDENCE.c.run_id)))
                .select_from(_EVIDENCE.join(_RUNS, _EVIDENCE.c.run_id == _RUNS.c.run_id))
                .where(_RUNS.c.status.in_(_COMPLETED))
            ).scalar_one()
        )
        finding_count = int(connection.execute(select(func.count()).select_from(_FINDINGS)).scalar_one())
        approval_rows = connection.execute(
            select(
                _APPROVALS.c.state,
                _APPROVALS.c.requested_at,
                _APPROVALS.c.decided_at,
            )
        ).all()

    total_runs = sum(status_counts.values())
    completed = sum(status_counts.get(status, 0) for status in _COMPLETED)
    failed = sum(status_counts.get(status, 0) for status in _FAILED)
    terminal_runs = sum(status_counts.get(status, 0) for status in _TERMINAL)

    durations: list[float] = []
    tokens_in = 0
    tokens_out = 0
    model_latencies: list[float] = []
    tool_durations: list[float] = []
    run_info: dict[str, dict[str, Any]] = {}
    for row in run_rows:
        seconds = float(row.execution_seconds or 0.0)
        payload: dict[str, Any] = {}
        try:
            payload = json.loads(row.payload)
        except (TypeError, ValueError):
            payload = {}
        if seconds <= 0:
            seconds = _attempt_seconds(payload) or 0.0
        if row.status in _TERMINAL and seconds > 0:
            durations.append(seconds)
        tokens = payload.get("llm_tokens") or {}
        tokens_in += int(tokens.get("input") or 0)
        tokens_out += int(tokens.get("output") or 0)
        latency_ms = float(payload.get("llm_latency_ms") or 0.0)
        if latency_ms > 0:
            model_latencies.append(latency_ms)
        for call in payload.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            call_ms = float(call.get("duration_ms") or 0.0)
            if call_ms > 0:
                tool_durations.append(call_ms)
        run_info[str(row.run_id)] = {
            "status": row.status,
            "seconds": seconds,
            "tokens": tokens,
        }

    tool_counts: dict[str, int] = {}
    tool_status: dict[str, int] = {}
    for run_id, status, count in tool_rows:
        tool_counts[str(run_id)] = tool_counts.get(str(run_id), 0) + int(count)
        tool_status[str(status)] = tool_status.get(str(status), 0) + int(count)
    total_tool_calls = sum(tool_status.values())
    tool_failures = total_tool_calls - tool_status.get("ok", 0)

    report_total = len(report_rows)
    report_validated = sum(1 for status, _ in report_rows if status == "validated")
    numeric_interceptions = 0
    unsourced_numbers = 0
    for _, validation_json in report_rows:
        try:
            validation = json.loads(validation_json)
        except (TypeError, ValueError):
            continue
        numeric, unverified = _numeric_issues(validation)
        numeric_interceptions += numeric
        unsourced_numbers += unverified

    approval_counts: dict[str, int] = {}
    pending_waits: list[float] = []
    decided_waits: list[float] = []
    now = datetime.now(timezone.utc)
    for state, requested_at, decided_at in approval_rows:
        approval_counts[str(state)] = approval_counts.get(str(state), 0) + 1
        requested = _parse_iso(requested_at)
        if requested is None:
            continue
        if state == "pending":
            pending_waits.append(max(0.0, (now - requested).total_seconds()))
        elif decided_at:
            decided = _parse_iso(decided_at)
            if decided is not None:
                decided_waits.append(max(0.0, (decided - requested).total_seconds()))

    resumed_runs = [row for row in run_rows if int(row.resume_count or 0) > 0]
    resumed_completed = sum(1 for row in resumed_runs if row.status in _COMPLETED)

    model_calls_by_run: dict[str, int] = {}
    for run_id, _detail in model_event_rows:
        key = str(run_id or "unattributed")
        model_calls_by_run[key] = model_calls_by_run.get(key, 0) + 1
    model_calls_per_run = list(model_calls_by_run.values())
    total_model_calls = int(event_counts.get("model_response", 0))

    domains_by_run: dict[str, set[str]] = {}
    for run_id, domain in evidence_domain_rows:
        domains_by_run.setdefault(str(run_id), set()).add(str(domain or "unspecified"))
    domain_runs: dict[str, list[dict[str, Any]]] = {}
    domain_tokens: dict[str, int] = {}
    for run_id, domains in domains_by_run.items():
        info = run_info.get(run_id)
        if info is None:
            continue
        for domain in domains:
            domain_runs.setdefault(domain, []).append(info)
            tokens = info["tokens"] or {}
            domain_tokens[domain] = (
                domain_tokens.get(domain, 0) + int(tokens.get("input") or 0) + int(tokens.get("output") or 0)
            )
    by_domain = {}
    for domain, items in sorted(domain_runs.items()):
        domain_durations = [item["seconds"] for item in items if item["seconds"] > 0]
        domain_completed = sum(1 for item in items if item["status"] in _COMPLETED)
        by_domain[domain] = {
            "runs": len(items),
            "completed": domain_completed,
            "success_rate": _rate(domain_completed, len(items)),
            "avg_execution_seconds": round(sum(domain_durations) / len(domain_durations), 3)
            if domain_durations
            else None,
            "p95_execution_seconds": _percentile(domain_durations, 0.95),
            "tokens": domain_tokens.get(domain, 0),
        }

    price_in = getattr(settings, "model_input_price_per_million", None)
    price_out = getattr(settings, "model_output_price_per_million", None)
    priced = price_in is not None and price_out is not None
    estimated_cost = (
        round(
            tokens_in / 1_000_000 * float(price_in) + tokens_out / 1_000_000 * float(price_out),
            6,
        )
        if priced
        else None
    )
    completed_for_cost = completed or 0
    cost = {
        "currency": getattr(settings, "model_price_currency", "CNY"),
        "price_source": getattr(settings, "model_price_source", "unconfigured"),
        "input_price_per_million_tokens": price_in,
        "output_price_per_million_tokens": price_out,
        "estimated": priced,
        "estimated_total": estimated_cost,
        "estimated_per_completed_run": (
            round(estimated_cost / completed_for_cost, 6) if estimated_cost is not None and completed_for_cost else None
        ),
        "basis": "run_store_token_count_x_configured_price",
        "note": (
            "价格来自配置，按 run store 记录的 token 数估算，不是供应商账单。"
            if priced
            else "未配置 EBA_MODEL_INPUT/OUTPUT_PRICE_PER_MTOKENS，未估算成本。"
        ),
    }
    performance = {
        "model_calls_per_run": {
            "total": total_model_calls,
            "avg": round(sum(model_calls_per_run) / len(model_calls_per_run), 3) if model_calls_per_run else None,
            "p95": _percentile(model_calls_per_run, 0.95),
            "max": max(model_calls_per_run) if model_calls_per_run else None,
            "scope": "shared_database_model_response_events",
        },
        "avg_model_latency_ms": round(sum(model_latencies) / len(model_latencies), 3) if model_latencies else None,
        "p95_model_latency_ms": _percentile(model_latencies, 0.95),
        "model_latency_scope": "sum_of_model_response_latency_per_run",
        "avg_tool_duration_ms": round(sum(tool_durations) / len(tool_durations), 3) if tool_durations else None,
        "p95_tool_duration_ms": _percentile(tool_durations, 0.95),
        "tool_duration_scope": "run_payload_tool_call_duration",
        "avg_tool_calls_per_run": round(sum(tool_counts.values()) / len(tool_counts), 3) if tool_counts else None,
        "timeouts": {
            "runs": int(status_counts.get("timeout", 0)),
            "llm": int(status_counts.get("llm_timeout", 0)),
            "tool_calls": int(tool_status.get("timeout", 0)),
        },
        "retries": {
            "llm": int(event_counts.get("llm_retry", 0)),
            "tool": None,
            "tool_note": "工具重试次数未单独持久化，无法从 run store 计算。",
        },
        "by_domain": by_domain,
        "domain_scope": "evidence_domain_joined_to_runs",
    }

    try:
        from ..observability.setup import tracing_status

        observability = tracing_status().as_dict()
    except Exception:  # pragma: no cover - defensive import guard
        observability = {
            "otel_enabled": False,
            "otel_sdk_available": False,
            "exporter_configured": False,
            "exporter_kind": None,
            "initialized": False,
            "init_error": "observability module unavailable",
            "exported_spans": 0,
            "dropped_spans": 0,
            "exported_spans_scope": "process_local",
            "note": "tracing status could not be read",
        }
    observability["otel_enabled"] = bool(getattr(settings, "otel_enabled", False))

    return {
        "generated_at": generated_at,
        "scope": "shared_database",
        "runs": {
            "total": total_runs,
            "by_status": status_counts,
            "completed": completed,
            "failed": failed,
            "terminal": terminal_runs,
            "success_rate": _rate(completed, terminal_runs),
            "failure_rate": _rate(failed, terminal_runs),
            "avg_execution_seconds": round(sum(durations) / len(durations), 3) if durations else None,
            "p95_execution_seconds": _percentile(durations, 0.95),
            "resumed_runs": len(resumed_runs),
            "resume_count": sum(int(row.resume_count or 0) for row in run_rows),
            "resume_success_rate": _rate(resumed_completed, len(resumed_runs)),
        },
        "tools": {
            "total_calls": total_tool_calls,
            "avg_calls_per_run": round(sum(tool_counts.values()) / len(tool_counts), 3) if tool_counts else None,
            "by_status": tool_status,
            "failure_rate": _rate(tool_failures, total_tool_calls),
        },
        "model": {
            "calls": total_model_calls,
            "calls_per_run": performance["model_calls_per_run"]["avg"],
            "retries": int(event_counts.get("llm_retry", 0)),
            "errors": int(event_counts.get("llm_error", 0)),
            "avg_latency_ms": performance["avg_model_latency_ms"],
            "p95_latency_ms": performance["p95_model_latency_ms"],
            "input_tokens": tokens_in,
            "output_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        },
        "cost": cost,
        "performance": performance,
        "checkpoints": {
            "count": checkpoint_count,
            "storage_bytes": checkpoint_bytes,
            "avg_bytes": round(checkpoint_bytes / checkpoint_count, 1) if checkpoint_count else None,
        },
        "validation": {
            "reports": report_total,
            "validated": report_validated,
            "pass_rate": _rate(report_validated, report_total),
            "numeric_interceptions": numeric_interceptions,
            "unsourced_numbers": unsourced_numbers,
            "evidence_runs": evidence_runs,
            "evidence_coverage": _rate(evidence_runs, completed),
            "findings": finding_count,
        },
        "approvals": {
            "total": sum(approval_counts.values()),
            "by_state": approval_counts,
            "pending": approval_counts.get("pending", 0),
            "avg_pending_wait_seconds": round(sum(pending_waits) / len(pending_waits), 3) if pending_waits else None,
            "avg_decision_wait_seconds": round(sum(decided_waits) / len(decided_waits), 3) if decided_waits else None,
        },
        "database": {
            "backend": store.engine.url.get_backend_name(),
            "connection_errors": int(getattr(store, "connection_errors").count)
            if hasattr(store, "connection_errors")
            else 0,
            "connection_errors_scope": "process_local",
        },
        "observability": observability,
    }


__all__ = ["metrics_snapshot"]
