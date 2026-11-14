from __future__ import annotations

import json
import re
import time
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import (
    AgentRuntime,
    AgentStatus,
    AnalysisGateway,
    ToolDefinition,
    ToolRegistry,
)
from enterprise_business_agent.agent.graph import AGENT_SYSTEM_PROMPT, _failure_category
from enterprise_business_agent.agent.tools import (
    ComparisonArgs,
    MetricQueryArgs,
    ToolErrorInfo,
    ToolResult,
    build_default_tools,
)
from enterprise_business_agent.analysis.contracts import TimeGrain
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.llm import (
    LLMInvalidResponse,
    LLMTimeout,
    LLMToolCall,
    LLMToolResponse,
)
from enterprise_business_agent.security import AuthorizationPolicy, Principal
from tests.agent_fakes import (
    ScriptedLLM,
    finish_response,
    tool_response,
    truncated_response,
)

ANALYST = Principal("key:test", "analyst", frozenset({"metrics:read", "agent:run"}))


@pytest.fixture()
def agent_env(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        agent_max_tool_calls=4,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        agent_tool_max_retries=0,
        agent_max_consecutive_failures=2,
    )
    path = tmp_path / "agent.db"
    write_engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    read_engine = make_readonly_engine(f"sqlite:///{path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=read_engine, class_=Session, expire_on_commit=False)
    try:
        yield SimpleNamespace(
            settings=settings,
            path=path,
            write_engine=write_engine,
            read_engine=read_engine,
            factory=factory,
            authorization=AuthorizationPolicy(settings),
        )
    finally:
        read_engine.dispose()
        write_engine.dispose()


def _runtime(
    env,
    llm,
    *,
    settings: Settings | None = None,
    principal_policy: AuthorizationPolicy | None = None,
    definitions: list[ToolDefinition] | None = None,
) -> AgentRuntime:
    settings = settings or env.settings
    registry = ToolRegistry(
        AnalysisGateway(env.factory, settings),
        principal_policy or env.authorization,
        settings,
        definitions=definitions,
    )
    return AgentRuntime(llm_client=llm, registry=registry, settings=settings)


def _tool_messages(call: dict):
    return [item for item in call["messages"] if item["role"] == "tool"]


def test_multi_step_investigation_uses_tool_result_to_choose_next_step(agent_env):
    def decide(llm: ScriptedLLM):
        observations = _tool_messages(llm.calls[-1])
        if not observations:
            return tool_response(
                "c1",
                "top_n",
                {
                    "metric": "sales_amount",
                    "dimension": "region",
                    "start": "2025-03-01",
                    "end": "2025-03-01",
                    "n": 3,
                },
            )
        if len(observations) == 1:
            summary = json.loads(observations[0]["content"])["summary"]
            region = re.search(r"([A-Z]{2})=", summary).group(1)
            return tool_response(
                "c2",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-03-01",
                    "end": "2025-03-01",
                    "dimensions": [],
                    "filters": [{"dimension": "region", "operator": "eq", "value": region}],
                },
            )
        return finish_response("已完成区域排名，并按排名第一的区域查询了销售额。")

    llm = ScriptedLLM([decide, decide, decide])
    result = _runtime(agent_env, llm).run("分析2025年3月区域销售表现", ANALYST)

    assert result.status == AgentStatus.COMPLETED
    assert result.planner == "langgraph"
    assert result.planner_model == "fake-scripted-model"
    assert [item.tool for item in result.tool_calls] == ["top_n", "query_metric"]
    assert len(result.steps) == 3
    assert result.state.tool_calls_used == 2
    assert result.state.remaining_budget == 2
    assert len(result.evidence) == 2
    assert all(item.traceable for item in result.evidence)
    assert all(item.source == "sales_fact" for item in result.evidence)

    second_arguments = result.tool_calls[1].arguments
    first_summary = result.tool_calls[0].summary
    chosen_region = re.search(r"([A-Z]{2})=", first_summary).group(1)
    assert second_arguments["filters"][0]["value"] == chosen_region

    # The second decision saw the first tool result, not a fixed full plan.
    second_prompt = llm.calls[1]["messages"][-1]["content"]
    assert "已验证发现" in second_prompt
    assert "top_n" in second_prompt


def test_comparison_and_contribution_evidence_records_baseline(agent_env):
    registry = ToolRegistry(
        AnalysisGateway(agent_env.factory, agent_env.settings),
        agent_env.authorization,
        agent_env.settings,
    )
    comparison = registry.execute(
        "compare_periods",
        {
            "metric": "sales_amount",
            "kind": "mom",
            "period": "2025-12-01",
        },
        ANALYST,
        call_id="c1",
    )
    assert comparison.status == "ok"
    comparison_evidence = comparison.evidence[0]
    assert comparison_evidence.traceable is True
    assert comparison_evidence.baseline_period == "2025-11"
    assert comparison_evidence.baseline_time_range_start == "2025-11-01"
    assert comparison_evidence.baseline_time_range_end == "2025-11-01"
    assert comparison_evidence.missing_fields == []

    contribution = registry.execute(
        "contribution",
        {
            "metric": "sales_amount",
            "dimension": "product",
            "current_start": "2025-12-01",
            "current_end": "2025-12-01",
            "baseline_start": "2025-11-01",
            "baseline_end": "2025-11-01",
        },
        ANALYST,
        call_id="c2",
    )
    assert contribution.status == "ok"
    contribution_evidence = contribution.evidence[0]
    assert contribution_evidence.traceable is True
    assert contribution_evidence.baseline_period == "2025-11..2025-11"
    assert contribution_evidence.baseline_time_range_start == "2025-11-01"
    assert contribution_evidence.baseline_time_range_end == "2025-11-01"
    assert contribution_evidence.missing_fields == []


def test_contribution_rejects_overlapping_baseline(agent_env):
    registry = ToolRegistry(
        AnalysisGateway(agent_env.factory, agent_env.settings),
        agent_env.authorization,
        agent_env.settings,
    )
    result = registry.execute(
        "contribution",
        {
            "metric": "sales_amount",
            "dimension": "product",
            "current_start": "2025-12-01",
            "current_end": "2025-12-01",
            "baseline_start": "2025-11-01",
            "baseline_end": "2025-12-01",
        },
        ANALYST,
        call_id="c-overlap",
    )
    assert result.status == "invalid_arguments"
    assert "重叠" in json.dumps(result.data, ensure_ascii=False)


