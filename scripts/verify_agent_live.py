"""Live Phase 4 acceptance check against a real Anthropic Messages endpoint.

This script makes real network calls and real database queries. It is NOT part
of the offline test suite. Run it only after explicitly configuring a key:

    export ANTHROPIC_API_KEY=...
    export LLM_MODEL=claude-sonnet-5-5
    export LLM_BASE_URL=https://api.anthropic.com/v1/messages
    python scripts/verify_agent_live.py --check-config
    python scripts/verify_agent_live.py --question "华东2025年12月销售为什么下降？"

`.env` is not loaded automatically; export the variables in this shell. If the
key was ever exposed, rotate it at the provider console before running. The
script prints configuration and a redacted key status, never the key value, and
exits non-zero without making a request when configuration is inconsistent.
It prints every tool call, evidence draft and structured status. Exit code 0
only when the agent finishes with evidence; a fake client is never used here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from enterprise_business_agent.agent import (  # noqa: E402
    AgentRuntime,
    AgentStatus,
    AnalysisGateway,
    ToolRegistry,
)
from enterprise_business_agent.config import get_settings  # noqa: E402
from enterprise_business_agent.db import make_readonly_engine  # noqa: E402
from enterprise_business_agent.llm import build_llm_client  # noqa: E402
from enterprise_business_agent.security import AuthorizationPolicy, Principal  # noqa: E402

DEFAULT_QUESTION = "华东2025年12月销售表现如何？哪些产品贡献最大？"

ANALYST = Principal(
    "key:live-check",
    "analyst",
    frozenset({"metrics:read", "agent:run"}),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Phase 4 agent with a real LLM")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="only validate live LLM configuration; never prints the key",
    )
    args = parser.parse_args()

    settings = get_settings()
    print(
        f"config: provider={settings.llm_provider} model={settings.llm_model} "
        f"base_url={settings.llm_base_url} "
        f"key={'configured' if settings.llm_enabled else 'not configured'}"
    )
    if not settings.llm_enabled:
        if Path(".env").exists():
            print(f"NOTE: a .env file was found but did not provide a key for provider {settings.llm_provider!r}.")
        print(f"FAIL: no API key for provider {settings.llm_provider!r}; real LLM calls: 0.")
        return 1
    issues = settings.llm_endpoint_issues()
    if issues:
        print("FAIL: live acceptance configuration is inconsistent; no request was sent:")
        for issue in issues:
            print(f"  - {issue}")
        print(
            "Fix the provider/model/base URL, and rotate any previously exposed key "
            "at the provider console before retrying."
        )
        return 1
    if args.check_config:
        print("PASS: live LLM configuration is consistent with the Anthropic Messages API.")
        return 0

    client = build_llm_client(settings)
    if client is None:
        print("FAIL: no LLM client was built.")
        return 1

    engine = make_readonly_engine(settings.application_database_url, settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    registry = ToolRegistry(
        AnalysisGateway(factory, settings),
        AuthorizationPolicy(settings),
        settings,
    )
    runtime = AgentRuntime(llm_client=client, registry=registry, settings=settings)

    print(f"provider={settings.llm_provider} model={settings.llm_model} key=<redacted>")
    print(f"question={args.question}")
    try:
        result = runtime.run(args.question, ANALYST)
    finally:
        engine.dispose()

    print(f"\nrun_id={result.run_id}")
    print(f"status={result.status.value} stop_reason={result.stop_reason}")
    print(f"planner={result.planner} planner_model={result.planner_model}")
    print("\ntrace:")
    for step in result.steps:
        print(f"  step {step.index}: action={step.action} tool={step.tool} call_id={step.tool_call_id}")
    for call in result.tool_calls:
        print(
            f"  tool {call.tool} status={call.status} duration_ms={call.duration_ms} evidence_ids={call.evidence_ids}"
        )
        print(f"    args={json.dumps(call.arguments, ensure_ascii=False)}")
        print(f"    summary={call.summary}")
    print("\nevidence drafts:")
    for item in result.evidence:
        print(
            f"  {item.evidence_id} call={item.tool_call_id} metric={item.metric} "
            f"range={item.time_range_start}..{item.time_range_end} "
            f"baseline={item.baseline_period} source={item.source} "
            f"traceable={item.traceable} missing={item.missing_fields}"
        )
    print(f"\nstate: {json.dumps(result.state.model_dump(mode='json'), ensure_ascii=False)}")
    print(f"\nfinal_answer={result.final_answer}")
    print(f"warnings={result.warnings}")

    ok = result.status in {AgentStatus.COMPLETED, AgentStatus.COMPLETED_WITH_WARNINGS}
    return 0 if ok and result.evidence else 1


if __name__ == "__main__":
    raise SystemExit(main())
