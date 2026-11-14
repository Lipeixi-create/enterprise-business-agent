from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from ..metric_resolver import MetricResolver
from .contracts import EvidenceRecord, EvidenceStatus, EvidenceType
from .ledger import EvidenceLedger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..agent.tools import ToolResult


_TOOL_TYPES: dict[str, EvidenceType] = {
    "query_metric": "metric_value",
    "compare_periods": "comparison",
    "top_n": "ranking",
    "contribution": "contribution",
    "trend": "trend",
    "anomaly_scan": "anomaly",
    "describe_metric": "metric_definition",
}

# Tools that expose the metric catalog do not carry a business fact.
_NON_EVIDENCE_TOOLS = frozenset({"list_metrics"})

_INSUFFICIENT = frozenset({"no_data", "insufficient_data"})
_FAILED = frozenset({"timeout", "execution_error"})

_CALCULATION = {
    "query_metric": "deterministic metric aggregation",
    "compare_periods": "deterministic period comparison",
    "top_n": "deterministic ranking",
    "contribution": "deterministic additive contribution",
    "trend": "deterministic trend direction",
    "anomaly_scan": "deterministic anomaly rule",
    "describe_metric": "semantic layer definition",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class EvidenceCollector:
    """Turn executed tool results into ledger records.

    The collector is a pure adapter over already-validated tool output: it does
    not re-run queries, round values, or reinterpret a metric. Failed calls are
    recorded with an explicit non-valid status instead of being dropped or
    promoted to evidence.
    """

    def __init__(
        self,
        ledger: EvidenceLedger,
        *,
        resolver: MetricResolver | None = None,
    ):
        self.ledger = ledger
        self.resolver = resolver or MetricResolver()

    def collect(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool: str,
        arguments: dict[str, Any],
        result: "ToolResult",
    ) -> list[EvidenceRecord]:
        return [record for record, _ in self.collect_persisted(
            run_id=run_id,
            tool_call_id=tool_call_id,
            tool=tool,
            arguments=arguments,
            result=result,
        )]

    def collect_persisted(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool: str,
        arguments: dict[str, Any],
        result: "ToolResult",
    ) -> list[tuple[EvidenceRecord, bool]]:
        if tool in _NON_EVIDENCE_TOOLS:
            return []
        evidence_type = _TOOL_TYPES.get(tool, "metric_value")
        status = _status_for(str(result.status))
        fingerprint = hashlib.sha256(
            _canonical(
                {
                    "tool": tool,
                    "arguments": arguments,
                    "run": run_id,
                }
            ).encode("utf-8")
        ).hexdigest()
        metric_code = arguments.get("metric")
        definition = self.resolver.get(metric_code) if metric_code else None
        explain = (result.data or {}).get("explain") or {}
        source_tables = sorted(
            {
                item
                for item in [
                    explain.get("source_table"),
                    getattr(definition, "source_table", None),
                ]
                if item
            }
        )
        time_start, time_end, baseline_period, baseline_start, baseline_end = _ranges(
            arguments
        )
        record = EvidenceRecord(
            evidence_id=f"{run_id[:8]}-{tool_call_id}-e1",
            run_id=run_id,
            tool_call_id=tool_call_id,
            evidence_type=evidence_type,
            domain=getattr(definition, "domain", None),
            metric_code=metric_code,
            metric_definition_version=getattr(definition, "version", None),
            time_range_start=time_start,
            time_range_end=time_end,
            baseline_period=baseline_period,
            baseline_time_range_start=baseline_start,
            baseline_time_range_end=baseline_end,
            grain=_grain(arguments, result),
            filters=list(arguments.get("filters") or []),
            dimensions=_dimensions(arguments, result),
            query_parameters=dict(arguments),
            result=dict(result.data or {}),
            result_summary=result.summary,
            unit=getattr(result, "unit", None) or (result.data or {}).get("unit"),
            source_tables=source_tables,
            calculation_method=_CALCULATION.get(tool),
            status=status,
            error_code=result.error.code if result.error else None,
            error_message=result.error.message if result.error else None,
            traceable=bool(source_tables) and status == "valid",
            fingerprint=fingerprint,
            created_at=_now(),
        )
        return self.ledger.record_many([record])


def _status_for(tool_status: str) -> EvidenceStatus:
    if tool_status == "ok":
        return "valid"
    if tool_status in _INSUFFICIENT:
        return "insufficient_data"
    if tool_status == "not_calculable":
        return "not_calculable"
    if tool_status in _FAILED:
        return "failed"
    return "rejected"


def _ranges(arguments: dict[str, Any]):
    if "current_start" in arguments:
        return (
            _str(arguments.get("current_start")),
            _str(arguments.get("current_end")),
            _period_label(arguments.get("baseline_start"), arguments.get("baseline_end")),
            _str(arguments.get("baseline_start")),
            _str(arguments.get("baseline_end")),
        )
    if arguments.get("period") or arguments.get("kind"):
        start, end, baseline_start, baseline_end, baseline_period = _comparison_ranges(
            arguments
        )
        return start, end, baseline_period, baseline_start, baseline_end
    return (
        _str(arguments.get("start")),
        _str(arguments.get("end")),
        None,
        None,
        None,
    )


def _comparison_ranges(arguments: dict[str, Any]):
    kind = arguments.get("kind")
    if kind in {"mom", "yoy"} and arguments.get("period"):
        current = _month(arguments["period"])
        offset = -1 if kind == "mom" else -12
        baseline = _shift(current, offset)
        return current, current, baseline, baseline, baseline
    if kind == "yoy_year" and arguments.get("year"):
        year = int(arguments["year"])
        return (
            f"{year}-01-01",
            f"{year}-12-01",
            f"{year - 1}-01-01",
            f"{year - 1}-12-01",
            str(year - 1),
        )
    year, quarter = arguments.get("year"), arguments.get("quarter")
    if year and quarter:
        start_month = (int(quarter) - 1) * 3 + 1
        end_month = int(quarter) * 3
        start = f"{int(year)}-{start_month:02d}-01"
        end = f"{int(year)}-{end_month:02d}-01"
        if kind == "qoq":
            baseline_start = _shift_months(start, -3)
            baseline_end = _shift_months(end, -3)
        else:
            baseline_start = f"{int(year) - 1}-{start_month:02d}-01"
            baseline_end = f"{int(year) - 1}-{end_month:02d}-01"
        return start, end, baseline_start, baseline_end, f"{baseline_start[:7]}"
    return None, None, None, None, None


def _month(value: Any) -> str:
    text = _str(value) or ""
    return f"{text[:7]}-01"


def _shift(value: str, offset: int) -> str:
    return _shift_months(value, offset)


def _shift_months(value: str, offset: int) -> str:
    year, month = int(value[:4]), int(value[5:7])
    index = year * 12 + (month - 1) + offset
    return f"{index // 12}-{index % 12 + 1:02d}-01"


def _period_label(start: Any, end: Any) -> str | None:
    start_text, end_text = _str(start), _str(end)
    if start_text and end_text:
        return f"{start_text[:7]}..{end_text[:7]}"
    return start_text or end_text


def _str(value: Any) -> str | None:
    return None if value is None else str(value)


def _grain(arguments: dict[str, Any], result: "ToolResult") -> str | None:
    grain = arguments.get("grain")
    if grain:
        return str(grain)
    kind = arguments.get("kind")
    if kind in {"mom", "yoy"}:
        return "month"
    if kind in {"qoq", "yoy_quarter"}:
        return "quarter"
    if kind == "yoy_year":
        return "year"
    if not any(
        key in arguments
        for key in ("start", "period", "current_start", "year", "kind")
    ):
        return None
    for point in (result.data or {}).get("points") or []:
        period = str((point or {}).get("period") or "")
        if len(period) == 10 and period[4] == "-" and period[7] == "-":
            return "month"
        if len(period) == 7 and period[4] == "-":
            return "month"
    return None


def _dimensions(arguments: dict[str, Any], result: "ToolResult") -> dict[str, str]:
    dimensions: dict[str, str] = {}
    for item in arguments.get("dimensions") or []:
        dimensions[str(item)] = ""
    single = arguments.get("dimension")
    if single:
        dimensions[str(single)] = ""
    if not dimensions:
        for point in (result.data or {}).get("points") or []:
            for key in (point or {}).get("dimensions") or {}:
                dimensions[str(key)] = ""
    for item in arguments.get("filters") or []:
        if isinstance(item, dict) and item.get("dimension"):
            value = item.get("value")
            if isinstance(value, list):
                value = ",".join(str(part) for part in value)
            dimensions[str(item["dimension"])] = str(value)
    return dimensions
