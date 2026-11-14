#!/usr/bin/env python
"""Run the Phase 7 multi-dimensional evaluation.

Offline checks (corpus integrity) never call a model. A live run requires
``--allow-live-model`` plus a configured provider; it never substitutes a fake
client and stops on transport/provider failure after recording the real
failure. A run is evidence only for the cases actually executed.

Usage:
    python scripts/run_phase7_evals.py --check-only
    python scripts/run_phase7_evals.py --allow-live-model --limit 0 \
        --output data/exports/phase7_eval.json \
        --markdown docs/phase7/PHASE7_EVALUATION_REPORT.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
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
    summarize_live_results,
)
from enterprise_business_agent.agent.live_readiness import (  # noqa: E402
    FAIL as CHECK_FAIL,
)
from enterprise_business_agent.agent.live_readiness import (
    FIRST_ROUND_IDS,
    config_snapshot,
    live_preflight,
)
from enterprise_business_agent.agent.live_readiness import (
    PASS as CHECK_PASS,
)
from enterprise_business_agent.agent.live_readiness import (
    WARN as CHECK_WARN,
)
from enterprise_business_agent.config import get_settings  # noqa: E402
from enterprise_business_agent.db import make_readonly_engine  # noqa: E402
from enterprise_business_agent.llm import (  # noqa: E402
    ModelCallBudgetExceeded,
    build_llm_client,
)
from enterprise_business_agent.security import AuthorizationPolicy, Principal  # noqa: E402

DEFAULT_CORPUS = ROOT / "tests" / "evals" / "phase7_cases.jsonl"
BASE_CORPUS = ROOT / "tests" / "evals" / "live_agent_cases.jsonl"
DEFAULT_MAX_SECONDS = 1_800.0
STOP_STATUSES = {
    AgentStatus.LLM_UNAVAILABLE.value,
    AgentStatus.LLM_TIMEOUT.value,
    # A run that exhausted its own time budget is just as terminal as a model
    # timeout; continuing the batch would apply a different budget per case.
    AgentStatus.TIMEOUT.value,
    AgentStatus.FAILED.value,
}
MAX_CASE_SECONDS = 300.0
# A provider outage returns quickly for every case. Three in a row means the
# remaining corpus would only measure the outage, not model quality.
MAX_CONSECUTIVE_TRANSPORT_FAILURES = 3
PRINCIPAL = Principal(
    "key:phase7-eval",
    "analyst",
    frozenset({"metrics:read", "agent:run"}),
)
REQUIRED_DOMAINS = {"Sales", "Marketing", "Finance", "Supply Chain", "HR"}
REQUIRED_CATEGORIES = {
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
}


def _load_all(corpus: Path, include_base: bool) -> list:
    cases = load_live_agent_cases(corpus)
    if include_base and BASE_CORPUS.exists():
        cases = load_live_agent_cases(BASE_CORPUS) + cases
    return cases


def check_corpus(cases: list, *, require_size: bool = True) -> list[str]:
    problems: list[str] = []
    if require_size and not 50 <= len(cases) <= 100:
        problems.append(f"case count {len(cases)} is outside 50-100")
    ids = [case.id for case in cases]
    questions = [case.question for case in cases]
    if len(set(ids)) != len(ids):
        problems.append("duplicate case ids")
    if len(set(questions)) != len(questions):
        problems.append("duplicate questions")
    domains = {case.domain for case in cases if case.domain}
    missing_domains = REQUIRED_DOMAINS - domains
    if missing_domains:
        problems.append(f"missing domains: {sorted(missing_domains)}")
    categories = {case.category for case in cases}
    missing_categories = REQUIRED_CATEGORIES - categories
    if missing_categories:
        problems.append(f"missing categories: {sorted(missing_categories)}")
    for case in cases:
        if case.scored and not (case.required_calls or case.plan_alternatives):
            if case.query_check is None and not case.expect_refusal:
                problems.append(f"{case.id}: scored case has no plan and no query check")
        if case.query_check is not None and case.expect_canonical_status == "ok":
            if case.query_check.min is None and case.query_check.equals is None:
                problems.append(f"{case.id}: query check has no expected value")
            if case.query_check.equals is not None and case.expect_number:
                try:
                    float(case.query_check.equals.replace(",", ""))
                except ValueError:
                    problems.append(f"{case.id}: non-numeric expectation requires expect_number=false")
        if case.require_order and len(case.required_calls) < 2:
            problems.append(f"{case.id}: require_order needs at least two calls")
    return problems


def _cost_block(summary: dict, settings) -> dict:
    tokens_in = summary["overall"]["input_tokens"]
    tokens_out = summary["overall"]["output_tokens"]
    price_in = settings.model_input_price_per_million
    price_out = settings.model_output_price_per_million
    configured = price_in is not None and price_out is not None
    estimated = None
    if configured:
        estimated = round(tokens_in / 1_000_000 * price_in + tokens_out / 1_000_000 * price_out, 6)
    runs = summary["overall"]["scored_cases"] or 0
    return {
        "estimated": configured,
        "currency": settings.model_price_currency,
        "price_source": settings.model_price_source,
        "input_price_per_million_tokens": price_in,
        "output_price_per_million_tokens": price_out,
        "tokens": {"input": tokens_in, "output": tokens_out},
        "estimated_total": estimated,
        "estimated_per_case": (round(estimated / runs, 6) if estimated is not None and runs else None),
        "basis": "token_count_x_configured_price",
        "note": (
            "价格来自配置，不是账单。未配置价格时不做估算。"
            if configured
            else "未配置模型单价（EBA_MODEL_INPUT/OUTPUT_PRICE_PER_MTOKENS），不估算成本。"
        ),
    }


def _fmt(value, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _preflight_summary(counts: dict | None) -> str:
    if not counts:
        return "—"
    return (
        f"{counts.get('total', '—')} 项："
        f"{counts.get(CHECK_PASS, 0)} ok / "
        f"{counts.get(CHECK_WARN, 0)} warn / "
        f"{counts.get(CHECK_FAIL, 0)} fail"
    )


def render_markdown(payload: dict) -> str:
    summary = payload["summary"]
    overall = summary["overall"]
    run = payload.get("run", {})
    config = payload.get("config", {})
    lines = [
        "# Phase 7 评测报告",
        "",
        f"生成时间：{payload['generated_at']}",
        "",
        ("> 本报告来自真实模型调用。" if summary["real_llm"] else "> **本报告不是真实模型结果**：未调用任何真实模型。"),
        "",
        "## 1. 基本信息",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 模型 | `{summary.get('model')}` |",
        f"| Provider | `{summary.get('provider')}` |",
        f"| Prompt 版本 | `{config.get('prompt_version')}` |",
        f"| 预置用例集 | {config.get('preset') or '全部'} |",
        f"| 选中用例 | {run.get('selected_cases', overall['selected_cases'])} |",
        f"| 实际执行 | {run.get('executed_cases', overall['scored_cases'])} |",
        f"| 未执行 | {len(run.get('not_executed_cases') or [])} |",
        f"| 计分用例 | {overall['scored_cases']} |",
        f"| 通过 / 失败 | {overall['passed']} / {overall['failed']} |",
        f"| 通过率 | {_fmt(overall['pass_rate'])} |",
        f"| 实际模型调用 | {overall['model_calls']} |",
        f"| 调用预算上限 | {run.get('max_model_calls', '—')} |",
        (
            "| 单例时间预算 | "
            f"{run.get('effective_case_seconds', (config.get('limits') or {}).get('max_seconds', '—'))} s |"
        ),
        f"| 硬上限拒绝的调用 | {run.get('budget_refused_calls', 0)} |",
        (
            f"| 输入 / 输出 Token | {overall['input_tokens']} / {overall['output_tokens']}"
            + (
                ""
                if overall.get("tokens_complete", True)
                else (
                    f"（仅汇总 {overall.get('token_records', 0)}/"
                    f"{overall.get('scored_cases', 0)} 条有独立 token 记录的案例；"
                    f"缺失 {overall.get('token_records_missing', 0)} 条不做推断）"
                )
            )
            + " |"
        ),
        f"| P50 / P95 单例耗时 | {_fmt(overall['p50_latency_ms'], 1)} / {_fmt(overall['p95_latency_ms'], 1)} ms |",
        f"| 停止原因 | {run.get('stopped_reason') or '无'} |",
        f"| 预算终止原因 | {run.get('budget_stop_reason') or '—'} |",
        f"| Preflight | {_preflight_summary(run.get('preflight'))} |",
        "",
    ]
    if run.get("not_executed_cases"):
        lines += [
            "未执行用例：" + "、".join(run["not_executed_cases"]),
            "",
        ]
    lines += [
        "",
        "## 2. 多维质量指标",
        "",
        "| 指标 | 值 | 评估用例数 |",
        "| --- | --- | --- |",
    ]
    rows = [
        ("Agent 决策 / 工具选择准确率", "tool_selection_accuracy"),
        ("工具参数准确率", "tool_argument_accuracy"),
        ("调用顺序正确率（仅限要求顺序的用例）", "plan_order_accuracy"),
        ("查询结果正确率", "query_accuracy"),
        ("任务完成率", "task_completion_rate"),
        ("Evidence 引用完整度（均值）", "evidence_citation_completeness"),
        ("Finding 引用正确率", "finding_citation_correctness"),
        ("Numeric Validator 通过率", "numeric_validator_pass_rate"),
        ("报告结构完整度（均值）", "report_structure_completeness"),
        ("数字准确率", "number_accuracy"),
        ("回答数字可溯源率（诊断）", "answer_number_provenance_rate"),
        ("回答引用标识有效率（诊断）", "answer_citation_validity"),
        ("安全拦截率", "safety_interception_rate"),
        ("错误拒绝率", "error_refusal_rate"),
        ("叙述完整率", "narrative_completeness"),
    ]
    for label, key in rows:
        lines.append(f"| {label} | {_fmt(overall.get(key))} | {overall.get(f'{key}_evaluated', 0)} |")
    scored_cases = int(overall.get("scored_cases") or 0)
    relation_failures = int(overall.get("relation_validation_failures") or 0)
    relation_rate = round((scored_cases - relation_failures) / scored_cases, 4) if scored_cases else None
    lines.append(f"| 业务关系一致性（最大/最小、排序、排名、数量） | {_fmt(relation_rate)} | {scored_cases} |")

    lines += [
        "",
        "## 3. 分业务域",
        "",
        "| 业务域 | 用例 | 通过率 | 工具选择 | 参数 | 任务完成 | 数字准确 | P95 耗时(ms) |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for domain, block in summary["by_domain"].items():
        lines.append(
            f"| {domain} | {block['cases']} | {_fmt(block['pass_rate'])} | "
            f"{_fmt(block['tool_selection_accuracy'])} | "
            f"{_fmt(block['tool_argument_accuracy'])} | "
            f"{_fmt(block['task_completion_rate'])} | "
            f"{_fmt(block['number_accuracy'])} | "
            f"{_fmt(block['p95_latency_ms'], 1)} |"
        )

    lines += [
        "",
        "## 4. 分题型",
        "",
        "| 题型 | 用例 | 通过率 | 工具选择 | 参数 | 任务完成 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for category, block in summary["by_category"].items():
        lines.append(
            f"| {category} | {block['cases']} | {_fmt(block['pass_rate'])} | "
            f"{_fmt(block['tool_selection_accuracy'])} | "
            f"{_fmt(block['tool_argument_accuracy'])} | "
            f"{_fmt(block['task_completion_rate'])} |"
        )

    lines += ["", "## 5. 失败用例", ""]
    if summary["failed_cases"]:
        lines += [
            "| 用例 | 业务域 | 状态 | 失败维度 | 期望观测值 | 回答摘要 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for item in summary["failed_cases"]:
            excerpt = (item["answer_excerpt"] or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {item['id']} | {item['domain']} | {item['status']} | "
                f"{', '.join(item['reasons']) or '—'} | {item['observed']} | {excerpt} |"
            )
    else:
        lines.append("无失败用例。")

    cost = payload["cost"]
    lines += [
        "",
        "## 6. 成本与性能",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 成本估算 | {_fmt(cost['estimated_total'], 6)} {cost['currency']} |",
        f"| 价格来源 | {cost['price_source']} |",
        f"| 单例平均耗时 | {_fmt(overall['p50_latency_ms'], 1)} ms (P50) |",
        f"| 模型平均延迟 | {_fmt(overall['avg_llm_latency_ms'], 1)} ms |",
        f"| 单例平均模型调用 | {_fmt(overall['model_calls'] / overall['scored_cases'] if overall['scored_cases'] else None, 2)} |",
        f"| 计划内重复工具调用 | {overall.get('planned_repeated_tool_calls', overall.get('repeated_tool_calls', 0))} |",
        f"| 模型发起的工具调用 | {overall.get('attempted_tool_calls', 0)} |",
        f"| 实际执行工具调用 | {overall.get('executed_tool_calls', 0)} |",
        f"| 语义去重拦截（复用已有结果，无新查询） | {overall.get('semantic_dedupe_blocks', 0)} |",
        f"| 成功的数据查询调用 | {overall.get('successful_data_tool_calls', 0)} |",
        f"| 已归档的模型工具消息 | {overall.get('tool_messages_archived', 0)} |",
        f"| 归档内容超限被截断 | {overall.get('tool_messages_content_truncated', 0)} |",
        f"| 业务关系校验不一致（最大/最小、排序、排名、数量） | {overall.get('relation_validation_failures', 0)} |",
        f"| 计划外工具调用次数 | {overall.get('extra_tool_calls', 0)} |",
        "",
        f"> {cost['note']} 成本是 token 数乘以配置单价，不是供应商账单。",
        "> 回答数字可溯源率与引用标识有效率是**诊断维度**，不参与 `passed` 判定；"
        "派生比值等合法计算数字会计入未溯源，需人工复核。",
        "",
        "## 7. 历史对比",
        "",
    ]
    history = summary.get("history")
    if history:
        lines.append(
            f"上一版本通过率 {_fmt(history['previous_pass_rate'])}"
            f"（{history.get('previous_generated_at') or '未知时间'}），"
            f"本次变化 {_fmt(history['pass_rate_delta'])}。"
        )
    else:
        lines.append("没有提供历史版本记录。")
    lines += [
        "",
        "## 8. 说明与限制",
        "",
        "- 期望数字来自同一确定性数据库上的独立 canonical 查询，不是模型生成。",
        "- 报告结构、引用与 Numeric Validator 指标由独立的确定性报告生成器计算，不额外调用模型。",
        "- 未执行的用例不计入通过率；失败用例原样保留，不删除、不降低断言。",
        "- 评测只代表本次模型、Prompt 版本和数据集下的结果，不外推到未测问题。",
        "",
    ]
    return "\n".join(lines)


class _BudgetedClient:
    """Refuses the call that would exceed the run's hard model-call cap.

    ``_execute_cases`` checks the budget *between* cases, but one case can issue
    several calls, so that check alone can overshoot. This guard makes the cap
    absolute: the (N+1)th call raises before any request is built.
    """

    def __init__(self, inner, max_calls: int):
        self._inner = inner
        self.remaining = max_calls
        self.sent = 0
        self.refused = 0

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def complete_with_tools(self, **kwargs):
        if self.remaining <= 0:
            self.refused += 1
            raise ModelCallBudgetExceeded(
                f"local hard cap of {self.sent} model calls reached; another call was refused"
            )
        self.remaining -= 1
        self.sent += 1
        return self._inner.complete_with_tools(**kwargs)


def _eval_error_record(case, exc: Exception) -> dict:
    return {
        "id": case.id,
        "category": case.category,
        "question": case.question,
        "domain": case.domain or case.category,
        "scored": case.scored,
        "passed": False,
        "status": "eval_error",
        "failure_reasons": ["eval_error"],
        "final_answer": None,
        "duration_ms": 0.0,
        "model_calls": 0,
        "llm_tokens": {},
        "detail": {"error": f"{type(exc).__name__}: {exc}"},
    }


def _budget_stop_reason(status: str) -> str | None:
    if status in {"llm_timeout", "timeout"}:
        return "case_time_budget_exhausted"
    if status == "budget_exhausted":
        return "model_or_token_budget_exhausted"
    return None


def _execute_cases(
    cases: list,
    *,
    run_case,
    max_model_calls: int,
    max_seconds: float,
    clock=time.monotonic,
    printer=print,
    continue_on_failure: bool = False,
    initial_records: list[dict] | None = None,
) -> dict:
    """Run one case at a time, stopping at the first budget or hard failure.

    Cases that never ran are left out of ``records``, so they cannot enter the
    pass rate; they are reported by id instead. ``continue_on_failure`` keeps
    going after a hard failure so a long regression can still cover the rest
    of the corpus; the failure stays a recorded failure. ``initial_records``
    seeds a resumed run with records already persisted from earlier attempts.
    """

    records: list[dict] = list(initial_records or [])
    executed_ids: list[str] = [record["id"] for record in records]
    hard_failure_ids: list[str] = []
    model_calls = sum(int(record.get("model_calls") or 0) for record in records)
    consecutive_transport_failures = 0
    stopped_reason: str | None = None
    budget_stop_reason: str | None = None
    started = clock()
    for case in cases:
        if model_calls >= max_model_calls:
            stopped_reason = "budget_exhausted"
            budget_stop_reason = "model_call_budget_exhausted"
            printer("STOP: model-call budget exhausted; not continuing.")
            break
        if clock() - started > max_seconds:
            stopped_reason = "wall_time_budget_exhausted"
            budget_stop_reason = "wall_time_budget_exhausted"
            printer("STOP: wall-clock budget exhausted; not continuing.")
            break
        try:
            result = run_case(case)
        except Exception as exc:  # eval_error: record it, then stop
            records.append(_eval_error_record(case, exc))
            executed_ids.append(case.id)
            stopped_reason = f"{case.id}:eval_error"
            budget_stop_reason = "evaluator_error"
            printer(f"FAIL {case.id}: eval_error {type(exc).__name__}: {exc}")
            printer("STOP: evaluator raised; not continuing.")
            break
        record = result.model_dump(mode="json")
        record["human_check"] = case.human_check
        records.append(record)
        executed_ids.append(case.id)
        model_calls += result.model_calls
        verdict = "PASS" if result.passed else "FAIL"
        printer(
            f"{verdict} {result.id} [{result.domain}/{result.category}] "
            f"status={result.status} select={result.tool_selection_correct} "
            f"args={result.tool_argument_correct} order={result.plan_order_correct} "
            f"query={result.query_correct} "
            f"evidence={result.evidence_sufficient} overreach={result.conclusion_overreach} "
            f"number={result.number_accuracy} narrative={result.narrative_complete} "
            f"calls={result.model_calls} "
            f"latency={result.duration_ms:.0f}ms tokens={result.llm_tokens}"
        )
        if result.failure_reasons:
            printer(f"     reasons: {', '.join(result.failure_reasons)}")
        if result.status == AgentStatus.LLM_UNAVAILABLE.value:
            consecutive_transport_failures += 1
        else:
            consecutive_transport_failures = 0
        if result.status in STOP_STATUSES:
            hard_failure_ids.append(result.id)
            if consecutive_transport_failures >= MAX_CONSECUTIVE_TRANSPORT_FAILURES:
                stopped_reason = "consecutive_transport_failures"
                budget_stop_reason = "provider_unavailable"
                printer(
                    "STOP: "
                    f"{consecutive_transport_failures} consecutive transport "
                    "failures; the provider looks unavailable."
                )
                break
            if continue_on_failure:
                printer(f"CONTINUE: {result.id} ended with {result.status}; recorded as a hard failure.")
                continue
            stopped_reason = f"{result.id}:{result.status}"
            budget_stop_reason = _budget_stop_reason(result.status)
            printer(f"STOP: real LLM run ended with {result.status}; not continuing.")
            break
    return {
        "records": records,
        "executed_ids": executed_ids,
        "hard_failure_ids": hard_failure_ids,
        "model_calls": model_calls,
        "stopped_reason": stopped_reason,
        "budget_stop_reason": budget_stop_reason,
        "wall_time_ms": round((clock() - started) * 1000, 3),
        "not_executed_cases": [case.id for case in cases if case.id not in set(executed_ids)],
    }


def _print_readiness(readiness) -> None:
    for item in readiness.checks:
        print(f"{item.status:4} {item.name}: {item.message}")
    counts = readiness.counts()
    print(
        f"preflight: {counts['total']} checks, {counts[CHECK_PASS]} ok, "
        f"{counts[CHECK_WARN]} warn, {counts[CHECK_FAIL]} fail"
    )
    # A missing operator flag is not a broken environment; say so explicitly so
    # nobody "fixes" a working provider configuration.
    if readiness.operator_gated:
        print("  operator gates not opened: " + ", ".join(item.name for item in readiness.operator_gated))
    if readiness.environment_blockers:
        print("  environment blockers: " + ", ".join(item.name for item in readiness.environment_blockers))


def _select_cases(cases: list, args) -> list:
    selected = cases
    if args.preset == "first-round-20":
        wanted = set(FIRST_ROUND_IDS)
        selected = [case for case in selected if case.id in wanted]
    if args.ids:
        wanted = set(args.ids)
        selected = [case for case in selected if case.id in wanted]
    if args.domains:
        wanted = {item.lower() for item in args.domains}
        selected = [case for case in selected if case.domain.lower() in wanted]
    if args.limit > 0:
        selected = selected[: args.limit]
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase 7 evaluations")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument(
        "--include-base", action="store_true", default=True, help="include the Phase 4 live corpus (default: true)"
    )
    parser.add_argument("--no-include-base", dest="include_base", action="store_false")
    parser.add_argument("--limit", type=int, default=0, help="number of cases to run; 0 runs all")
    parser.add_argument("--ids", nargs="*", default=None)
    parser.add_argument("--domains", nargs="*", default=None)
    parser.add_argument(
        "--preset",
        choices=["first-round-20"],
        default=None,
        help="run a fixed, reviewed subset instead of the whole corpus",
    )
    parser.add_argument("--output", default="data/exports/phase7_eval.json")
    parser.add_argument("--markdown", default=None)
    parser.add_argument("--previous", default=None, help="previous JSON result for a version comparison")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="check corpus integrity only; returns before provider config is read",
    )
    parser.add_argument("--allow-live-model", action="store_true")
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=0,
        help="hard cap on model calls for the whole run; required for live runs",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=DEFAULT_MAX_SECONDS,
        help="wall-clock cap for the whole run",
    )
    parser.add_argument(
        "--case-max-seconds",
        type=float,
        default=None,
        help=(
            "explicit per-case run budget; overrides AGENT_MAX_SECONDS for this "
            f"process only (max {MAX_CASE_SECONDS:g}s). Without it the value "
            "from the environment/.env is used unchanged."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "reuse records from <output>.partial.jsonl and run only cases that "
            "are still missing; use after an interrupted live regression"
        ),
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help=(
            "record a hard failure (timeout/budget stop) and continue with the "
            "next case; without it the run stops at the first hard failure"
        ),
    )
    parser.add_argument(
        "--load-dotenv",
        action="store_true",
        help=("load ./.env before building settings; without it only process environment variables are read"),
    )
    args = parser.parse_args()

    cases = _load_all(Path(args.corpus), args.include_base)
    problems = check_corpus(cases)
    print(f"corpus: {len(cases)} cases")
    if problems:
        for problem in problems:
            print(f"CORPUS: {problem}")
        return 1
    if args.check_only:
        print("corpus check: OK (no model call was made)")
        return 0

    selected = _select_cases(cases, args)
    if not selected:
        print("FAIL: no cases selected.")
        return 1
    selected_total = list(selected)
    if args.preset:
        print(f"preset: {args.preset} -> {len(selected)} cases")

    partial_path = Path(args.output).with_name(Path(args.output).stem + ".partial.jsonl")
    resumed: list[dict] = []
    if args.resume and partial_path.exists():
        latest: dict[str, dict] = {}
        for line in partial_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("id"):
                latest[str(record["id"])] = record
        resumed = list(latest.values())
        done = {record["id"] for record in resumed}
        before = len(selected)
        selected = [case for case in selected if case.id not in done]
        print(
            f"resume: {len(resumed)} persisted records loaded; "
            f"{before - len(selected)} cases skipped, {len(selected)} to run"
        )

    dotenv_path = ROOT / ".env"
    settings = get_settings(load_dotenv=args.load_dotenv, dotenv_path=dotenv_path)
    if args.case_max_seconds is not None:
        if not 0 < args.case_max_seconds <= MAX_CASE_SECONDS:
            print(
                "FAIL: --case-max-seconds must be greater than 0 and at most "
                f"{MAX_CASE_SECONDS:g}; no request was sent."
            )
            return 3
        # The runtime treats settings.agent_max_seconds as a hard ceiling, so an
        # explicit eval budget has to be applied to this process's settings.
        # .env is never modified.
        settings = settings.model_copy(update={"agent_max_seconds": args.case_max_seconds})
        print(f"case budget: {args.case_max_seconds:g}s per case (explicit override; .env unchanged)")
    print(
        "dotenv: "
        + (
            f"loaded {dotenv_path.name}"
            if args.load_dotenv
            else f"not loaded ({dotenv_path.name} exists)"
            if dotenv_path.exists()
            else "not present; process environment only"
        )
    )
    readiness = live_preflight(
        settings,
        selected,
        allow_live_model=args.allow_live_model,
        max_model_calls=args.max_model_calls,
        preset=args.preset,
        output_path=args.output,
        dotenv_path=dotenv_path,
        dotenv_loaded=args.load_dotenv,
    )
    if args.preflight_only:
        _print_readiness(readiness)
        print("preflight only: no request was sent. real LLM calls: 0")
        return 1 if readiness.blocking else 0
    if not args.allow_live_model:
        print(
            "FAIL: live model calls are disabled. Re-run with --allow-live-model "
            "after confirming the API key and budget. real LLM calls: 0."
        )
        return 2

    _print_readiness(readiness)
    if args.max_model_calls <= 0:
        print(
            "FAIL: --max-model-calls is required for a live run; the call budget was not confirmed. real LLM calls: 0."
        )
        return 3
    if readiness.blocking:
        print("FAIL: preflight has blocking issues; no request was sent. real LLM calls: 0.")
        return 1

    client = build_llm_client(settings)
    if client is None:
        print("FAIL: no real LLM client was built; no request was sent.")
        return 1
    budget = _BudgetedClient(client, args.max_model_calls)

    engine = make_readonly_engine(settings.application_database_url, settings.query_timeout_ms)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    registry = ToolRegistry(
        AnalysisGateway(factory, settings),
        AuthorizationPolicy(settings),
        settings,
    )
    runtime = AgentRuntime(llm_client=budget, registry=registry, settings=settings)

    outcome: dict = {}
    print(
        f"cases: {len(selected)} to run" + (f", {len(resumed)} resumed" if resumed else "") + " (real LLM, no fallback)"
    )

    def run_case(case):
        result = run_live_agent_case(
            case,
            runtime=runtime,
            registry=registry,
            principal=PRINCIPAL,
            max_seconds=args.case_max_seconds,
        )
        record = result.model_dump(mode="json")
        record["human_check"] = case.human_check
        partial_path.parent.mkdir(parents=True, exist_ok=True)
        with partial_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return result

    try:
        outcome = _execute_cases(
            selected,
            run_case=run_case,
            max_model_calls=args.max_model_calls,
            max_seconds=args.max_seconds,
            continue_on_failure=args.continue_on_failure,
            initial_records=resumed,
        )
    finally:
        engine.dispose()

    if budget.refused:
        print(f"STOP: local model-call cap reached; refused {budget.refused} call(s) after {budget.sent} sent.")

    records = outcome["records"]
    not_executed = outcome["not_executed_cases"]
    model_calls_total = outcome["model_calls"]
    stopped_reason = outcome["stopped_reason"]
    budget_stop_reason = outcome["budget_stop_reason"]
    wall_ms = outcome["wall_time_ms"]
    previous = None
    if args.previous:
        previous_path = Path(args.previous)
        if previous_path.exists():
            previous = json.loads(previous_path.read_text(encoding="utf-8")).get("summary")

    summary = summarize_live_results(
        records,
        previous=previous,
        model=getattr(client, "model", None),
        provider=settings.llm_provider,
        real_llm=True,
    )
    summary["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    run_info = {
        "preset": args.preset,
        "selected_cases": len(selected) + len(resumed),
        "resumed_cases": len(resumed),
        "partial_records": str(partial_path),
        "executed_cases": len(records),
        "not_executed_cases": not_executed,
        "hard_failure_ids": outcome.get("hard_failure_ids", []),
        "continue_on_failure": args.continue_on_failure,
        "model_calls": model_calls_total,
        # Kept under the old name so earlier archives stay comparable; it now
        # counts real model calls rather than attempted cases.
        "real_llm_calls": model_calls_total,
        "max_model_calls": args.max_model_calls,
        "budget_refused_calls": budget.refused,
        "model_calls_sent": budget.sent,
        "max_seconds": args.max_seconds,
        "case_max_seconds": args.case_max_seconds,
        "effective_case_seconds": settings.agent_max_seconds,
        "stopped_reason": stopped_reason,
        "budget_stop_reason": budget_stop_reason,
        "wall_time_ms": wall_ms,
        "dotenv_loaded": args.load_dotenv,
        "preflight": readiness.counts(),
    }
    summary["corpus"] = run_info
    payload = {
        "generated_at": summary["generated_at"],
        "summary": summary,
        "run": run_info,
        "config": config_snapshot(settings, selected_total, preset=args.preset, dotenv_loaded=args.load_dotenv),
        "cost": _cost_block(summary, settings),
        "records": records,
    }

    print(
        f"\nreal_llm_calls={model_calls_total} "
        f"executed={len(records)}/{len(selected_total)} "
        f"passed={summary['overall']['passed']}/{summary['overall']['scored_cases']} "
        f"failed={summary['overall']['failed']} stopped={stopped_reason}"
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"record written to {output}")
    if args.markdown:
        markdown_path = Path(args.markdown)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(render_markdown(payload), encoding="utf-8")
        print(f"markdown written to {markdown_path}")

    ok = summary["overall"]["failed"] == 0 and stopped_reason is None and len(records) == len(selected_total)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
