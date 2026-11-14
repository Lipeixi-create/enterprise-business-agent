"""Run the five Phase 5 business acceptance scenarios against the configured LLM.

The key is read from the existing environment / local dotenv file. It is never
printed, copied or written by this script. Results distinguish real provider
execution from scripted test doubles by reporting the configured provider and
model, token usage and latency.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from enterprise_business_agent.agent import AgentRuntime, AnalysisGateway, ToolRegistry  # noqa: E402
from enterprise_business_agent.config import get_settings, load_env_file  # noqa: E402
from enterprise_business_agent.db import make_readonly_engine  # noqa: E402
from enterprise_business_agent.evidence import Phase5Pipeline  # noqa: E402
from enterprise_business_agent.llm import build_llm_client  # noqa: E402
from enterprise_business_agent.security import AuthorizationPolicy, Principal  # noqa: E402

SCENARIOS = {
    "A": "分析华东 2025 年 12 月销售额下降的原因，并给出可验证的改进建议。",
    "B": "分析 2025 年第四季度营销活动的投入产出（ROI）变化，请区分事实、统计关联和假设。",
    "C": "分析 2025 年第四季度库存积压情况，并评估可能的资金占用成本。",
    "D": "综合销售、市场、财务、库存和人力数据，分析 2025 年第四季度的整体经营表现。",
    "E": "预测 2026 年员工满意度和离职率的变化趋势。",
}

REFUSAL_SIGNALS = (
    "无法预测",
    "不能预测",
    "无法可靠",
    "数据不足",
    "缺少",
    "不足以",
    "没有依据",
    "无法给出",
)


def _summary(result, pipeline: Phase5Pipeline) -> dict:
    evidences = pipeline.ledger.list(result.run_id)
    findings = pipeline.finding_store.list(result.run_id)
    stored = pipeline.report_store.latest(result.run_id)
    report = stored.report if stored else None
    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "stop_reason": result.stop_reason,
        "final_answer": result.final_answer,
        "warnings": result.warnings,
        "tool_calls": [
            {
                "tool": item.tool,
                "status": item.status,
                "arguments": item.arguments,
                "summary": item.summary,
            }
            for item in result.tool_calls
        ],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "type": item.evidence_type,
                "status": item.status,
                "metric": item.metric_code,
                "tool_call_id": item.tool_call_id,
                "source_tables": item.source_tables,
                "metric_definition_version": item.metric_definition_version,
            }
            for item in evidences
        ],
        "findings": [
            {
                "finding_id": item.finding_id,
                "type": item.finding_type,
                "severity": item.severity,
                "statement": item.statement,
                "evidence_ids": item.evidence_ids,
            }
            for item in findings
        ],
        "report": None
        if stored is None
        else {
            "report_id": stored.report_id,
            "version": stored.version,
            "status": stored.status,
            "validation_passed": stored.validation.passed,
            "narrative_mode": report.narrative_mode if report else None,
            "conclusion_count": len(report.conclusions) if report else 0,
            "recommendation_count": len(report.recommendations) if report else 0,
            "issues": [issue.model_dump(mode="json") for issue in stored.validation.issues],
            "executive_summary": report.executive_summary if report else None,
        },
        "llm": {
            "latency_ms": result.llm_latency_ms,
            "tokens": result.llm_tokens,
            "decisions": len(result.steps),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios",
        default="ABCDE",
        help="subset of A,B,C,D,E (default all five)",
    )
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--max-seconds", type=float, default=180.0)
    parser.add_argument(
        "--database-url",
        default=None,
        help="business data source; defaults to the configured application database URL",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "phase5_acceptance_results.json"),
        help="JSON output path",
    )
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    settings = get_settings()
    issues = settings.llm_endpoint_issues()
    if not settings.llm_enabled or issues:
        print("LLM is not configured for a live run:", issues or ["missing API key"])
        return 2
    settings = settings.model_copy(
        update={
            "agent_max_tool_calls": args.max_tool_calls,
            "agent_max_seconds": args.max_seconds,
            "phase5_enabled": True,
            "phase5_narrative_enabled": True,
            "phase5_max_fix_attempts": 1,
        }
    )
    llm = build_llm_client(settings)
    assert llm is not None
    print(f"provider: {settings.llm_provider} | model: {settings.llm_model}")
    print("api key: loaded from environment (not displayed)")

    business_engine = make_readonly_engine(
        args.database_url or settings.application_database_url,
        settings.query_timeout_ms,
    )
    factory = sessionmaker(bind=business_engine, class_=Session, expire_on_commit=False)
    run_engine = create_engine(
        f"sqlite:///{ROOT / 'phase5_acceptance_runs.db'}", future=True
    )
    pipeline = Phase5Pipeline(
        run_engine,
        llm=llm,
        narrative_enabled=True,
        max_fix_attempts=settings.phase5_max_fix_attempts,
    )
    runtime = AgentRuntime(
        llm_client=llm,
        registry=ToolRegistry(
            AnalysisGateway(factory, settings),
            AuthorizationPolicy(settings),
            settings,
        ),
        settings=settings,
        pipeline=pipeline,
    )
    principal = Principal(
        "key:acceptance", "analyst", frozenset({"metrics:read", "agent:run"})
    )

    results = []
    exit_code = 0
    for key in args.scenarios:
        if key not in SCENARIOS:
            print(f"unknown scenario {key!r}", file=sys.stderr)
            return 2
        question = SCENARIOS[key]
        print(f"\n=== Scenario {key} ===")
        print(f"question: {question}")
        started = time.monotonic()
        result = runtime.run(question, principal, run_id=f"p5-{key}-{uuid.uuid4().hex[:10]}")
        elapsed = round((time.monotonic() - started) * 1000, 1)
        payload = _summary(result, pipeline)
        payload["scenario"] = key
        payload["question"] = question
        payload["wall_ms"] = elapsed
        if key == "E":
            text = (result.final_answer or "") + " " + (
                payload["report"]["executive_summary"] if payload["report"] else ""
            )
            payload["prediction_refusal_signal"] = any(
                signal in text for signal in REFUSAL_SIGNALS
            )
            if not payload["prediction_refusal_signal"]:
                exit_code = 1
        results.append(payload)
        report = payload["report"]
        print(
            f"status={payload['status']} tools={len(payload['tool_calls'])} "
            f"evidence={len(payload['evidence'])} findings={len(payload['findings'])} "
            f"report={report['status'] if report else None} "
            f"validation={report['validation_passed'] if report else None} "
            f"narrative={report['narrative_mode'] if report else None} "
            f"tokens={payload['llm']['tokens']} latency_ms={payload['llm']['latency_ms']} "
            f"wall_ms={elapsed}"
        )
        if report and not report["validation_passed"]:
            for issue in report["issues"][:3]:
                print(f"  validator: {issue['code']} | {issue['message']}")

    output = {
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "tool_call_limit": args.max_tool_calls,
        "runs": results,
    }
    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {args.output}")
    business_engine.dispose()
    run_engine.dispose()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
