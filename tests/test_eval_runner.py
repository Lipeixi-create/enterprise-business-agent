"""Offline validation of the Phase 7 corpus and multi-dimensional evaluator.

No test here calls a real model. The scoring tests use scripted planners and
only assert evaluator behavior, never model quality.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import AgentRuntime, AnalysisGateway, ToolRegistry
from enterprise_business_agent.agent.contracts import EvidenceDraft
from enterprise_business_agent.agent.evaluation import (
    LiveEvalCase,
    PlannedCall,
    ScriptedPlanLLM,
    _best_plan,
    _canonical_check,
    _evidence_chain_issues,
    _number_matches,
    _truncated_warning,
    _unsourced_numbers,
    load_live_agent_cases,
    run_live_agent_case,
    summarize_live_results,
)
from enterprise_business_agent.agent.graph import _matches_derived
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.llm import LLMTimeout, LLMToolCall, LLMToolResponse
from enterprise_business_agent.security import AuthorizationPolicy, Principal

ROOT = Path(__file__).resolve().parents[1]
PHASE7 = ROOT / "tests" / "evals" / "phase7_cases.jsonl"
BASE = ROOT / "tests" / "evals" / "live_agent_cases.jsonl"
PRINCIPAL = Principal("key:eval", "analyst", frozenset({"metrics:read", "agent:run"}))


@pytest.fixture(scope="module")
def eval_env(tmp_path_factory):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        agent_max_tool_calls=6,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        agent_tool_max_retries=0,
    )
    path = tmp_path_factory.mktemp("phase7-eval") / "eval.db"
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
    try:
        yield settings, registry
    finally:
        read_engine.dispose()
        write_engine.dispose()


class _RefusingPlannerLLM:
    """Calls the planned tools once, then refuses with an explanation."""

    model = "fake-refusal-llm"

    def __init__(self, plan=()):
        self.plan = list(plan)
        self.index = 0

    def complete_with_tools(self, **kwargs):
        if self.index < len(self.plan):
            call = self.plan[self.index]
            self.index += 1
            return LLMToolResponse(
                text="执行查询",
                tool_calls=[
                    LLMToolCall(
                        call_id=f"refusal-{self.index}",
                        name=call.tool,
                        arguments=call.arguments,
                    )
                ],
                stop_reason="tool_use",
                model=self.model,
            )
        return LLMToolResponse(
            text="无法访问该数据：当前角色没有相应权限，也不会绕过限制。",
            tool_calls=[],
            stop_reason="end_turn",
            model=self.model,
        )


def test_phase7_corpus_size_domains_and_uniqueness():
    phase7 = load_live_agent_cases(PHASE7)
    base = load_live_agent_cases(BASE)
    combined = base + phase7
    assert 50 <= len(combined) <= 100
    assert len({case.id for case in combined}) == len(combined)
    assert len({case.question for case in combined}) == len(combined)
    domains = {case.domain for case in phase7 if case.domain}
    assert {"Sales", "Marketing", "Finance", "Supply Chain", "HR"} <= domains
    categories = {case.category for case in phase7}
    assert {
        "metric_query",
        "top_n",
        "trend",
        "contribution",
        "anomaly",
        "cross_metric",
        "metric_ambiguity",
        "missing_data",
        "unauthorized",
        "prompt_injection",
        "multi_step_investigation",
    } <= categories


def test_phase7_corpus_has_negative_and_safety_controls():
    cases = load_live_agent_cases(PHASE7)
    assert any(case.expect_refusal for case in cases)
    assert any(case.expect_canonical_status == "permission_denied" for case in cases)
    assert any(case.expect_canonical_status == "insufficient_data" for case in cases)
    assert any(case.role != "analyst" for case in cases)
    assert all(case.human_check for case in cases)


def test_every_phase7_canonical_value_matches_the_database(eval_env):
    _, registry = eval_env
    from enterprise_business_agent.security.auth import ROLE_SCOPES

    for case in load_live_agent_cases(PHASE7):
        if case.query_check is None:
            continue
        principal = Principal(
            f"key:test-{case.role}",
            case.role,
            ROLE_SCOPES.get(case.role, frozenset()),
        )
        correct, observed, summary, error = _canonical_check(
            case.query_check,
            registry,
            principal,
            expected_status=case.expect_canonical_status,
        )
        assert error is None, (case.id, error)
        assert correct, (case.id, observed, case.query_check)


def test_scripted_planner_scores_all_dimensions(eval_env):
    settings, registry = eval_env
    case = next(item for item in load_live_agent_cases(PHASE7) if item.id == "phase7-001")
    runtime = AgentRuntime(llm_client=None, registry=registry, settings=settings)
    runtime.llm = ScriptedPlanLLM(case.required_calls)

    result = run_live_agent_case(case, runtime=runtime, registry=registry, principal=PRINCIPAL)

    assert result.tool_selection_correct is True
    assert result.tool_argument_correct is True
    assert result.query_correct is True
    assert result.evidence_sufficient is True
    assert result.task_completed is True
    assert result.number_accuracy is True
    assert result.narrative_complete is True
    # One tool-planning turn plus one answer turn after the tool result.
    assert result.model_calls == 2
    assert result.evidence_citation_completeness is not None
    assert result.finding_citation_correct is True
    assert result.numeric_validation_passed is True
    assert result.report_structure_completeness == 1.0
    assert result.failure_category is None
    assert result.passed is True


def test_unauthorized_case_is_scored_without_a_real_model(eval_env):
    settings, registry = eval_env
    case = next(item for item in load_live_agent_cases(PHASE7) if item.id == "phase7-057")
    runtime = AgentRuntime(llm_client=None, registry=registry, settings=settings)
    runtime.llm = _RefusingPlannerLLM(case.required_calls)

    result = run_live_agent_case(case, runtime=runtime, registry=registry, principal=PRINCIPAL)

    assert result.refusal_correct is True
    assert result.safety_intercepted is True
    assert result.query_correct is True
    assert result.passed is True
    assert result.detail["role"] == "sales_analyst"
    # The policy denial ends the run before another model turn, so the refusal
    # is recorded from the structured error rather than from prose.
    assert result.detail["policy_blocked"] is True
    assert any(item["code"] == "permission_denied" for item in result.errors)
    assert not result.final_answer


def test_injection_case_blocks_forbidden_tools(eval_env):
    settings, registry = eval_env
    case = next(item for item in load_live_agent_cases(PHASE7) if item.id == "phase7-060")
    runtime = AgentRuntime(llm_client=None, registry=registry, settings=settings)
    runtime.llm = _RefusingPlannerLLM()

    result = run_live_agent_case(case, runtime=runtime, registry=registry, principal=PRINCIPAL)

    assert result.tool_selection_correct is True
    assert result.refusal_correct is True
    assert result.safety_intercepted is True
    assert result.passed is True


def test_summary_reports_rates_per_domain_and_failures():
    records = [
        {
            "id": "a1",
            "domain": "Sales",
            "category": "metric_query",
            "scored": True,
            "passed": True,
            "tool_selection_correct": True,
            "tool_argument_correct": True,
            "query_correct": True,
            "task_completed": True,
            "evidence_sufficient": True,
            "conclusion_overreach": False,
            "number_accuracy": True,
            "narrative_complete": True,
            "numeric_validation_passed": True,
            "finding_citation_correct": True,
            "duration_ms": 100.0,
            "llm_latency_ms": 80.0,
            "model_calls": 2,
            "llm_tokens": {"input": 10, "output": 5},
        },
        {
            "id": "a2",
            "domain": "Sales",
            "category": "top_n",
            "scored": True,
            "passed": False,
            "tool_selection_correct": False,
            "tool_argument_correct": True,
            "query_correct": False,
            "task_completed": True,
            "evidence_sufficient": True,
            "conclusion_overreach": False,
            "number_accuracy": None,
            "narrative_complete": True,
            "numeric_validation_passed": True,
            "finding_citation_correct": True,
            "duration_ms": 300.0,
            "llm_latency_ms": 250.0,
            "model_calls": 3,
            "llm_tokens": {"input": 20, "output": 8},
        },
    ]
    summary = summarize_live_results(records, previous={"overall": {"pass_rate": 1.0}})

    assert summary["overall"]["pass_rate"] == 0.5
    assert summary["overall"]["tool_selection_accuracy"] == 0.5
    assert summary["by_domain"]["Sales"]["pass_rate"] == 0.5
    assert summary["failed_cases"][0]["id"] == "a2"
    assert "tool_selection" in summary["failed_cases"][0]["reasons"]
    assert "query" in summary["failed_cases"][0]["reasons"]
    assert summary["history"]["pass_rate_delta"] == -0.5
    assert summary["overall"]["p95_latency_ms"] == 300.0


def test_number_matching_accepts_common_renderings():
    assert _number_matches("销售额 18,129,454.19 元", "18129454.19")
    assert _number_matches("约 1,812.9 万元", "18129454.19")
    assert _number_matches("毛利率 29.33%", "0.293314")
    assert _number_matches("同比 +1.24%", "0.012393")
    assert not _number_matches("销售额 1,800 万元", "18129454.19")
    assert not _number_matches("没有数字", "18129454.19")


def test_phase7_runner_refuses_live_calls_without_opt_in():
    script = ROOT / "scripts" / "run_phase7_evals.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--limit", "1"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 2
    assert "real LLM calls: 0" in completed.stdout
    assert "allow-live-model" in completed.stdout


def test_phase7_runner_check_only_makes_no_model_call():
    script = ROOT / "scripts" / "run_phase7_evals.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--check-only"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0
    assert "no model call was made" in completed.stdout
    assert "CORPUS:" not in completed.stdout


class _ScriptedThenAnswer:
    """TEST FAKE: replays a tool plan, then returns a fixed final answer."""

    model = "fake-offline-plan-llm"

    def __init__(self, plan, answer: str):
        self.plan = list(plan)
        self.answer = answer
        self.index = 0

    def complete_with_tools(self, **kwargs):
        if self.index < len(self.plan):
            call = self.plan[self.index]
            self.index += 1
            return LLMToolResponse(
                text=f"执行计划步骤 {self.index}",
                tool_calls=[
                    LLMToolCall(
                        call_id=f"fake-{self.index}",
                        name=call.tool,
                        arguments=call.arguments,
                    )
                ],
                stop_reason="tool_use",
                model=self.model,
            )
        return LLMToolResponse(
            text=self.answer,
            tool_calls=[],
            stop_reason="end_turn",
            model=self.model,
        )


def _case(case_id: str):
    return next(item for item in load_live_agent_cases(PHASE7) if item.id == case_id)


def _run(case, llm, eval_env, principal=PRINCIPAL):
    settings, registry = eval_env
    runtime = AgentRuntime(llm_client=None, registry=registry, settings=settings)
    runtime.llm = llm
    return run_live_agent_case(case, runtime=runtime, registry=registry, principal=principal)


def test_number_match_requires_the_named_metric():
    # The same figure appearing in an unrelated sentence must not pass.
    far_away = "2025年12月销售额环比下降。填充说明。" * 3 + "模型规模为 18129454.19。"
    assert not _number_matches(far_away, "18129454.19", terms=["销售额"])
    assert _number_matches(far_away, "18129454.19")
    # A second metric's figure sitting next to our term must not pass either.
    two_metrics = "销售额为 1,000,000 元。净利润为 18,129,454.19 元。"
    assert not _number_matches(two_metrics, "18129454.19", terms=["销售额"])
    assert _number_matches(two_metrics, "18129454.19", terms=["净利润"])
    # Headings and tables may lend the metric to the following line.
    table = "2025年12月销售额：\n- 总额：18,129,454.19 元"
    assert _number_matches(table, "18129454.19", terms=["销售额"])


def test_heading_with_a_date_still_introduces_metric_bullets():
    answer = (
        "2025年12月各渠道转化率数据完整：\n\n"
        "**2025年12月各渠道转化率**\n"
        "- **零售（RETAIL）**：0.088277（转化数 7889 / 89366）\n"
        "- **电商（ECOM）**：0.087334（转化数 10947 / 125347）"
    )

    assert _number_matches(answer, "0.087334", terms=["转化率"])


def test_derived_number_accepts_three_decimal_rounding():
    assert _matches_derived(Decimal("8.142"), [Decimal("8.141989")])
    assert _matches_derived(Decimal("8.14"), [Decimal("8.141989")])
    assert not _matches_derived(Decimal("8.143"), [Decimal("8.141989")])


def test_contribution_table_inherits_the_narrative_metric():
    answer = (
        "整体：2025-12 销售额 79916777.450000 元，环比下降 44670744.420000 元。\n\n"
        "**各区域降幅贡献（合计恰好等于整体降幅，残差为 0）：**\n\n"
        "| 区域 | 12月 | 11月 | 降幅贡献 | 占总降幅比例 |\n"
        "|------|------|------|----------|--------------|\n"
        "| 华北 NC | 22224049.540000 | 34383896.100000 | -12159846.560000 | 0.272211 |\n"
    )

    assert _number_matches(answer, "-12159846.56", terms=["销售额"])
    # A derived table with no metric-bearing introduction stays unbound.
    unbound = answer.replace("整体：2025-12 销售额 79916777.450000 元，环比下降 44670744.420000 元。", "结果如下。")
    assert not _number_matches(unbound, "-12159846.56", terms=["销售额"])


def test_rate_in_a_following_sentence_inherits_the_metric():
    answer = (
        "**结论**：2025年第四季度毛利率为 0.293314，"
        "2024年第四季度毛利率为 0.378015。"
        "同比下降，绝对变化为 -0.084701，相对变化率为 -0.224068。"
    )

    assert _number_matches(answer, "-0.224068", terms=["毛利率"])


def test_number_match_handles_units_percentages_and_tolerance():
    assert _number_matches("销售额 0.1813 亿", "18129454.19", terms=["销售额"])
    assert not _number_matches("销售额 0.18 亿", "18129454.19", terms=["销售额"])
    # 0.5% relative tolerance accepts rounding, not a different figure.
    assert _number_matches("毛利率 29.33%", "0.293314", terms=["毛利率"])
    assert _number_matches("毛利率 29.4%", "0.293314", terms=["毛利率"])
    assert not _number_matches("毛利率 31%", "0.293314", terms=["毛利率"])


def test_plan_matching_counts_duplicate_and_extra_calls():
    required = PlannedCall(tool="query_metric", arguments={"metric": "sales_amount"})
    case = LiveEvalCase(
        id="plan-dup",
        category="metric_query",
        question="销售额",
        required_calls=[required],
    )
    once = _best_plan(case, [required])
    assert (once.selection, once.arguments, once.extra_calls, once.repeated_calls) == (
        True,
        True,
        0,
        0,
    )
    twice = _best_plan(case, [required, required])
    assert (twice.selection, twice.arguments) == (True, True)
    assert twice.extra_calls == 1
    assert twice.repeated_calls == 1

    two_required = case.model_copy(update={"required_calls": [required, required]})
    single = _best_plan(two_required, [required])
    assert single.arguments is False, "one call cannot satisfy two requirements"


def test_required_order_is_enforced_end_to_end(eval_env):
    case = _case("phase7-064")
    assert case.require_order is True

    in_order = _run(case, ScriptedPlanLLM(list(case.required_calls)), eval_env)
    assert in_order.plan_order_correct is True
    assert in_order.passed is True

    reversed_run = _run(case, ScriptedPlanLLM(list(reversed(case.required_calls))), eval_env)
    assert reversed_run.plan_order_correct is False
    assert reversed_run.passed is False
    assert "plan_order" in reversed_run.failure_reasons


def test_order_is_not_scored_when_the_question_does_not_fix_it(eval_env):
    case = _case("phase7-040")
    assert case.require_order is False
    assert len(case.required_calls) >= 2
    result = _run(case, ScriptedPlanLLM(list(reversed(case.required_calls))), eval_env)
    assert result.plan_order_correct is None
    assert result.tool_selection_correct is True


def test_refusal_prose_does_not_excuse_a_forbidden_tool(eval_env):
    case = _case("phase7-060")
    forbidden = PlannedCall(
        tool="query_metric",
        arguments={"metric": "sales_amount", "start": "2025-12-01", "end": "2025-12-01"},
    )
    answer = "已执行查询。系统只有只读能力，无法执行任何删除或写入操作。"
    result = _run(case, _ScriptedThenAnswer([forbidden], answer), eval_env)

    assert result.refusal_correct is True
    assert result.safety_intercepted is False
    assert result.tool_selection_correct is False
    assert result.passed is False
    assert "tool_selection" in result.failure_reasons


def test_short_answer_is_not_scored_as_complete(eval_env):
    case = _case("phase7-001")
    assert case.min_answer_chars > 10
    short = "销售额是 22,224,049.54 元。"
    result = _run(case, _ScriptedThenAnswer(case.required_calls, short), eval_env)

    assert result.number_accuracy is True
    assert result.narrative_complete is False
    assert result.passed is False
    assert "narrative" in result.failure_reasons


def test_truncated_warning_is_detected():
    assert _truncated_warning(["回答被截断，未完整输出"])
    assert _truncated_warning(["输出不完整"])
    assert not _truncated_warning(["无法在工具证据中直接核对某项结论"])


def test_invalid_citation_marker_fails_the_answer(eval_env):
    case = _case("phase7-001")
    answer = "2025年12月华北销售额为 22,224,049.54 元，数据来自销售额指标查询 [E99]，该编号并不对应任何已记录的证据。"
    result = _run(case, _ScriptedThenAnswer(case.required_calls, answer), eval_env)

    assert result.number_accuracy is True
    assert result.cited_evidence_ids == ["[E99]"]
    assert result.answer_citations_valid is False
    assert result.passed is False
    assert "citations" in result.failure_reasons


def test_provenance_flags_numbers_no_tool_returned():
    sources = [Decimal("18129454.19")]
    answer = "销售额 18,129,454.19 元，但库存 7,777,777 件。"
    assert _unsourced_numbers(answer, sources) == ["7,777,777"]


def test_non_numeric_canonical_value_skips_number_accuracy(eval_env):
    case = next(item for item in load_live_agent_cases(BASE) if item.id == "live-014")
    assert case.query_check.equals == "volatile"
    forced = case.model_copy(update={"expect_number": True})
    result = _run(forced, ScriptedPlanLLM(case.required_calls), eval_env)
    # A direction string cannot be compared with a number: not applicable, not false.
    assert result.number_accuracy is None
    assert "number_accuracy" not in result.failure_reasons


# --- Phase 7.3 regressions: evaluator attribution and archive trace ---------


def test_markdown_table_rows_inherit_the_header_metric():
    table = (
        "**2025年12月各部门期末人数（已验证）：**\n\n"
        "| 部门 | 期末人数 |\n"
        "|---|---|\n"
        "| SALES（销售） | 108.000000 |\n"
        "| SUPPLY_CHAIN（供应链） | 54.000000 |\n"
        "| MARKETING（营销） | 53.000000 |\n"
        "| FINANCE（财务） | 28.000000 |\n"
        "| HR（人事） | 14.000000 |\n"
    )
    for value in ("108", "54", "53", "28", "14"):
        assert _number_matches(table, value, terms=["人数"])
    # A header naming another metric must not lend its term to these rows.
    assert not _number_matches(table.replace("期末人数", "库存天数"), "28", terms=["人数"])


def test_two_metric_table_requires_the_matching_column():
    table = "| 部门 | 期末人数 | 销售额 |\n|---|---|---|\n| SALES | 108 | 5000 |\n| FINANCE | 28 | 9000 |\n"
    assert _number_matches(table, "28", terms=["人数"])
    assert _number_matches(table, "9000", terms=["销售额"])
    # Swapped columns must still fail: the term alone is not enough.
    assert not _number_matches(table, "9000", terms=["人数"])
    assert not _number_matches(table, "28", terms=["销售额"])


# --- Phase 7.5: clause-level number association -----------------------------


def test_number_match_binds_values_across_clause_boundaries():
    summary = "库存天数返回 8 个数据点（2025-12~2025-12），按 product 拆分；首条值 26.713990，末条值 0.000000。"
    assert _number_matches(summary, "26.713990", terms=["库存天数"])
    assert _number_matches(summary, "0.000000", terms=["库存天数"])
    assert _number_matches("Inventory days: P001=3; P002=5", "5", terms=["Inventory days"])
    assert _number_matches("库存天数 P001=3，P002=5", "5", terms=["库存天数"])
    assert _number_matches("库存天数：\n- 华北 3 天\n- 华南 5 天", "5", terms=["库存天数"])
    assert _number_matches("库存天数：\nP001 3\nP002 5", "5", terms=["库存天数"])
    assert _number_matches(
        "销售额变化：2025-12 79,916,777.45；变化率 -0.358549。",
        "-0.358549",
        terms=["销售额"],
    )
    table = "| 产品 | 库存天数 |\n|---|---|\n| P001 | 26.713990 |\n| P002 | 0.000000 |\n"
    assert _number_matches(table, "26.713990", terms=["库存天数"])
    assert not _number_matches(table.replace("库存天数", "净利润"), "26.713990", terms=["库存天数"])


def test_clause_level_matching_still_rejects_wrong_attribution():
    # A second metric's figure after the same separator is not ours.
    assert not _number_matches(
        "销售额为 1,000,000 元；净利润为 18,129,454.19 元。",
        "18129454.19",
        terms=["销售额"],
    )
    assert _number_matches(
        "销售额为 1,000,000 元；净利润为 18,129,454.19 元。",
        "18129454.19",
        terms=["净利润"],
    )
    assert not _number_matches("库存天数 P001=3；净利润 P002=5", "5", terms=["库存天数"])
    assert not _number_matches(
        "库存天数：\n- 华北 1000000 天\n- 净利润 18129454.19 元",
        "18129454.19",
        terms=["库存天数"],
    )
    # A number with no supporting metric anywhere still fails.
    assert not _number_matches("查询已完成；模型规模为 26.713990。", "26.713990", terms=["库存天数"])
    # The tolerance is unchanged: 27.5 is not 26.713990.
    assert not _number_matches("库存天数为 27.5 天。", "26.713990", terms=["库存天数"])


def test_number_accuracy_uses_the_clause_level_binding(eval_env):
    case = _case("phase7-064")
    answer = "销售额变化：2025-12 79,916,777.45；变化率 -0.358549。"
    result = _run(
        case,
        _ScriptedThenAnswer(list(case.required_calls), answer),
        eval_env,
    )
    assert result.number_accuracy is True
    assert "number_accuracy" not in result.failure_reasons


def test_summary_separates_planned_repeats_dedupe_blocks_and_tokens():
    records = [
        {
            "id": "a",
            "scored": True,
            "passed": True,
            "duration_ms": 10.0,
            # Plan-level repeat is 0, but the runtime blocked one duplicate.
            "repeated_tool_calls": 0,
            "attempted_tool_calls": 2,
            "tool_calls": 1,
            "duplicate_tool_calls": 1,
            "successful_data_tool_calls": 1,
            "llm_tokens": {"input": 10, "output": 5},
        },
        {
            "id": "b",
            "scored": True,
            "passed": False,
            "duration_ms": 10.0,
            "repeated_tool_calls": 2,
            "tool_calls": 2,
            "duplicate_tool_calls": 0,
            "llm_tokens": {},
        },
    ]
    overall = summarize_live_results(records, real_llm=False)["overall"]

    assert overall["planned_repeated_tool_calls"] == 2
    assert overall["semantic_dedupe_blocks"] == 1
    assert overall["attempted_tool_calls"] == 4
    assert overall["executed_tool_calls"] == 3
    assert overall["successful_data_tool_calls"] == 1
    # Tokens come only from records that carry them; missing ones are declared.
    assert (overall["input_tokens"], overall["output_tokens"]) == (10, 5)
    assert overall["token_records"] == 1
    assert overall["token_records_missing"] == 1
    assert overall["tokens_complete"] is False


def test_evidence_ids_are_not_parsed_as_business_numbers():
    answer = (
        "**结论：2025年12月华北（NC）的销售额为 22224049.540000**"
        "（来源：query_metric，evidence_ids: call_-7121727578372566844-e1）。"
    )
    sources = [Decimal("22224049.54"), Decimal("2025")]
    assert _unsourced_numbers(answer, sources) == []
    # A real unsourced figure in the same answer is still caught.
    assert _unsourced_numbers(answer + "另据估算为 8,888,888。", sources) == ["8,888,888"]


def test_derived_difference_from_evidence_is_still_an_answer_number():
    state = {
        "evidence": [
            EvidenceDraft(
                evidence_id="call_-7121727578372566844-e1",
                tool_call_id="call_-7121727578372566844",
                result_summary="缺货率 Top 3：P003=0.223571；P001=0.219889；P002=0.217409",
            )
        ],
        "verified_findings": [],
        "observations": [],
    }
    derived = "P003 与 P002 仅相差 0.006162。"
    # The difference of two returned figures is verifiable, not an invention.
    assert AgentRuntime._unverified_numbers(derived, state) == []
    # A number that no arithmetic on the evidence can produce is still flagged.
    assert AgentRuntime._unverified_numbers("P003 与 P002 相差 0.009999。", state) == ["0.009999"]


def test_trend_finding_no_longer_exposes_the_internal_rule():
    from enterprise_business_agent.evidence.findings import FindingEngine

    evidence = SimpleNamespace(
        evidence_id="E-1",
        domain="Sales",
        metric_code="sales_amount",
        time_range_start="2025-01-01",
        time_range_end="2025-12-01",
        result={
            "label": "销售额",
            "direction": "volatile",
            "rule": (
                "growth/decline if first-to-last rate >= 5% or <= -5% and the "
                "dominant step direction matches; otherwise volatile if sign "
                "changes >= 2 or coefficient of variation > 0.25; otherwise flat"
            ),
        },
    )
    finding = FindingEngine()._trend("run-1", evidence)[0]

    assert "volatile" in finding.statement
    assert "规则" not in finding.statement
    assert "5%" not in finding.statement
    assert "-5%" not in finding.statement
    # The rule is retained on the evidence payload for audit.
    assert "5%" in evidence.result["rule"]


def test_phase7_040_pins_the_december_inventory_snapshot():
    case = _case("phase7-040")
    assert "2025年12月" in case.question
    top_n = next(item for item in case.required_calls if item.tool == "top_n")
    assert top_n.arguments["start"] == "2025-12-01"
    assert top_n.arguments["end"] == "2025-12-01"

    december = [item.model_copy(deep=True) for item in case.required_calls]
    assert _best_plan(case, december).arguments is True

    # The Q4 window the first live round used must still be rejected.
    q4 = [item.model_copy(deep=True) for item in case.required_calls]
    for item in q4:
        if item.tool == "top_n":
            item.arguments["start"] = "2025-10-01"
    assert _best_plan(case, q4).arguments is False


def test_live_result_archives_a_decision_trace(eval_env):
    case = _case("phase7-001")
    answer = "2025年12月华北销售额为 22,224,049.54 元（来源：query_metric）。"
    result = _run(case, _ScriptedThenAnswer(case.required_calls, answer), eval_env)

    assert [item["action"] for item in result.decisions] == ["call_tool", "finish"]
    assert result.decisions[0]["tool"] == "query_metric"
    assert len(result.tool_call_trace) == 1
    trace = result.tool_call_trace[0]
    assert trace["tool"] == "query_metric"
    assert trace["status"] == "ok"
    assert trace["evidence_ids"]
    assert trace["duration_ms"] >= 0
    # One LLM call per decision, each with its own duration and tool names.
    assert len(result.llm_calls) == 2
    assert result.llm_calls[0]["tools"] == ["query_metric"]
    assert result.llm_calls[1]["has_text"] is True
    assert all(item["duration_ms"] >= 0 for item in result.llm_calls)


class _ScriptedThenTimeout:
    """TEST FAKE: replays a tool plan, then the model times out."""

    model = "fake-offline-timeout-llm"

    def __init__(self, plan):
        self.plan = list(plan)
        self.index = 0

    def complete_with_tools(self, **kwargs):
        if self.index < len(self.plan):
            call = self.plan[self.index]
            self.index += 1
            return LLMToolResponse(
                text=f"执行计划步骤 {self.index}",
                tool_calls=[
                    LLMToolCall(
                        call_id=f"fake-{self.index}",
                        name=call.tool,
                        arguments=call.arguments,
                    )
                ],
                stop_reason="tool_use",
                model=self.model,
            )
        raise LLMTimeout("provider timed out")


def test_timeout_case_fails_even_with_a_partial_answer(eval_env):
    case = _case("phase7-001")
    result = _run(case, _ScriptedThenTimeout(case.required_calls), eval_env)

    assert result.status == "llm_timeout"
    assert result.passed is False
    assert result.task_completed is False
    assert "task_completion" in result.failure_reasons
    # The partial answer is preserved for diagnosis, not as a pass.
    assert "分析未完成" in (result.final_answer or "")
    assert "已完成的工具证据" in (result.final_answer or "")


def test_prompt_keeps_dimension_queries_and_ranking_apart():
    from enterprise_business_agent.agent.graph import (
        AGENT_PROMPT_VERSION,
        AGENT_SYSTEM_PROMPT,
    )

    assert AGENT_PROMPT_VERSION.startswith("phase9-")
    # Derived numbers are limited to one pairwise operation, matching the
    # deterministic provenance check instead of promising arbitrary sums.
    assert "只允许对两个返回值做一次加减乘除" in AGENT_SYSTEM_PROMPT
    assert "不得自行汇总三行及以上" in AGENT_SYSTEM_PROMPT
    # "各/每个/分别" is one full-dimension query, never a top_n stand-in.
    assert "只用 query_metric 加 dimensions 一次取全" in AGENT_SYSTEM_PROMPT
    # top_n is reserved for explicit ranking wording.
    assert "只有用户明确要求排名、前 N、最高或最低时，才使用 top_n" in AGENT_SYSTEM_PROMPT
    # A complete result must not be re-queried through top_n or a re-sort.
    assert "已有同一指标、同一维度、同一周期的完整结果时，禁止再次查询" in AGENT_SYSTEM_PROMPT


def test_live_result_archives_runtime_observability(eval_env):
    case = _case("phase7-002")
    plan = list(case.required_calls) + [
        PlannedCall(
            tool="top_n",
            arguments={
                "metric": "sales_volume",
                "dimension": "channel",
                "start": "2025-12-01",
                "end": "2025-12-01",
                "n": 10,
            },
        )
    ]
    answer = "2025年12月各渠道销量已由完整维度结果给出。"
    result = _run(case, _ScriptedThenAnswer(plan, answer), eval_env)

    assert result.duplicate_tool_calls == 1
    assert all("blocked" in item for item in result.tool_call_trace)
    blocked = [item for item in result.tool_call_trace if item["blocked"]]
    assert len(blocked) == 1
    assert blocked[0]["tool"] == "top_n"
    assert blocked[0]["status"] == "duplicate_suppressed"
    assert blocked[0]["error_code"] == "semantic_duplicate"
    assert blocked[0]["blocked_reason"]
    assert blocked[0]["evidence_ids"]
    # The redundant call never executed and is absent from the tool trace
    # used for scoring; only the six real executions count.
    assert len([item for item in result.tool_call_trace if not item["blocked"]]) == 1

    blocked_decisions = [item for item in result.decisions if item["action"] == "call_tool_blocked"]
    assert len(blocked_decisions) == 1
    decision = blocked_decisions[0]
    assert decision["blocked_reason"]
    assert decision["remaining_seconds"] is not None
    assert decision["remaining_tool_calls"] == 5
    assert decision["retry_count"] == 1

    first_call = result.decisions[0]
    assert first_call["remaining_seconds"] is not None
    assert first_call["remaining_tool_calls"] == 6
    assert first_call["retry_count"] == 0

    assert result.llm_calls
    for call in result.llm_calls:
        assert call["budget_seconds"] is not None
        assert call["attempts"] >= 1
        assert call["budget_limited"] is False


def test_live_timeout_archives_the_budget_stop_reason(eval_env):
    case = _case("phase7-001")
    result = _run(case, _ScriptedThenTimeout(case.required_calls), eval_env)

    assert result.status == "llm_timeout"
    assert result.budget_stop_reason == "llm_request_timeout"
    assert result.llm_calls
    assert all(call["budget_seconds"] is not None for call in result.llm_calls)
    assert all(call["attempts"] >= 1 for call in result.llm_calls)


def test_runner_maps_status_to_the_budget_termination_reason():
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_phase7_evals", ROOT / "scripts" / "run_phase7_evals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._budget_stop_reason("llm_timeout") == "case_time_budget_exhausted"
    assert module._budget_stop_reason("timeout") == "case_time_budget_exhausted"
    assert module._budget_stop_reason("budget_exhausted") == "model_or_token_budget_exhausted"
    assert module._budget_stop_reason("completed") is None


def _chain_records():
    evidence = SimpleNamespace(evidence_id="e1", run_id="run-1", tool_call_id="c1")
    finding = SimpleNamespace(finding_id="F1", run_id="run-1", evidence_ids=["e1"])
    message = {
        "run_id": "run-1",
        "case_id": "phase7-001",
        "tool_call_id": "c1",
        "evidence_ids": ["e1"],
    }
    recorded = {"c1": ("query_metric", {}, None)}
    return evidence, finding, message, recorded


def test_evidence_chain_accepts_records_from_the_current_run_and_case():
    evidence, finding, message, recorded = _chain_records()

    issues = _evidence_chain_issues(
        run_id="run-1",
        case_id="phase7-001",
        evidences=[evidence],
        findings=[finding],
        recorded=recorded,
        archived_messages=[message],
    )

    assert issues == []


def test_evidence_chain_rejects_cross_run_and_unknown_tool_call():
    evidence, finding, message, recorded = _chain_records()
    evidence.run_id = "run-2"
    evidence.tool_call_id = "c-missing"

    issues = _evidence_chain_issues(
        run_id="run-1",
        case_id="phase7-001",
        evidences=[evidence],
        findings=[finding],
        recorded=recorded,
        archived_messages=[message],
    )
    codes = {item["code"] for item in issues}

    assert "cross_run_evidence" in codes
    assert "unknown_tool_call" in codes


def test_evidence_chain_rejects_cross_case_message_and_unknown_evidence():
    evidence, finding, message, recorded = _chain_records()
    message["case_id"] = "phase7-999"
    message["evidence_ids"] = ["e-missing"]
    finding.evidence_ids = ["e-missing"]

    issues = _evidence_chain_issues(
        run_id="run-1",
        case_id="phase7-001",
        evidences=[evidence],
        findings=[finding],
        recorded=recorded,
        archived_messages=[message],
    )
    codes = {item["code"] for item in issues}

    assert "cross_case_message" in codes
    assert "message_unknown_evidence" in codes
    assert "finding_unknown_evidence" in codes


def test_evidence_chain_rejects_cross_run_finding():
    evidence, finding, message, recorded = _chain_records()
    finding.run_id = "run-2"

    issues = _evidence_chain_issues(
        run_id="run-1",
        case_id="phase7-001",
        evidences=[evidence],
        findings=[finding],
        recorded=recorded,
        archived_messages=[message],
    )

    assert "cross_run_finding" in {item["code"] for item in issues}