def test_model_can_finish_early_without_evidence_and_is_marked(agent_env):
    llm = ScriptedLLM([finish_response("我认为不需要查询。")])
    result = _runtime(agent_env, llm).run("随便看看", ANALYST)

    assert result.status == AgentStatus.NO_EVIDENCE
    assert result.stop_reason == "model_finished"
    assert result.tool_calls == []
    assert result.evidence == []
    assert result.open_questions


def test_budget_exhaustion_does_not_execute_extra_tool(agent_env):
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                },
            ),
            tool_response(
                "c2",
                "query_metric",
                {
                    "metric": "net_profit",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                },
            ),
        ]
    )
    result = _runtime(agent_env, llm).run("预算测试", ANALYST, max_tool_calls=1)

    assert result.status == AgentStatus.BUDGET_EXHAUSTED
    assert result.stop_reason == "tool_budget_exhausted"
    assert result.state.failure_category == "tool_budget_exhausted"
    assert result.state.tool_calls_used == 1
    assert len(result.tool_calls) == 1


def test_repeated_identical_tool_call_is_stopped(agent_env):
    call = {
        "metric": "sales_amount",
        "start": "2025-01-01",
        "end": "2025-01-01",
    }
    llm = ScriptedLLM(
        [
            tool_response("c1", "query_metric", call),
            tool_response("c2", "query_metric", call),
        ]
    )
    result = _runtime(agent_env, llm).run("重复调用测试", ANALYST)

    assert result.status == AgentStatus.REPEATED_TOOL_CALL
    assert result.state.tool_calls_used == 1
    assert result.errors[-1]["code"] == "repeated_tool_call"


def test_invalid_tool_arguments_are_fed_back_and_recovered(agent_env):
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "top_n",
                {
                    "metric": "sales_amount",
                    "dimension": "region",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                    "n": 999,
                },
            ),
            tool_response(
                "c2",
                "top_n",
                {
                    "metric": "sales_amount",
                    "dimension": "region",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                    "n": 5,
                },
            ),
            finish_response("已按修正后的参数完成 Top 5 查询。"),
        ]
    )
    result = _runtime(agent_env, llm).run("非法参数测试", ANALYST)

    assert result.status in {
        AgentStatus.COMPLETED,
        AgentStatus.COMPLETED_WITH_WARNINGS,
    }
    assert result.tool_calls[0].status == "invalid_arguments"
    assert result.tool_calls[1].status == "ok"
    assert any("参数" in item for item in result.warnings)
    # The recovered turn must still see the validation errors as data.
    assert any(
        message.get("role") == "tool" and "invalid_arguments" in message.get("content", "")
        for message in llm.calls[1]["messages"]
    )


def test_repeated_invalid_arguments_stop_at_failure_circuit_breaker(agent_env):
    bad = {
        "metric": "sales_amount",
        "dimension": "region",
        "start": "2025-01-01",
        "end": "2025-01-01",
        "n": 999,
    }
    llm = ScriptedLLM(
        [
            tool_response("c1", "top_n", bad),
            tool_response("c2", "top_n", {**bad, "n": 0}),
        ]
    )
    result = _runtime(agent_env, llm).run("连续非法参数测试", ANALYST)

    assert result.status == AgentStatus.TOOL_FAILURE
    assert result.stop_reason == "consecutive_tool_failures"
    assert result.evidence == []
    assert [item.status for item in result.tool_calls] == [
        "invalid_arguments",
        "invalid_arguments",
    ]


def test_metric_authorization_blocks_tool_before_execution(agent_env):
    settings = agent_env.settings.model_copy(
        update={
            "authz_policy": {
                "restricted_analyst": {
                    "metrics": ["sales_amount"],
                    "dimensions": ["region"],
                    "regions": ["EC"],
                }
            }
        }
    )
    restricted = Principal(
        "key:restricted",
        "restricted_analyst",
        frozenset({"metrics:read", "agent:run"}),
    )
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": "net_profit",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                },
            )
        ]
    )
    result = _runtime(
        agent_env,
        llm,
        settings=settings,
        principal_policy=AuthorizationPolicy(settings),
    ).run("查询净利润", restricted)

    assert result.status == AgentStatus.INVALID_ACTION
    assert result.stop_reason == "permission_denied"
    assert result.evidence == []
    assert result.tool_calls[0].status == "permission_denied"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "top_n",
            {
                "metric": "sales_amount",
                "dimension": "product",
                "start": "2025-01-01",
                "end": "2025-01-01",
            },
        ),
        (
            "contribution",
            {
                "metric": "sales_amount",
                "dimension": "product",
                "current_start": "2025-01-01",
                "current_end": "2025-01-01",
                "baseline_start": "2024-12-01",
                "baseline_end": "2024-12-01",
            },
        ),
    ],
)
def test_singular_dimension_is_authorized_before_execution(agent_env, tool, arguments):
    settings = agent_env.settings.model_copy(
        update={
            "authz_policy": {
                "restricted_analyst": {
                    "metrics": ["sales_amount"],
                    "dimensions": ["region"],
                    "regions": ["EC"],
                }
            }
        }
    )
    restricted = Principal(
        "key:restricted",
        "restricted_analyst",
        frozenset({"metrics:read", "agent:run"}),
    )
    registry = ToolRegistry(
        AnalysisGateway(agent_env.factory, settings),
        AuthorizationPolicy(settings),
        settings,
    )
    result = registry.execute(tool, arguments, restricted, call_id="c1")

    assert result.status == "permission_denied"
    assert result.error is not None
    assert "product" in result.error.message
    assert result.evidence == []


def test_llm_unavailable_returns_structured_state(agent_env):
    result = AgentRuntime(
        llm_client=None,
        registry=ToolRegistry(
            AnalysisGateway(agent_env.factory, agent_env.settings),
            agent_env.authorization,
            agent_env.settings,
        ),
        settings=agent_env.settings,
    ).run("没有模型", ANALYST)

    assert result.status == AgentStatus.LLM_UNAVAILABLE
    assert result.stop_reason == "llm_not_configured"
    assert result.final_answer is None
    assert result.errors[0]["code"] == "llm_unavailable"


