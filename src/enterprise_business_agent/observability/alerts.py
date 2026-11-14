"""Operational alert evaluation over the shared run metrics.

Rules are intentionally small and data-driven: every alert reports the metric,
the threshold and the window it was evaluated over. There is no paging
integration here; ``/api/v1/alerts`` is the stable interface an external
monitor can poll, and the response can be forwarded to Alertmanager or a
webhook by deployment tooling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _alert(
    code: str,
    severity: str,
    message: str,
    *,
    observed: Any,
    threshold: Any,
    scope: str,
    runbook: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "observed": observed,
        "threshold": threshold,
        "scope": scope,
        "runbook": runbook,
    }


def evaluate_alerts(metrics: dict[str, Any], settings: Any) -> dict[str, Any]:
    """Evaluate the configured rules against a metrics snapshot."""

    alerts: list[dict[str, Any]] = []
    runs = metrics.get("runs") or {}
    terminal = int(runs.get("terminal") or 0)
    min_runs = int(getattr(settings, "alert_min_runs", 5) or 1)
    failure_rate = runs.get("failure_rate")
    failure_limit = float(getattr(settings, "alert_failure_rate", 0.25))
    if terminal >= min_runs and failure_rate is not None and failure_rate > failure_limit:
        alerts.append(
            _alert(
                "run_failure_rate_high",
                "critical" if failure_rate >= failure_limit * 2 else "warning",
                f"Terminal run failure rate is {failure_rate:.0%}, above the {failure_limit:.0%} threshold.",
                observed=failure_rate,
                threshold=failure_limit,
                scope=f"{terminal} terminal runs in the shared run store",
                runbook="Inspect failed run events, stop_reason and llm_error details; "
                "check provider availability before retrying.",
            )
        )

    p95 = runs.get("p95_execution_seconds")
    p95_limit = float(getattr(settings, "alert_p95_seconds", 300.0))
    if terminal >= min_runs and p95 is not None and p95 > p95_limit:
        alerts.append(
            _alert(
                "run_p95_latency_high",
                "warning",
                f"P95 run execution time is {p95:.1f}s, above the {p95_limit:.1f}s threshold.",
                observed=p95,
                threshold=p95_limit,
                scope=f"{terminal} terminal runs",
                runbook="Check model latency, tool timeouts and queue wait; a high p95 often follows provider retries.",
            )
        )

    approvals = metrics.get("approvals") or {}
    pending = int(approvals.get("pending") or 0)
    pending_limit = int(getattr(settings, "alert_pending_approvals", 10))
    if pending > pending_limit:
        alerts.append(
            _alert(
                "approvals_backlog",
                "warning",
                f"{pending} report approvals are pending, above the {pending_limit} threshold.",
                observed=pending,
                threshold=pending_limit,
                scope="agent_report_approvals where state=pending",
                runbook="Open the approval workbench; check whether an approver "
                "with approval:decide is available and whether reports are stale.",
            )
        )

    performance = metrics.get("performance") or {}
    timeouts = performance.get("timeouts") or {}
    if int(timeouts.get("llm") or 0) > 0:
        alerts.append(
            _alert(
                "llm_timeout_observed",
                "warning",
                f"{int(timeouts.get('llm') or 0)} run(s) ended with an LLM timeout.",
                observed=int(timeouts.get("llm") or 0),
                threshold=0,
                scope="runs.status=llm_timeout",
                runbook="Check provider latency and LLM_TIMEOUT_SECONDS. Provider "
                "retries are bounded; repeated timeouts need a lower-latency model "
                "or a larger request timeout, not an unbounded retry loop.",
            )
        )

    database = metrics.get("database") or {}
    connection_errors = int(database.get("connection_errors") or 0)
    if connection_errors > 0:
        alerts.append(
            _alert(
                "database_connection_errors",
                "critical",
                f"{connection_errors} database connection error(s) were observed in this process.",
                observed=connection_errors,
                threshold=0,
                scope="process-local run store counter",
                runbook="Check the runtime database, credentials and pool health. "
                "Do not print the connection string; inspect the configured role.",
            )
        )

    observability = metrics.get("observability") or {}
    if observability.get("otel_enabled") and not observability.get("initialized"):
        alerts.append(
            _alert(
                "tracing_not_initialized",
                "warning",
                "Tracing is enabled but no exporter was initialised: "
                f"{observability.get('init_error') or observability.get('note')}",
                observed=observability.get("init_error"),
                threshold="initialized=true",
                scope="process-local tracing status",
                runbook="Check EBA_OTEL_EXPORTER, the endpoint/file path, and that "
                "the optional OTel packages are installed.",
            )
        )

    severities = {item["severity"] for item in alerts}
    status = "critical" if "critical" in severities else "warning" if alerts else "ok"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "status": status,
        "alerts": alerts,
        "evaluated": {
            "terminal_runs": terminal,
            "failure_rate": failure_rate,
            "p95_execution_seconds": p95,
            "pending_approvals": pending,
            "llm_timeouts": int(timeouts.get("llm") or 0),
            "database_connection_errors": connection_errors,
            "min_runs_for_rate_rules": min_runs,
        },
        "note": "Polling interface only; no external notification was sent.",
    }


__all__ = ["evaluate_alerts"]
