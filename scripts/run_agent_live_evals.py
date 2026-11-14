#!/usr/bin/env python
"""Run the Phase 4.1 live agent evaluation against a real LLM.

This script never substitutes a fake client. It refuses to run when the live
configuration is missing or inconsistent, and it stops on a transport/provider
failure after recording the real failure. A run is evidence only for the
questions actually executed; it is not a claim about untested questions.

Usage:
    export ANTHROPIC_API_KEY=...
    export LLM_MODEL=claude-sonnet-5-5
    export LLM_BASE_URL=https://api.anthropic.com/v1/messages
    python scripts/run_agent_live_evals.py                 # 5 smoke cases
    python scripts/run_agent_live_evals.py --limit 0       # all 26 cases
    python scripts/run_agent_live_evals.py --output data/exports/live_eval.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from enterprise_business_agent.agent import (  # noqa: E402
    AgentRuntime,
    AgentStatus,
    AnalysisGateway,
    ToolRegistry,
)
from enterprise_business_agent.agent.evaluation import (  # noqa: E402
    load_live_agent_cases,
    run_live_agent_case,
)
from enterprise_business_agent.config import get_settings  # noqa: E402
from enterprise_business_agent.db import make_readonly_engine  # noqa: E402
from enterprise_business_agent.llm import build_llm_client  # noqa: E402
from enterprise_business_agent.security import AuthorizationPolicy, Principal  # noqa: E402

CASES = ROOT / "tests" / "evals" / "live_agent_cases.jsonl"
PRINCIPAL = Principal(
    "key:live-eval",
    "analyst",
    frozenset({"metrics:read", "agent:run"}),
)
STOP_STATUSES = {
    AgentStatus.LLM_UNAVAILABLE.value,
    AgentStatus.LLM_TIMEOUT.value,
    AgentStatus.FAILED.value,
}


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or "<invalid>"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real-LLM Phase 4.1 evals")
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="number of cases to run; 0 runs all (default: 5 smoke cases)",
    )
    parser.add_argument("--ids", nargs="*", default=None, help="specific case ids")
    parser.add_argument("--output", default=None, help="write the full record as JSON")
    args = parser.parse_args()

    settings = get_settings()
    print(
        f"config: provider={settings.llm_provider} model={settings.llm_model} "
        f"host={_host(settings.llm_base_url)} "
        f"key={'configured' if settings.llm_enabled else 'not configured'}"
    )
    if not settings.llm_enabled:
        print(f"FAIL: no API key for provider {settings.llm_provider!r}; real LLM calls: 0.")
        return 1
    issues = settings.llm_endpoint_issues()
    if issues:
        print("FAIL: live configuration is inconsistent; no request was sent:")
        for issue in issues:
            print(f"  - {issue}")
        return 1
    client = build_llm_client(settings)
    if client is None:
        print("FAIL: no real LLM client was built; no request was sent.")
        return 1

    cases = load_live_agent_cases(CASES)
    if args.ids:
        wanted = set(args.ids)
        cases = [case for case in cases if case.id in wanted]
    elif args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        print("FAIL: no cases selected.")
        return 1

    engine = make_readonly_engine(settings.application_database_url, settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    registry = ToolRegistry(
        AnalysisGateway(factory, settings),
        AuthorizationPolicy(settings),
        settings,
    )
    runtime = AgentRuntime(llm_client=client, registry=registry, settings=settings)

    started = time.monotonic()
    records = []
    real_calls = 0
    stopped_reason: str | None = None
    print(f"cases: {len(cases)} (real LLM, no fallback)")
    try:
        for case in cases:
            real_calls += 1
            result = run_live_agent_case(
                case,
                runtime=runtime,
                registry=registry,
                principal=PRINCIPAL,
            )
            record = result.model_dump(mode="json")
            record["human_check"] = case.human_check
            records.append(record)
            verdict = "PASS" if result.passed else "FAIL"
            query = "n/a" if result.query_correct is None else result.query_correct
            print(
                f"{verdict} {result.id} [{result.category}] status={result.status} "
                f"plan={result.planning_correct} args={result.tool_argument_correct} "
                f"query={query} evidence={result.evidence_sufficient} "
                f"overreach={result.conclusion_overreach} "
                f"latency={result.duration_ms:.0f}ms "
                f"tokens={result.llm_tokens}"
            )
            if result.status in STOP_STATUSES:
                stopped_reason = f"{result.id}:{result.status}"
                print(f"STOP: real LLM run ended with {result.status}; not continuing.")
                break
    finally:
        engine.dispose()

    scored = [item for item in records if item["scored"]]
    passed = sum(1 for item in scored if item["passed"])
    failed = len(scored) - passed
    total_latency = round((time.monotonic() - started) * 1000, 3)
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "planner_model": getattr(client, "model", None),
        "provider": settings.llm_provider,
        "real_llm": True,
        "fake_client_used": False,
        "cases_selected": len(cases),
        "real_llm_calls": real_calls,
        "scored_cases": len(scored),
        "scored_passed": passed,
        "scored_failed": failed,
        "unscored_cases": len(records) - len(scored),
        "stopped_reason": stopped_reason,
        "wall_time_ms": total_latency,
        "llm_latency_ms_sum": round(sum(item["llm_latency_ms"] or 0 for item in records), 3),
        "input_tokens_sum": sum((item["llm_tokens"] or {}).get("input", 0) for item in records),
        "output_tokens_sum": sum((item["llm_tokens"] or {}).get("output", 0) for item in records),
        "records": records,
    }
    print(
        f"\nreal_llm_calls={real_calls} scored_passed={passed}/{len(scored)} "
        f"unscored={len(records) - len(scored)} stopped={stopped_reason}"
    )
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"record written to {output}")
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    ok = failed == 0 and stopped_reason is None and len(records) == len(cases)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