def test_metric_catalog_answer_without_data_evidence_is_completed(agent_env):
    llm = ScriptedLLM(
        [
            tool_response("c1", "list_metrics", {}),
            finish_response("可用指标包括销售额、销量和平均销售单价。"),
        ]
    )
    result = _runtime(agent_env, llm).run("系统里有哪些指标？", ANALYST)

    assert result.status in {
        AgentStatus.COMPLETED,
        AgentStatus.COMPLETED_WITH_WARNINGS,
    }
    assert result.evidence == []
    assert result.final_answer


def test_empty_model_response_is_retried_once_then_recovers(agent_env):
    llm = ScriptedLLM(
        [
            LLMInvalidResponse("GLM response contained no text or tool call"),
            finish_response("本次没有查询到数据，无法给出结论。"),
        ]
    )
    events: list[dict] = []
    result = _runtime(agent_env, llm).run("模型空响应", ANALYST, trace_sink=events.append)

    assert len(llm.calls) == 2
    assert result.status == AgentStatus.NO_EVIDENCE
    assert result.final_answer == "本次没有查询到数据，无法给出结论。"
    # The retry carries an explicit continuation instruction.
    assert "上一轮没有返回" in llm.calls[1]["messages"][-1]["content"]
    assert [event["event_type"] for event in events].count("llm_retry") == 1


def test_empty_model_response_stops_after_one_retry(agent_env):
    llm = ScriptedLLM(
        [
            LLMInvalidResponse("GLM response contained no text or tool call"),
            LLMInvalidResponse("GLM response contained no text or tool call"),
        ]
    )
    result = _runtime(agent_env, llm).run("模型持续空响应", ANALYST)

    assert len(llm.calls) == 2
    assert result.status == AgentStatus.INVALID_ACTION
    assert result.stop_reason == "llm_invalid_response"


def test_multiple_tool_calls_in_one_turn_run_only_the_first(agent_env):
    two_calls = LLMToolResponse(
        text="",
        tool_calls=[
            LLMToolCall(
                call_id="c1",
                name="query_metric",
                arguments={
                    "metric": "sales_amount",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                },
            ),
            LLMToolCall(
                call_id="c2",
                name="top_n",
                arguments={
                    "metric": "sales_amount",
                    "dimension": "region",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                    "n": 4,
                },
            ),
        ],
        stop_reason="tool_use",
        model="fake-scripted-model",
    )
    llm = ScriptedLLM([two_calls, finish_response("本次查询完成，未做补充计算。")])
    result = _runtime(agent_env, llm).run("帮我看一下销售额", ANALYST)

    assert [item.tool for item in result.tool_calls] == ["query_metric"]
    assert result.tool_calls[0].call_id == "c1"
    assert result.status == AgentStatus.COMPLETED_WITH_WARNINGS
    assert any("只执行第一个" in item for item in result.warnings)


def test_llm_timeout_returns_structured_state(agent_env):
    llm = ScriptedLLM([LLMTimeout("provider timed out"), LLMTimeout("provider timed out again")])
    result = _runtime(agent_env, llm).run("模型超时", ANALYST)

    # The status stays a terminal LLM timeout; only the answer becomes a
    # bounded, explicitly incomplete statement instead of None.
    assert result.status == AgentStatus.LLM_TIMEOUT
    assert result.stop_reason == "llm_timeout"
    assert result.state.failure_category == "model_timeout"
    assert result.final_answer is not None
    assert "分析未完成" in result.final_answer
    assert "不可作为完整决策依据" in result.final_answer
    assert any("模型调用超时" in item for item in result.warnings)
    # One bounded retry, then a terminal structured state.
    assert len(llm.calls) == 2


def test_failure_category_separates_model_timeout_from_its_budget_variant():
    assert _failure_category(AgentStatus.LLM_TIMEOUT, {"budget_stop_reason": "llm_request_timeout"}) == "model_timeout"
    assert (
        _failure_category(
            AgentStatus.LLM_TIMEOUT,
            {"budget_stop_reason": "llm_request_budget_exhausted"},
        )
        == "model_timeout_budget"
    )
    assert (
        _failure_category(AgentStatus.TIMEOUT, {"budget_stop_reason": "insufficient_remaining_time"})
        == "case_time_budget"
    )


def test_llm_timeout_retry_carries_recovery_feedback(agent_env):
    llm = ScriptedLLM(
        [
            LLMTimeout("provider timed out"),
            finish_response("基于已有证据，本次没有可用的数据结论。"),
        ]
    )
    result = _runtime(agent_env, llm).run("模型超时后恢复", ANALYST)

    assert len(llm.calls) == 2
    retry_messages = llm.calls[1]["messages"]
    assert any("上一次模型请求超时" in str(item.get("content") or "") for item in retry_messages)
    assert result.final_answer == "基于已有证据，本次没有可用的数据结论。"


def test_exhausted_provider_retries_are_not_retried_again(agent_env):
    timeout = LLMTimeout("provider timed out")
    timeout.attempts = agent_env.settings.llm_max_retries + 1
    llm = ScriptedLLM([timeout, finish_response("不应到达")])

    result = _runtime(agent_env, llm).run("模型超时后不应重复计费", ANALYST)

    assert len(llm.calls) == 1
    assert result.status == AgentStatus.LLM_TIMEOUT
    assert result.final_answer is not None


def test_total_time_budget_stops_before_model_call(agent_env):
    llm = ScriptedLLM([finish_response("不应到达")])
    result = _runtime(agent_env, llm).run("时间预算", ANALYST, max_seconds=0.000001)

    assert result.status == AgentStatus.TIMEOUT
    assert result.stop_reason == "time_budget_exhausted"
    assert result.state.failure_category == "case_deadline"
    assert llm.calls == []


def test_tool_timeout_is_bounded_and_reported(agent_env):
    def slow(gateway, args):
        time.sleep(0.2)
        raise AssertionError("timed out tool must not complete")

    definitions = [
        ToolDefinition(
            name="query_metric",
            description="slow test tool",
            args_model=MetricQueryArgs,
            handler=slow,
        )
    ]
    settings = agent_env.settings.model_copy(
        update={
            "agent_tool_timeout_seconds": 0.01,
            "agent_tool_max_retries": 0,
            "agent_max_consecutive_failures": 1,
        }
    )
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                },
            )
        ]
    )
    result = _runtime(agent_env, llm, settings=settings, definitions=definitions).run("工具超时", ANALYST)

    assert result.status == AgentStatus.TOOL_FAILURE
    assert result.tool_calls[0].status == "timeout"
    assert result.errors[-1]["code"] == "tool_timeout"
    assert result.state.failure_category == "tool_timeout"


