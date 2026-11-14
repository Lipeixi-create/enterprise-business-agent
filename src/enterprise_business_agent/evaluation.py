from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .nl.contracts import NaturalLanguageRequest
from .nl.planner import RuleBasedPlanner
from .nl.service import NaturalLanguageAnalyzer
from .security import QueryError, SQLGuard


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            case = json.loads(line)
            case.setdefault("_line", line_number)
            cases.append(case)
    return cases


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _filter_tuples(intent) -> list[list[str]]:
    return [[item.dimension, item.operator, item.value] for item in intent.filters]


def check_plan(case: dict[str, Any], planner: RuleBasedPlanner) -> list[str]:
    expected = case.get("expected", {})
    intent = planner.plan(case["question"], as_of=_as_date(case.get("as_of")))
    if expected.get("intent", "missing") is None:
        return [] if intent is None else ["expected planner to return no intent"]
    if intent is None:
        return ["planner returned no intent"]

    failures: list[str] = []
    for field in (
        "operation",
        "metric",
        "metric_candidates",
        "comparison_kind",
        "dimension",
        "dimensions",
        "n",
        "quarter",
        "region",
        "products",
    ):
        if field in expected and getattr(intent, field) != expected[field]:
            failures.append(f"{field}: expected {expected[field]!r}, got {getattr(intent, field)!r}")
    if "grain" in expected and intent.grain.value != expected["grain"]:
        failures.append(f"grain: expected {expected['grain']!r}, got {intent.grain.value!r}")
    if "start" in expected and (intent.start.isoformat() if intent.start else None) != expected["start"]:
        failures.append(f"start: expected {expected['start']!r}, got {intent.start}")
    if "end" in expected and (intent.end.isoformat() if intent.end else None) != expected["end"]:
        failures.append(f"end: expected {expected['end']!r}, got {intent.end}")
    if "filters" in expected and _filter_tuples(intent) != expected["filters"]:
        failures.append(f"filters: expected {expected['filters']!r}, got {_filter_tuples(intent)!r}")
    return failures


def check_analyze(case: dict[str, Any], analyzer: NaturalLanguageAnalyzer) -> list[str]:
    expected = case.get("expected", {})
    response = analyzer.analyze(
        NaturalLanguageRequest(
            question=case["question"],
            as_of=_as_date(case.get("as_of")),
        )
    )
    failures: list[str] = []
    if "status" in expected and response.status.value != expected["status"]:
        failures.append(f"status: expected {expected['status']!r}, got {response.status.value!r}")
    if "result_type" in expected:
        actual = response.result_type.value if response.result_type else None
        if actual != expected["result_type"]:
            failures.append(f"result_type: expected {expected['result_type']!r}, got {actual!r}")
    if "metric" in expected and response.metric != expected["metric"]:
        failures.append(f"metric: expected {expected['metric']!r}, got {response.metric!r}")
    if "candidates" in expected:
        actual = sorted(item.name for item in response.candidates)
        if actual != sorted(expected["candidates"]):
            failures.append(f"candidates: expected {expected['candidates']!r}, got {actual!r}")
    value_check = expected.get("value")
    if value_check:
        failures.extend(_check_analyze_value(response, value_check))
    return failures


def _check_analyze_value(response, check: dict[str, Any]) -> list[str]:
    if response.result is None:
        return [f"value: expected {check.get('path')!r}, result is missing"]
    payload = response.result.model_dump(mode="json")
    try:
        observed = _lookup(payload, str(check["path"]))
    except (KeyError, IndexError, TypeError) as exc:
        return [f"value: path {check.get('path')!r} not found ({type(exc).__name__})"]
    if "equals" in check:
        return [] if str(observed) == str(check["equals"]) else [
            f"value: expected {check['equals']!r}, got {observed!r}"
        ]
    try:
        numeric = float(observed)
    except (TypeError, ValueError):
        return [f"value: {observed!r} is not numeric"]
    if "min" in check and numeric < float(check["min"]):
        return [f"value: {numeric} below min {check['min']}"]
    if "max" in check and numeric > float(check["max"]):
        return [f"value: {numeric} above max {check['max']}"]
    return []


def _lookup(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def check_guard(case: dict[str, Any], guard: SQLGuard) -> list[str]:
    expected = case.get("expected", {})
    try:
        guard.validate(case["sql"])
    except QueryError as exc:
        actual = exc.code.value
    else:
        actual = "allowed"
    if "code" in expected and actual != expected["code"]:
        return [f"guard code: expected {expected['code']!r}, got {actual!r}"]
    return []


def run_cases(
    cases: list[dict[str, Any]],
    *,
    planner: RuleBasedPlanner,
    analyzer: NaturalLanguageAnalyzer,
    guard: SQLGuard,
) -> list[tuple[dict[str, Any], list[str]]]:
    results: list[tuple[dict[str, Any], list[str]]] = []
    for case in cases:
        kind = case.get("kind")
        if kind == "plan":
            failures = check_plan(case, planner)
        elif kind == "analyze":
            failures = check_analyze(case, analyzer)
        elif kind == "guard":
            failures = check_guard(case, guard)
        else:
            failures = [f"unknown case kind: {kind!r}"]
        results.append((case, failures))
    return results
