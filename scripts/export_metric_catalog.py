#!/usr/bin/env python3
"""把语义层指标目录导出成工作台可视化页可直接 fetch 的静态 JSON。

语义词义层是唯一事实来源（semantic_layer/metrics.json），前端不重复维护指标清单。
改动指标后重新执行：

    PYTHONPATH=src .venv/bin/python scripts/export_metric_catalog.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "semantic_layer" / "metrics.json"
TARGET = ROOT / "src" / "enterprise_business_agent" / "web" / "metrics_catalog.json"

FIELDS = ("name", "label", "unit", "domain", "description", "dimensions", "time_grains", "metric_type")


def main() -> int:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    metrics = payload.get("metrics") or []
    catalog = []
    for item in metrics:
        if not item.get("name"):
            continue
        entry = {field: item.get(field) for field in FIELDS}
        entry["dimensions"] = entry["dimensions"] or []
        entry["time_grains"] = entry["time_grains"] or ["month"]
        catalog.append(entry)
    if not catalog:
        print(f"no metrics found in {SOURCE}", file=sys.stderr)
        return 1
    TARGET.write_text(
        json.dumps(
            {
                "version": payload.get("version"),
                "grain_note": payload.get("grain_note"),
                "metrics": catalog,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(catalog)} metrics -> {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