def test_tool_exception_text_is_not_exposed(agent_env):
    def boom(gateway, args):
        raise RuntimeError("password=super-secret db detail")

    registry = ToolRegistry(
        AnalysisGateway(agent_env.factory, agent_env.settings),
        agent_env.authorization,
        agent_env.settings,
        definitions=[
            ToolDefinition(
                name="query_metric",
                description="failing test tool",
                args_model=MetricQueryArgs,
                handler=boom,
            )
        ],
    )
    result = registry.execute(
        "query_metric",
        {
            "metric": "sales_amount",
            "start": "2025-01-01",
            "end": "2025-01-01",
        },
        ANALYST,
        call_id="c1",
    )
    assert result.status == "execution_error"
    assert result.error is not None
    assert "super-secret" not in result.error.message
    assert "RuntimeError" in result.error.message


def test_no_matching_data_is_not_reported_as_success(agent_env):
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                    "filters": [{"dimension": "region", "operator": "eq", "value": "ZZ"}],
                },
            ),
            finish_response("没有数据。"),
        ]
    )
    result = _runtime(agent_env, llm).run("查询不存在的区域", ANALYST)

    assert result.status == AgentStatus.NO_DATA
    assert result.tool_calls[0].status == "insufficient_data"
    assert result.verified_findings == []
    assert any("没有返回可用的数据结果" in item for item in result.warnings)


def test_sql_injection_metric_is_not_executed_and_table_survives(agent_env):
    registry = ToolRegistry(
        AnalysisGateway(agent_env.factory, agent_env.settings),
        agent_env.authorization,
        agent_env.settings,
    )
    result = registry.execute(
        "query_metric",
        {
            "metric": "sales_amount; DROP TABLE sales_fact",
            "start": "2025-01-01",
            "end": "2025-01-01",
        },
        ANALYST,
        call_id="c1",
    )
    assert result.status in {"unsupported", "insufficient_data"}
    with agent_env.read_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM sqlite_master WHERE name='sales_fact'")).scalar_one() == 1


def test_tool_schema_rejects_raw_sql_argument(agent_env):
    registry = ToolRegistry(
        AnalysisGateway(agent_env.factory, agent_env.settings),
        agent_env.authorization,
        agent_env.settings,
    )
    spec = next(item for item in registry.specs(ANALYST) if item["name"] == "query_metric")
    assert "sql" not in spec["input_schema"]["properties"]

    result = registry.execute(
        "query_metric",
        {
            "metric": "sales_amount",
            "start": "2025-01-01",
            "end": "2025-01-01",
            "sql": "DROP TABLE sales_fact",
        },
        ANALYST,
        call_id="c1",
    )
    assert result.status == "invalid_arguments"


def test_metric_summary_does_not_describe_dimensions_as_time_series():
    result = SimpleNamespace(
        label="营销 ROI",
        points=[
            SimpleNamespace(period="2025-10", dimensions={"campaign": "C006"}, value="0.72"),
            SimpleNamespace(period="2025-11", dimensions={"campaign": "C007"}, value="1.75"),
        ],
    )
    summary = AnalysisGateway._metric_summary(result)
    assert "按 campaign 拆分" in summary
    assert "首条值 0.72" in summary
    assert "首值" not in summary


def test_metric_summary_single_point_is_not_a_range():
    result = SimpleNamespace(
        label="销售额",
        points=[SimpleNamespace(period="2025-10~2025-12", dimensions={}, value="300.00")],
    )
    assert AnalysisGateway._metric_summary(result) == "销售额在 2025-10~2025-12 为 300.00。"


def test_metric_summary_marks_null_values_as_not_calculable():
    result = SimpleNamespace(
        label="单位采购成本",
        points=[
            SimpleNamespace(period="2025-10~2025-12", dimensions={"product": "P004"}, value=None),
            SimpleNamespace(period="2025-10~2025-12", dimensions={"product": "P005"}, value="783.55"),
        ],
    )
    summary = AnalysisGateway._metric_summary(result)
    assert "首条值 不可计算" in summary
    assert "None" not in summary


def test_metric_summary_all_null_values_has_no_fake_number():
    result = SimpleNamespace(
        label="单位采购成本",
        points=[SimpleNamespace(period="2025-12", dimensions={"product": "P004"}, value=None)],
    )
    assert AnalysisGateway._metric_summary(result) == "单位采购成本返回 1 个数据点，但所有数值均不可计算。"


def test_truncated_final_answer_is_continued_once(agent_env):
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-03-01",
                    "end": "2025-03-01",
                    "dimensions": [],
                },
            ),
            truncated_response("2025年3月销售额为"),
            finish_response("约百万级，详见报告。"),
        ]
    )
    result = _runtime(agent_env, llm).run("查询2025年3月销售额", ANALYST)

    assert result.final_answer == "2025年3月销售额为约百万级，详见报告。"
    assert any("输出 token 上限" in item for item in result.warnings)
    continuation = llm.calls[-1]
    assert continuation["tools"] == []
    assert continuation["max_tokens"] == (agent_env.settings.agent_final_continuation_max_tokens)


def test_failed_continuation_keeps_partial_answer_and_warns(agent_env):
    from enterprise_business_agent.llm import LLMUnavailable

    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-03-01",
                    "end": "2025-03-01",
                    "dimensions": [],
                },
            ),
            truncated_response("2025年3月销售额为"),
            LLMUnavailable("provider down"),
        ]
    )
    result = _runtime(agent_env, llm).run("查询2025年3月销售额", ANALYST)

    assert result.final_answer == "2025年3月销售额为"
    assert any("续写失败" in item for item in result.warnings)
    assert result.status == AgentStatus.COMPLETED_WITH_WARNINGS


