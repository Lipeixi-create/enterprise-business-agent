#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enterprise_business_agent.config import get_settings  # noqa: E402
from enterprise_business_agent.db import make_readonly_session_factory  # noqa: E402
from enterprise_business_agent.nl import NaturalLanguageAnalyzer, NaturalLanguageRequest  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402


DEMOS = [
    "2025年第四季度净利润环比变化",
    "华东销售额环比变化",
    "2025年营销活动 ROI Top 5",
    "2025年第四季度商品库存风险排名前5",
    "2025年各部门人力成本贡献",
    "华东销售下降是否与核心商品缺货有关",
]


def _print_result(request_id, response) -> None:
    print(f"\n[{response.status.value}] {response.question}  (request_id={request_id})")
    print(f"  planner={response.planner} metric={response.metric} type={response.result_type}")
    if response.error:
        print(f"  error={response.error.code}: {response.error.message}")
    if response.result_type is None:
        return
    if response.result_type.value == "comparison":
        item = response.result.items[0]
        print(
            f"  {item.current_period}={item.current_value} vs {item.baseline_period}={item.baseline_value}"
            f" change={item.absolute_change} rate={item.change_rate}"
        )
    elif response.result_type.value == "top_n":
        print("  " + " | ".join(f"{item.dimension_value}={item.value}" for item in response.result.items))
    elif response.result_type.value == "contribution":
        print(
            f"  total_change={response.result.total_change} sum={response.result.sum_contributions}"
        )
        print(
            "  "
            + " | ".join(
                f"{item.dimension_value}={item.contribution} ({item.contribution_rate})"
                for item in response.result.items
            )
        )
    elif response.result_type.value == "investigation":
        for observation in response.result.observations:
            print(
                f"  observation {observation.name}: status={observation.status.value}"
                f" value={observation.value} baseline={observation.baseline_value}"
                f" change={observation.change_rate}"
            )
        print(f"  inference: {response.result.inference}")
        for disclaimer in response.result.disclaimers:
            print(f"  disclaimer: {disclaimer}")
    else:
        print(f"  {response.explanation}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the five Phase 3 demos plus the cross-metric investigation.")
    parser.add_argument("--database-url", default=None, help="Defaults to READONLY_DATABASE_URL or DATABASE_URL")
    args = parser.parse_args()

    settings = get_settings()
    database_url = args.database_url or settings.application_database_url
    print(f"database: {make_url(database_url).render_as_string(hide_password=True)}")
    factory = make_readonly_session_factory(database_url, timeout_ms=settings.query_timeout_ms)
    failures = 0
    with factory() as session:
        analyzer = NaturalLanguageAnalyzer(session, settings)
        for index, question in enumerate(DEMOS, start=1):
            response = analyzer.analyze(
                NaturalLanguageRequest(question=question, as_of=date(2025, 12, 1))
            )
            _print_result(f"demo-{index}", response)
            if response.status.value != "ok":
                failures += 1
    print(f"\n{len(DEMOS) - failures}/{len(DEMOS)} demos returned status=ok")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
