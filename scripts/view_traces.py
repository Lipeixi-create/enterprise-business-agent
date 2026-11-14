#!/usr/bin/env python3
"""Render local JSONL spans exported by ``EBA_OTEL_EXPORTER=file``.

Usage:
    python scripts/view_traces.py data/traces/eba-spans.jsonl
    python scripts/view_traces.py --trace <trace_id> --attributes
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


def _load(path: Path) -> list[dict]:
    spans: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                spans.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return spans


def _id(value: str | None) -> str | None:
    if not value:
        return None
    return value[2:] if value.startswith("0x") else value


def _duration_ms(span: dict) -> float | None:
    start = span.get("start_time")
    end = span.get("end_time")
    if not start or not end:
        return None
    try:
        left = datetime.fromisoformat(start.replace("Z", "+00:00"))
        right = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round((right - left).total_seconds() * 1000, 3)


def _short(value: str | None, width: int = 16) -> str:
    value = _id(value) or ""
    return value[:width]


def render(spans: list[dict], *, trace_filter: str | None, show_attributes: bool) -> str:
    by_trace: dict[str, list[dict]] = {}
    for span in spans:
        trace_id = _id((span.get("context") or {}).get("trace_id"))
        if not trace_id:
            continue
        if trace_filter and trace_id != _id(trace_filter):
            continue
        by_trace.setdefault(trace_id, []).append(span)

    lines: list[str] = []
    for trace_id, items in sorted(
        by_trace.items(), key=lambda pair: min(i.get("start_time") or "" for i in pair[1])
    ):
        children: dict[str, list[dict]] = {}
        roots: list[dict] = []
        for span in items:
            parent = _id(span.get("parent_id"))
            if parent and any(
                _id((other.get("context") or {}).get("span_id")) == parent
                for other in items
            ):
                children.setdefault(parent, []).append(span)
            else:
                roots.append(span)

        service = ((items[0].get("resource") or {}).get("attributes") or {}).get(
            "service.name", "unknown"
        )
        lines.append(f"trace {trace_id}  service={service}  spans={len(items)}")

        def walk(span: dict, depth: int) -> None:
            span_id = _short((span.get("context") or {}).get("span_id"))
            duration = _duration_ms(span)
            status = (span.get("status") or {}).get("status_code", "")
            suffix = f" {duration}ms" if duration is not None else ""
            if status and status != "UNSET":
                suffix += f" [{status}]"
            lines.append(f"{'  ' * depth}- {span.get('name')} ({span_id}){suffix}")
            if show_attributes:
                attributes = span.get("attributes") or {}
                for key, value in sorted(attributes.items()):
                    lines.append(f"{'  ' * (depth + 1)}  {key}={value}")
                for event in span.get("events") or []:
                    lines.append(
                        f"{'  ' * (depth + 1)}  event={event.get('name')} "
                        f"{event.get('attributes') or ''}"
                    )
            for child in sorted(
                children.get(span_id, []), key=lambda item: item.get("start_time") or ""
            ):
                walk(child, depth + 1)

        for root in sorted(roots, key=lambda item: item.get("start_time") or ""):
            walk(root, 0)
        lines.append("")
    return "\n".join(lines) or "no spans matched"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        default="data/traces/eba-spans.jsonl",
        help="JSONL file written by the file exporter",
    )
    parser.add_argument("--trace", help="only show one trace id")
    parser.add_argument(
        "--attributes", action="store_true", help="print span attributes and events"
    )
    args = parser.parse_args()
    path = Path(args.path)
    if not path.exists():
        raise SystemExit(f"trace file not found: {path}")
    print(
        render(
            _load(path),
            trace_filter=args.trace,
            show_attributes=args.attributes,
        )
    )


if __name__ == "__main__":
    main()