def test_llm_timeout_keeps_completed_evidence_in_a_partial_answer(agent_env):
    query = LLMToolResponse(
        text="",
        tool_calls=[
            LLMToolCall(
                call_id="c1",
                name="query_metric",
                arguments={
                    "metric": "sales_amount",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                },
            )
        ],
        stop_reason="tool_use",
        model="fake-scripted-model",
    )
    llm = ScriptedLLM([query, LLMTimeout("provider timed out"), LLMTimeout("provider timed out")])
    result = _runtime(agent_env, llm).run("先查销售额再总结", ANALYST)

    # The case still fails; the partial answer only reuses evidence that the
    # tool already returned.
    assert result.status == AgentStatus.LLM_TIMEOUT
    assert result.stop_reason == "llm_timeout"
    assert "分析未完成" in result.final_answer
    assert "已完成的工具证据" in result.final_answer
    assert "query_metric:" in result.final_answer
    assert "不可作为完整决策依据" in result.final_answer
    assert len(llm.calls) == 3


def test_insufficient_remaining_time_skips_a_doomed_model_call(agent_env):
    query = LLMToolResponse(
        text="",
        tool_calls=[
            LLMToolCall(
                call_id="c1",
                name="query_metric",
                arguments={
                    "metric": "sales_amount",
                    "start": "2025-01-01",
                    "end": "2025-01-01",
                },
            )
        ],
        stop_reason="tool_use",
        model="fake-scripted-model",
    )
    llm = ScriptedLLM([query, finish_response("不应到达")])
    # The default per-run ceiling is 5s here, so after the first tool call the
    # remaining time can never fit a second decision.
    result = _runtime(agent_env, llm).run("时间预算", ANALYST, max_seconds=5.0)

    assert result.status == AgentStatus.TIMEOUT
    assert result.stop_reason == "time_budget_exhausted"
    assert len(llm.calls) == 1
    assert any("剩余时间不足" in item for item in result.warnings)
    # The evidence the first call returned is kept, and the status still fails.
    assert "分析未完成" in result.final_answer
    assert "已完成的工具证据" in result.final_answer
    assert "query_metric:" in result.final_answer


def _sales_by_region(call_id: str, start: str = "2025-03-01", end: str = "2025-03-01") -> LLMToolResponse:
    return tool_response(
        call_id,
        "query_metric",
        {
            "metric": "sales_amount",
            "start": start,
            "end": end,
            "dimensions": ["region"],
            "grain": "total",
        },
    )


def _sales_top_n(
    call_id: str,
    *,
    start: str = "2025-03-01",
    end: str = "2025-03-01",
    n: int = 4,
    filters: list[dict] | None = None,
) -> LLMToolResponse:
    arguments = {
        "metric": "sales_amount",
        "dimension": "region",
        "start": start,
        "end": end,
        "n": n,
    }
    if filters is not None:
        arguments["filters"] = filters
    return tool_response(call_id, "top_n", arguments)


def test_semantic_duplicate_dimension_query_blocks_the_followup_top_n(agent_env):
    llm = ScriptedLLM(
        [
            _sales_by_region("c1"),
            _sales_top_n("c2"),
            finish_response("2025年3月各区域销售额已完成。"),
        ]
    )
    result = _runtime(agent_env, llm).run("2025年3月各区域销售额", ANALYST)

    assert result.status == AgentStatus.COMPLETED_WITH_WARNINGS
    # The redundant top_n never ran: one tool call, one recorded block.
    assert len(result.tool_calls) == 1
    assert result.state.tool_calls_used == 1
    assert len(result.blocked_tool_calls) == 1
    blocked = result.blocked_tool_calls[0]
    assert blocked.status == "duplicate_suppressed"
    assert blocked.error_code == "semantic_duplicate"
    assert result.state.duplicate_tool_calls == 1
    blocked_steps = [step for step in result.steps if step.action == "call_tool_blocked"]
    assert len(blocked_steps) == 1
    assert blocked_steps[0].blocked_reason
    assert blocked_steps[0].remaining_seconds is not None
    assert blocked_steps[0].remaining_tool_calls == 3
    assert blocked_steps[0].retry_count == 1
    # The model was handed the existing result instead of a fresh execution.
    feedback = json.loads(_tool_messages(llm.calls[-1])[-1]["content"])
    assert feedback["status"] == "duplicate_suppressed"
    assert feedback["existing_tool"] == "query_metric"
    assert feedback["existing_summary"]
    assert feedback["evidence_ids"]


def test_semantic_duplicate_allows_a_different_time_range(agent_env):
    llm = ScriptedLLM(
        [
            _sales_by_region("c1"),
            _sales_top_n("c2", start="2025-04-01", end="2025-04-01"),
            finish_response("两个月份的区域销售额都已查询。"),
        ]
    )
    result = _runtime(agent_env, llm).run("比较两个月的区域销售额", ANALYST)

    assert result.status == AgentStatus.COMPLETED
    assert [item.tool for item in result.tool_calls] == ["query_metric", "top_n"]
    assert result.blocked_tool_calls == []


def test_semantic_duplicate_allows_a_different_grain(agent_env):
    monthly = tool_response(
        "c1",
        "query_metric",
        {
            "metric": "sales_amount",
            "start": "2025-01-01",
            "end": "2025-03-01",
            "dimensions": ["region"],
            "grain": "month",
        },
    )
    aggregate = _sales_top_n("c2", start="2025-01-01", end="2025-03-01")
    llm = ScriptedLLM([monthly, aggregate, finish_response("月度序列和区间排名都已查询。")])
    result = _runtime(agent_env, llm).run("区域销售额月度序列与区间排名", ANALYST)

    assert result.status == AgentStatus.COMPLETED
    assert [item.tool for item in result.tool_calls] == ["query_metric", "top_n"]
    assert result.blocked_tool_calls == []


def test_top_n_full_page_is_not_treated_as_a_complete_member_list(agent_env):
    # n=3 returns exactly 3 of 4 regions: the runtime cannot prove the
    # dimension was fully enumerated, so the wider query must still be allowed.
    llm = ScriptedLLM(
        [
            _sales_top_n("c1", n=3),
            _sales_by_region("c2"),
            finish_response("已补充完整区域列表。"),
        ]
    )
    result = _runtime(agent_env, llm).run("区域销售额与完整列表", ANALYST)

    assert result.status == AgentStatus.COMPLETED
    assert [item.tool for item in result.tool_calls] == ["top_n", "query_metric"]
    assert result.blocked_tool_calls == []


