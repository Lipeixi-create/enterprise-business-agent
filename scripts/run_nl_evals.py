#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from enterprise_business_agent.config import Settings  # noqa: E402
from enterprise_business_agent.data_generator import generate_data  # noqa: E402
from enterprise_business_agent.db import Base  # noqa: E402
from enterprise_business_agent.evaluation import (  # noqa: E402
    check_analyze,
    check_guard,
    check_plan,
    load_cases,
)
from enterprise_business_agent.nl import NaturalLanguageAnalyzer, RuleBasedPlanner  # noqa: E402
from enterprise_business_agent.security import SQLGuard  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the offline NL/SQL evaluation set.")
    parser.add_argument(
        "--cases",
        default=str(ROOT / "tests" / "evals" / "nl_cases.jsonl"),
        help="JSONL evaluation cases",
    )
    parser.add_argument("--verbose", action="store_true", help="print every case")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    settings = Settings()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    generate_data(session, date(2024, 1, 1), settings.data_months, reset=False)

    planner = RuleBasedPlanner(settings)
    analyzer = NaturalLanguageAnalyzer(session, settings)
    guard = SQLGuard(max_rows=settings.query_max_rows, max_offset=settings.sql_max_offset)

    passed = 0
    failed = 0
    for case in cases:
        try:
            if case["kind"] == "plan":
                failures = check_plan(case, planner)
            elif case["kind"] == "analyze":
                failures = check_analyze(case, analyzer)
            elif case["kind"] == "guard":
                failures = check_guard(case, guard)
            else:
                failures = [f"unknown case kind: {case['kind']}"]
        except Exception as exc:  # evaluation must report, not crash
            failures = [f"{type(exc).__name__}: {exc}"]
        if failures:
            failed += 1
            print(f"FAIL {case['id']} [{case['category']}] {case['question'] if 'question' in case else case.get('sql', '')}")
            for failure in failures:
                print(f"     {failure}")
        else:
            passed += 1
            if args.verbose:
                print(f"PASS {case['id']} [{case['category']}]")

    session.close()
    engine.dispose()
    total = passed + failed
    print(f"\n{passed}/{total} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
