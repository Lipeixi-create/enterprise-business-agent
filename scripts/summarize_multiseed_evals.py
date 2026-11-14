#!/usr/bin/env python
"""Aggregate repeated live-model evaluation runs, one per random seed.

Each input is a JSON file produced by ``scripts/run_phase7_evals.py`` for a
single seed. The script never mixes these numbers with a different regression
run: every figure is reported per seed and the source files are recorded.

Usage:
    python scripts/summarize_multiseed_evals.py \
        --run 42=data/exports/phase9_2_1_seed_42.json \
        --run 7=data/exports/phase9_2_1_seed_7.json \
        --output data/exports/phase9_2_1_multiseed.json \
        --markdown docs/PHASE9_2_1_MULTISEED.md
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROVIDER_FAILURE_STATUSES = {"llm_unavailable", "llm_timeout", "failed"}
BUDGET_STOP_STATUSES = {"timeout", "budget_exhausted"}


def _rate(part: int, total: int) -> float | None:
    return round(part / total, 4) if total else None


def _tool_sequence(record: dict[str, Any]) -> list[tuple[str, str]]:
    sequence = []
    for call in record.get("actual_plan") or []:
        tool = str(call.get("tool") or "")
        arguments = call.get("arguments") or {}
        try:
            rendered = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            rendered = str(arguments)
        sequence.append((tool, rendered))
    return sequence


def _seed_block(records: list[dict[str, Any]], source: str) -> dict[str, Any]:
    scored = [item for item in records if item.get("scored", True)]
    passed = [item for item in scored if item.get("passed")]
    completed = [item for item in scored if item.get("task_completed")]
    failures = [item for item in scored if str(item.get("status") or "") in PROVIDER_FAILURE_STATUSES]
    budget_stops = [item for item in scored if str(item.get("status") or "") in BUDGET_STOP_STATUSES]
    input_tokens = sum(int((item.get("llm_tokens") or {}).get("input") or 0) for item in scored)
    output_tokens = sum(int((item.get("llm_tokens") or {}).get("output") or 0) for item in scored)
    duration_ms = sum(float(item.get("duration_ms") or 0.0) for item in scored)
    model_calls = sum(int(item.get("model_calls") or 0) for item in scored)
    tool_calls = sum(int(item.get("tool_calls") or 0) for item in scored)
    latencies = [float(item.get("llm_latency_ms")) for item in scored if item.get("llm_latency_ms") is not None]
    return {
        "source": source,
        "cases": len(records),
        "scored_cases": len(scored),
        "passed": len(passed),
        "pass_rate": _rate(len(passed), len(scored)),
        "task_completed": len(completed),
        "task_completion_rate": _rate(len(completed), len(scored)),
        "provider_failures": len(failures),
        "provider_failure_ids": [item.get("id") for item in failures],
        "budget_stops": len(budget_stops),
        "budget_stop_ids": [item.get("id") for item in budget_stops],
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_per_case": (round((input_tokens + output_tokens) / len(scored), 1) if scored else None),
        "duration_seconds": round(duration_ms / 1000.0, 2),
        "mean_case_seconds": (round(duration_ms / 1000.0 / len(scored), 2) if scored else None),
        "mean_llm_latency_ms": (round(sum(latencies) / len(latencies), 1) if latencies else None),
    }


def _variation(per_seed: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    by_case: dict[str, dict[int, list[tuple[str, str]]]] = {}
    for seed, records in per_seed.items():
        for record in records:
            case_id = str(record.get("id"))
            by_case.setdefault(case_id, {})[seed] = _tool_sequence(record)

    varied = []
    for case_id in sorted(by_case):
        sequences = by_case[case_id]
        if len(sequences) < 2:
            continue
        baseline = next(iter(sequences.values()))
        if any(sequence != baseline for sequence in sequences.values()):
            varied.append(
                {
                    "id": case_id,
                    "sequences": {
                        str(seed): [[tool, arguments] for tool, arguments in sequence]
                        for seed, sequence in sorted(sequences.items())
                    },
                }
            )
    return {
        "cases_compared": len(by_case),
        "cases_with_differences": len(varied),
        "different_cases": varied,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Phase 9.2.1 multi-seed live evaluation",
        "",
        "Live GLM calls only. These results are reported per seed and are not "
        "combined with the 92-case full regression.",
        "",
        "| Seed | Cases | Passed | Pass rate | Task completion | Provider failures "
        "| Budget stops | Model calls | Tool calls | Input tokens | Output tokens "
        "| Wall-clock s |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for seed, block in payload["seeds"].items():
        lines.append(
            "| {seed} | {cases} | {passed} | {pass_rate} | {completion} | {failures} "
            "| {budget_stops} | {model_calls} | {tool_calls} | {input_tokens} "
            "| {output_tokens} | {duration_seconds} |".format(
                seed=seed,
                cases=block["scored_cases"],
                passed=block["passed"],
                pass_rate=_pct(block["pass_rate"]),
                completion=_pct(block["task_completion_rate"]),
                failures=block["provider_failures"],
                budget_stops=block["budget_stops"],
                model_calls=block["model_calls"],
                tool_calls=block["tool_calls"],
                input_tokens=block["input_tokens"],
                output_tokens=block["output_tokens"],
                duration_seconds=block["duration_seconds"],
            )
        )
    variation = payload["tool_call_variation"]
    lines += [
        "",
        f"Tool-call sequences compared across seeds: {variation['cases_compared']} "
        f"cases; {variation['cases_with_differences']} differed between at least "
        "two seeds. A sequence includes each call's arguments, not just the tool "
        "name. A case with a provider or budget failure can differ because one "
        "seed never reached that step, so read differences together with the "
        "per-seed failure lists.",
        "",
    ]
    if variation["different_cases"]:
        lines.append("Cases whose tool calls differed:")
        lines.append("")
        for item in variation["different_cases"]:
            lines.append(f"- `{item['id']}`")
            for seed, sequence in item["sequences"].items():
                rendered = " -> ".join(_call_label(tool, arguments) for tool, arguments in sequence) or "(no tool call)"
                lines.append(f"  - seed {seed}: {rendered}")
        lines.append("")
    lines.append("Source files:")
    for seed, block in payload["seeds"].items():
        lines.append(f"- seed {seed}: `{block['source']}`")
    lines.append("")
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def _call_label(tool: str, arguments: str) -> str:
    if arguments in {"", "{}"}:
        return tool
    label = f"{tool}{arguments}"
    return label if len(label) <= 110 else label[:107] + "..."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        metavar="SEED=PATH",
        help="one live evaluation JSON file and the seed it was run with",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown", default=None)
    args = parser.parse_args()

    per_seed: dict[int, list[dict[str, Any]]] = {}
    sources: dict[int, str] = {}
    for item in args.run:
        seed_text, _, path_text = item.partition("=")
        if not path_text:
            parser.error(f"--run expects SEED=PATH, got {item!r}")
        seed = int(seed_text)
        paths = Path(path_text)
        payload = json.loads(paths.read_text(encoding="utf-8"))
        records = list(payload.get("records") or [])
        per_seed[seed] = records
        sources[seed] = str(paths)

    seeds = {str(seed): _seed_block(per_seed[seed], sources[seed]) for seed in sorted(per_seed)}
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": "multi_seed_live_evaluation",
        "note": (
            "Each seed is an independent live-model run. Do not combine these "
            "figures with the 92-case full regression or with scripted tests."
        ),
        "seeds": seeds,
        "tool_call_variation": _variation(per_seed),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {output}")
    if args.markdown:
        markdown = Path(args.markdown)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(render_markdown(payload), encoding="utf-8")
        print(f"written {markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