def test_top_n_short_page_fills_a_later_full_dimension_query(agent_env):
    # n=5 returns 4 regions, proving every member was already returned.
    llm = ScriptedLLM(
        [
            _sales_top_n("c1", n=5),
            _sales_by_region("c2"),
            finish_response("完整列表已在排名结果中。"),
        ]
    )
    result = _runtime(agent_env, llm).run("区域销售额完整列表", ANALYST)

    assert result.status == AgentStatus.COMPLETED_WITH_WARNINGS
    assert [item.tool for item in result.tool_calls] == ["top_n"]
    assert len(result.blocked_tool_calls) == 1


def test_semantic_duplicate_allows_a_different_filter(agent_env):
    filtered = tool_response(
        "c1",
        "query_metric",
        {
            "metric": "sales_amount",
            "start": "2025-03-01",
            "end": "2025-03-01",
            "dimensions": ["product"],
            "grain": "total",
            "filters": [{"dimension": "region", "operator": "eq", "value": "NC"}],
        },
    )
    other_region = tool_response(
        "c2",
        "top_n",
        {
            "metric": "sales_amount",
            "dimension": "product",
            "start": "2025-03-01",
            "end": "2025-03-01",
            "n": 8,
            "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
        },
    )
    llm = ScriptedLLM([filtered, other_region, finish_response("两个区域的排名都已查询。")])
    result = _runtime(agent_env, llm).run("华北与华东的产品销售额", ANALYST)

    assert result.status == AgentStatus.COMPLETED
    assert [item.tool for item in result.tool_calls] == ["query_metric", "top_n"]
    assert result.blocked_tool_calls == []


def test_semantic_duplicate_matches_filters_regardless_of_order(agent_env):
    filters = [
        {"dimension": "region", "operator": "eq", "value": "NC"},
        {"dimension": "channel", "operator": "eq", "value": "ECOM"},
    ]
    query = tool_response(
        "c1",
        "query_metric",
        {
            "metric": "sales_amount",
            "start": "2025-03-01",
            "end": "2025-03-01",
            "dimensions": ["product"],
            "grain": "total",
            "filters": filters,
        },
    )
    duplicate = tool_response(
        "c2",
        "top_n",
        {
            "metric": "sales_amount",
            "dimension": "product",
            "start": "2025-03-01",
            "end": "2025-03-01",
            "n": 8,
            "filters": list(reversed(filters)),
        },
    )
    llm = ScriptedLLM([query, duplicate, finish_response("产品销售额已从已有结果获取。")])
    result = _runtime(agent_env, llm).run("华北电商渠道的产品销售额", ANALYST)

    assert result.status == AgentStatus.COMPLETED_WITH_WARNINGS
    assert [item.tool for item in result.tool_calls] == ["query_metric"]
    assert len(result.blocked_tool_calls) == 1


def test_duplicate_blocks_stop_after_the_configured_limit(agent_env):
    llm = ScriptedLLM(
        [
            _sales_by_region("c1"),
            _sales_top_n("c2", n=4),
            _sales_top_n("c3", n=5),
            _sales_top_n("c4", n=6),
        ]
    )
    result = _runtime(agent_env, llm).run("反复请求已覆盖的排名", ANALYST)

    assert result.status == AgentStatus.REPEATED_TOOL_CALL
    assert result.stop_reason == "repeated_tool_call"
    assert len(result.blocked_tool_calls) == 3
    assert result.state.tool_calls_used == 1
    assert any("停止运行" in item["message"] for item in result.errors)


def test_retry_is_not_sent_when_the_remaining_budget_cannot_fit_the_last_call(
    agent_env,
):
    class SlowTimeoutLLM:
        model = "fake-slow-timeout"

        def __init__(self) -> None:
            self.calls = 0

        def complete_with_tools(self, **kwargs):
            self.calls += 1
            time.sleep(0.3)
            raise LLMTimeout("provider timed out")

    llm = SlowTimeoutLLM()
    result = _runtime(agent_env, llm).run("时间预算", ANALYST, max_seconds=1.0)

    # agent_llm_max_retries=1 would normally retry, but the observed call plus
    # the minimum request window no longer fit the remaining case budget.
    assert llm.calls == 1
    assert result.status == AgentStatus.LLM_TIMEOUT
    assert result.stop_reason == "llm_timeout"
    assert result.state.budget_stop_reason == "llm_request_timeout"


def test_fresh_decision_guard_uses_the_observed_call_duration(agent_env):
    slow = LLMToolResponse(
        text="",
        tool_calls=[
            LLMToolCall(
                call_id="c1",
                name="query_metric",
                arguments={
                    "metric": "sales_amount",
                    "start": "2025-03-01",
                    "end": "2025-03-01",
                    "dimensions": [],
                },
            )
        ],
        stop_reason="tool_use",
        model="fake-scripted-model",
        latency_ms=12_000.0,
    )
    llm = ScriptedLLM([slow, finish_response("不应到达")])
    result = _runtime(agent_env, llm).run("时间预算", ANALYST, max_seconds=10.0)

    # The last logical call took 12s; only ~10s remain, so the next call is
    # not started even though it would fit the old 5s floor.
    assert len(llm.calls) == 1
    assert result.status == AgentStatus.TIMEOUT
    assert result.stop_reason == "time_budget_exhausted"
    assert result.state.budget_stop_reason == "insufficient_remaining_time"
    assert any("预计至少 12.0s" in item for item in result.warnings)


# --- Phase 7.5: model-visible tool data and dedupe result reuse -------------


def _inventory_by_product(call_id: str, start: str = "2024-02-01", end: str = "2024-02-01") -> LLMToolResponse:
    return tool_response(
        call_id,
        "query_metric",
        {
            "metric": "inventory_days",
            "start": start,
            "end": end,
            "dimensions": ["product"],
            "grain": "total",
        },
    )


def _inventory_top_n(
    call_id: str,
    *,
    start: str = "2024-02-01",
    end: str = "2024-02-01",
    n: int = 8,
) -> LLMToolResponse:
    return tool_response(
        call_id,
        "top_n",
        {
            "metric": "inventory_days",
            "dimension": "product",
            "start": start,
            "end": end,
            "n": n,
        },
    )


