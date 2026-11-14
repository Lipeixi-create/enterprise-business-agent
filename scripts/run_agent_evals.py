#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from enterprise_business_agent.agent import AgentRuntime, AnalysisGateway, ToolRegistry  # noqa: E402
from enterprise_business_agent.agent.evaluation import (  # noqa: E402
    load_agent_cases,
    run_agent_case,
)
from enterprise_business_agent.config import Settings  # noqa: E402
from enterprise_business_agent.data_generator import generate_data  # noqa: E402
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine  # noqa: E402
from enterprise_business_agent.security import AuthorizationPolicy, Principal  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 4 agent control-flow eval set.")
    parser.add_argument(
        "--cases",
        default=str(ROOT / "tests" / "evals" / "agent_cases.jsonl"),
        help="JSONL agent cases",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cases = load_agent_cases(args.cases)
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        agent_max_tool_calls=6,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=5,
        agent_tool_max_retries=0,
    )
    principal = Principal("key:eval", "analyst", frozenset({"metrics:read", "agent:run"}))
    print("planner: fake-offline-plan-llm (scripted test double, NOT a real LLM)")
    print(f"cases: {len(cases)}")
    print("dimensions: planning | query | conclusion | status")

    passed = 0
    failed = 0
    with tempfile.TemporaryDirectory(prefix="eba-agent-eval-") as tmp:
        path = Path(tmp) / "eval.db"
        write_engine = make_engine(f"sqlite:///{path}")
        Base.metadata.create_all(write_engine)
        with Session(write_engine) as seed:
            generate_data(seed, date(2024, 1, 1), 24, reset=False)
            seed.commit()
        read_engine = make_readonly_engine(f"sqlite:///{path}", settings.query_timeout_ms)
        factory = sessionmaker(bind=read_engine, class_=Session, expire_on_commit=False)
        registry = ToolRegistry(
            AnalysisGateway(factory, settings),
            AuthorizationPolicy(settings),
            settings,
        )
        runtime = AgentRuntime(llm_client=None, registry=registry, settings=settings)
        try:
            for case in cases:
                result = run_agent_case(
                    case,
                    runtime=runtime,
                    registry=registry,
                    principal=principal,
                )
                ok = (
                    result.planning_correct
                    and result.query_correct
                    and result.conclusion_correct
                    and result.detail["status_correct"]
                )
                if ok:
                    passed += 1
                    if args.verbose:
                        print(
                            f"PASS {result.id} planning={result.planning_correct}"
                            f" query={result.query_correct}"
                            f" conclusion={result.conclusion_correct}"
                            f" status={result.status}"
                        )
                else:
                    failed += 1
                    print(
                        f"FAIL {result.id} planning={result.planning_correct}"
                        f" query={result.query_correct}"
                        f" conclusion={result.conclusion_correct}"
                        f" status={result.status}"
                    )
                    print(f"     observed={result.detail['observed']}")
                    print(f"     actual_plan={[item.model_dump() for item in result.actual_plan]}")
                    print(f"     answer={result.final_answer}")
        finally:
            read_engine.dispose()
            write_engine.dispose()

    total = passed + failed
    print(f"\n{passed}/{total} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
