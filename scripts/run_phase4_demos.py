#!/usr/bin/env python
from __future__ import annotations

import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from enterprise_business_agent.agent import AgentRuntime, AnalysisGateway, ToolRegistry  # noqa: E402
from enterprise_business_agent.config import Settings  # noqa: E402
from enterprise_business_agent.data_generator import generate_data  # noqa: E402
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine  # noqa: E402
from enterprise_business_agent.llm import LLMToolCall, LLMToolResponse  # noqa: E402
from enterprise_business_agent.security import AuthorizationPolicy, Principal  # noqa: E402


class ToolResultDrivenDemoLLM:
    """TEST FAKE for the demo: chooses each step from prior tool results."""

    model = "fake-demo-tool-loop"

    def __init__(self):
        self.calls = 0

    def complete_with_tools(self, *, system, messages, tools, timeout_seconds: float | None = None):
        self.calls += 1
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        if not tool_messages:
            return self._call(
                "demo-1",
                "top_n",
                {
                    "metric": "sales_amount",
                    "dimension": "product",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                    "n": 3,
                    "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
                },
            )
        if len(tool_messages) == 1:
            return self._call(
                "demo-2",
                "compare_periods",
                {
                    "metric": "sales_amount",
                    "kind": "mom",
                    "period": "2025-12-01",
                    "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
                },
            )
        if len(tool_messages) == 2:
            summary = json.loads(tool_messages[0]["content"])["summary"]
            product = re.search(r"(P\d{3})=", summary).group(1)
            return self._call(
                "demo-3",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                    "filters": [
                        {"dimension": "region", "operator": "eq", "value": "EC"},
                        {"dimension": "product", "operator": "eq", "value": product},
                    ],
                },
            )
        summaries = [json.loads(item["content"]).get("summary") for item in tool_messages]
        return LLMToolResponse(
            text="根据工具结果：" + "；".join(value for value in summaries if value),
            tool_calls=[],
            stop_reason="end_turn",
            model=self.model,
        )

    @staticmethod
    def _call(call_id, name, arguments):
        return LLMToolResponse(
            text=f"下一步调用 {name}",
            tool_calls=[LLMToolCall(call_id=call_id, name=name, arguments=arguments)],
            stop_reason="tool_use",
            model=ToolResultDrivenDemoLLM.model,
        )


def main() -> int:
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        agent_max_tool_calls=4,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=5,
        agent_tool_max_retries=0,
    )
    principal = Principal("key:demo", "analyst", frozenset({"metrics:read", "agent:run"}))
    question = "华东2025年12月销售表现如何？哪些产品贡献最大？"
    print("planner: fake-demo-tool-loop (scripted test double, NOT a real LLM)")
    print(f"question: {question}")

    with tempfile.TemporaryDirectory(prefix="eba-agent-demo-") as tmp:
        path = Path(tmp) / "demo.db"
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
        runtime = AgentRuntime(
            llm_client=ToolResultDrivenDemoLLM(),
            registry=registry,
            settings=settings,
        )
        try:
            result = runtime.run(question, principal)
        finally:
            read_engine.dispose()
            write_engine.dispose()

    print(f"\nrun_id: {result.run_id}")
    print(f"status: {result.status.value}  stop_reason={result.stop_reason}")
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
            f"  {item.evidence_id} call={item.tool_call_id} metric={item.metric}"
            f" range={item.time_range_start}..{item.time_range_end}"
            f" baseline={item.baseline_period}"
            f" source={item.source} traceable={item.traceable}"
            f" missing={item.missing_fields}"
        )
    print("\nstate:")
    print(f"  verified_findings={result.verified_findings}")
    print(f"  open_questions={result.open_questions}")
    print(f"  errors={result.errors}")
    print(f"  tool_calls_used={result.state.tool_calls_used}/{result.state.max_tool_calls}")
    print(f"  remaining_budget={result.state.remaining_budget}")
    print(f"\nfinal_answer: {result.final_answer}")
    print(f"warnings: {result.warnings}")
    return 0 if result.status.value == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