def _monthly_sales(call_id: str, start: str, end: str) -> LLMToolResponse:
    return tool_response(
        call_id,
        "query_metric",
        {
            "metric": "sales_amount",
            "start": start,
            "end": end,
            "dimensions": [],
            "grain": "month",
        },
    )


def _tool_payload(call: dict, position: int) -> dict:
    return json.loads(_tool_messages(call)[position]["content"])


def test_deduped_top_n_returns_every_existing_product_value(agent_env):
    """8 products -> top_n(n=8) is deduped but still hands over all 8 rows."""

    llm = ScriptedLLM(
        [
            _inventory_by_product("c1"),
            _inventory_top_n("c2"),
            finish_response("8 个产品的库存天数排名已完成。"),
        ]
    )
    result = _runtime(agent_env, llm).run("8 个产品的库存天数排名", ANALYST)

    assert result.status == AgentStatus.COMPLETED_WITH_WARNINGS
    # One database query, one dedupe reuse, no second execution.
    assert len(result.tool_calls) == 1
    assert len(result.blocked_tool_calls) == 1
    assert result.state.tool_calls_used == 1
    assert result.state.duplicate_tool_calls == 1

    normal = _tool_payload(llm.calls[1], 0)
    assert normal["reused"] is False
    assert normal["data"]["row_count"] == 8
    assert normal["data"]["displayed_count"] == 8
    assert normal["data"]["truncated"] is False
    assert normal["data"]["source_table"] == "inventory_snapshot"
    assert normal["data"]["time_range"] == {
        "start": "2024-02-01",
        "end": "2024-02-01",
    }
    rows = normal["data"]["rows"]
    assert len(rows) == 8
    assert {row["dimensions"]["product"] for row in rows} == {f"P00{index}" for index in range(1, 9)}
    assert all(row["value"] is not None for row in rows)
    assert len({row["value"] for row in rows}) == 8

    feedback = _tool_payload(llm.calls[-1], 1)
    assert feedback["status"] == "duplicate_suppressed"
    assert feedback["reused"] is True
    assert feedback["source"] == "existing_tool_result"
    assert feedback["existing_tool"] == "query_metric"
    assert feedback["row_count"] == 8
    assert feedback["displayed_count"] == 8
    assert feedback["truncated"] is False
    assert feedback["data"]["rows"] == rows
    # Reuse points at the original evidence and never mints a new record.
    assert feedback["evidence_ids"] == normal["evidence_ids"]
    assert len(result.evidence) == 1
    assert [item.evidence_id for item in result.evidence] == normal["evidence_ids"]

    blocked = result.blocked_tool_calls[0]
    assert blocked.reused is True
    assert blocked.row_count == 8
    assert blocked.displayed_count == 8
    assert blocked.truncated is False


@pytest.mark.parametrize(
    ("start", "end", "metric", "dimensions", "expected"),
    [
        ("2025-01-01", "2025-05-01", "sales_amount", [], 5),
        ("2025-12-01", "2025-12-01", "inventory_days", ["product"], 8),
        ("2024-05-01", "2025-12-01", "sales_amount", [], 20),
        ("2024-01-01", "2025-12-01", "sales_amount", [], 24),
    ],
)
def test_model_visible_rows_cover_5_8_20_and_more(agent_env, start, end, metric, dimensions, expected):
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": metric,
                    "start": start,
                    "end": end,
                    "dimensions": dimensions,
                    "grain": "month" if not dimensions else "total",
                },
            ),
            finish_response("已读取明细。"),
        ]
    )
    result = _runtime(agent_env, llm).run("读取明细", ANALYST)

    assert result.status == AgentStatus.COMPLETED
    view = _tool_payload(llm.calls[1], 0)["data"]
    assert view["row_count"] == expected
    assert view["displayed_count"] == min(expected, 20)
    assert view["truncated"] is (expected > 20)
    assert view["remaining_count"] == max(0, expected - 20)
    assert len(view["rows"]) == min(expected, 20)
    if expected > 20:
        assert view["has_more"] is True
        assert str(expected) in view["next_step"]
    else:
        assert view["has_more"] is False
        assert view["next_step"] == "已展示当前结果的完整明细。"
    record = result.tool_calls[0]
    assert record.displayed_count == min(expected, 20)
    assert record.truncated is (expected > 20)


def test_truncated_query_metric_does_not_block_a_different_tool(agent_env):
    """A 24-row view cannot answer trend, so the follow-up must execute."""

    llm = ScriptedLLM(
        [
            _monthly_sales("c1", "2024-01-01", "2025-12-01"),
            tool_response(
                "c2",
                "trend",
                {
                    "metric": "sales_amount",
                    "start": "2024-01-01",
                    "end": "2025-12-01",
                    "grain": "month",
                },
            ),
            finish_response("趋势已补充。"),
        ]
    )
    result = _runtime(agent_env, llm).run("销售额趋势", ANALYST)

    assert result.status == AgentStatus.COMPLETED
    assert [item.tool for item in result.tool_calls] == ["query_metric", "trend"]
    assert result.blocked_tool_calls == []
    assert result.state.duplicate_tool_calls == 0


def test_duplicate_reuse_is_reauthorized_before_it_is_returned(agent_env):
    runtime = _runtime(agent_env, None)
    seen: list[str] = []

    def deny(_name, _arguments, _principal):
        seen.append(_name)
        return ToolResult(
            status="permission_denied",
            error=ToolErrorInfo(code="permission_denied", message="test deny"),
        )

    runtime.registry.authorize_call = deny
    llm = ScriptedLLM(
        [
            _inventory_by_product("c1"),
            _inventory_top_n("c2"),
            finish_response("已用新查询补足明细。"),
        ]
    )
    runtime.llm = llm
    result = runtime.run("8 个产品的库存天数排名", ANALYST)

    # The coverage result was re-authorized for the current principal; when
    # that fails the call is executed normally instead of being reused.
    assert seen == ["query_metric"]
    assert result.blocked_tool_calls == []
    assert [item.tool for item in result.tool_calls] == ["query_metric", "top_n"]


def test_yoy_year_rejects_a_quarter_argument():
    """A quarter-scoped question must not silently use annual YoY."""

    with pytest.raises(Exception, match="yoy_quarter"):
        ComparisonArgs(
            metric="labor_cost",
            kind="yoy_year",
            year=2025,
            quarter=4,
        )


def test_relation_violation_triggers_one_bounded_correction(agent_env):
    query = tool_response(
        "c1",
        "query_metric",
        {
            "metric": "inventory_days",
            "start": "2025-12-01",
            "end": "2025-12-01",
            "dimensions": ["product"],
            "grain": "total",
        },
    )
    wrong = finish_response("2025年12月各产品库存天数：P004 87.345507，P005 79.203518。库存天数最高的是 P004 和 P005。")
    corrected = finish_response(
        "2025年12月各产品库存天数：P004 87.345507，P005 79.203518。"
        "库存天数最高的是 P004（87.345507）；P005 是第二高，不能称为最高。"
    )
    llm = ScriptedLLM([query, wrong, corrected])

    result = _runtime(agent_env, llm).run("2025年12月各产品的库存天数是多少？", ANALYST)

    assert len(llm.calls) == 3
    assert "业务关系不一致" in llm.calls[2]["messages"][-1]["content"]
    assert "P005" in llm.calls[2]["messages"][-1]["content"]
    assert result.relation_issues == []
    assert "最高的是 P004（87.345507）" in (result.final_answer or "")


def test_relation_correction_is_attempted_only_once(agent_env):
    query = tool_response(
        "c1",
        "query_metric",
        {
            "metric": "inventory_days",
            "start": "2025-12-01",
            "end": "2025-12-01",
            "dimensions": ["product"],
            "grain": "total",
        },
    )
    wrong = finish_response("2025年12月各产品库存天数：P004 87.345507，P005 79.203518。库存天数最高的是 P004 和 P005。")
    still_wrong = finish_response(
        "2025年12月各产品库存天数：P004 87.345507，P005 79.203518。库存天数最高的是 P004 和 P005。"
    )
    llm = ScriptedLLM([query, wrong, still_wrong])

    result = _runtime(agent_env, llm).run("2025年12月各产品的库存天数是多少？", ANALYST)

    assert len(llm.calls) == 3
    assert result.relation_issues
    assert result.final_answer == wrong.text


def test_truncated_correction_keeps_the_complete_original_answer(agent_env):
    query = tool_response(
        "c1",
        "query_metric",
        {
            "metric": "inventory_days",
            "start": "2025-12-01",
            "end": "2025-12-01",
            "dimensions": ["product"],
            "grain": "total",
        },
    )
    wrong = finish_response("2025年12月各产品库存天数：P004 87.345507，P005 79.203518。库存天数最高的是 P004 和 P005。")
    llm = ScriptedLLM([query, wrong, truncated_response("逐成员核对工具返回数据（top")])

    result = _runtime(agent_env, llm).run("2025年12月各产品的库存天数是多少？", ANALYST)

    assert result.final_answer == wrong.text
    assert result.relation_issues
    assert any("纠错输出被截断" in warning for warning in result.warnings)


def test_unknown_product_returns_an_explicit_no_data_answer(agent_env):
    missing = {
        "metric": "sales_amount",
        "start": "2025-12-01",
        "end": "2025-12-01",
        "filters": [{"dimension": "product", "operator": "eq", "value": "P999"}],
        "grain": "total",
    }
    llm = ScriptedLLM(
        [
            tool_response("c1", "query_metric", missing),
            tool_response(
                "c2",
                "top_n",
                {
                    "metric": "sales_amount",
                    "dimension": "product",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                    "n": 5,
                    "filters": missing["filters"],
                },
            ),
        ]
    )

    result = _runtime(agent_env, llm).run("2025年12月产品P999的销售额是多少？", ANALYST)

    assert result.status == AgentStatus.TOOL_FAILURE
    answer = result.final_answer or ""
    assert answer
    assert any(term in answer for term in ("无法", "未找到", "没有", "不存在", "缺少"))
    assert "P999" in answer


def test_decision_prompt_exposes_recent_results_and_tool_guidance(agent_env):
    runtime = _runtime(agent_env, ScriptedLLM([]))
    state = {
        "goal": "找出第四季度ROI最低的三个活动",
        "tool_calls_used": 1,
        "verified_findings": ["anomaly_scan 返回 2 个异常月份"],
        "errors": [{"code": "insufficient_data", "message": "没有数据"}],
        "observations": [
            SimpleNamespace(
                tool="anomaly_scan",
                status="ok",
                row_count=34,
                displayed_count=20,
                truncated=True,
                summary="发现两个异常月份",
            )
        ],
        "open_questions": ["还需要按活动排名"],
    }

    prompt = runtime._decision_prompt(state, budget_left=3)

    assert "最近工具结果" in prompt
    assert "anomaly_scan [ok] 返回 34 行，展示 20 行（已截断）" in prompt
    assert "还需要按活动排名" in prompt
    assert "排名用 top_n" in prompt
    assert "趋势用 trend" in prompt
    assert "异常扫描用 anomaly_scan" in prompt
    assert "最终业务结论正确不等于工具选择满足任务要求" in prompt


def test_prompt_forbids_substituting_an_unknown_metric():
    assert "查询时必须使用用户点名的指标" in AGENT_SYSTEM_PROMPT
    assert "不得替换成销售额等其他已知指标" in AGENT_SYSTEM_PROMPT
    assert "先调用 list_metrics 确认" in AGENT_SYSTEM_PROMPT


def test_metric_query_defaults_to_monthly_grain():
    args = MetricQueryArgs(
        metric="marketing_spend",
        start=date(2025, 10, 1),
        end=date(2025, 12, 1),
        dimensions=["channel"],
    )

    assert args.grain == TimeGrain.MONTH


def test_tool_descriptions_steer_to_specialized_tools():
    descriptions = {definition.name: definition.description for definition in build_default_tools()}

    assert "保留 month 明细" in descriptions["query_metric"]
    assert "top_n" in descriptions["query_metric"]
    assert "trend" in descriptions["query_metric"]
    assert "anomaly_scan" in descriptions["query_metric"]
    assert "ascending=true" in descriptions["top_n"]
    assert "不要用 query_metric 代替" in descriptions["trend"]
    assert "method=zscore" in descriptions["anomaly_scan"]
    assert "yoy_quarter" in descriptions["compare_periods"]
