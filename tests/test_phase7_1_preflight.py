"""Offline tests for the live-run guard rails.

Nothing here contacts a provider. The subprocess tests run the real CLI with a
deliberately empty environment, which is also proof that a run without a key
stops before any request is built.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from enterprise_business_agent.agent.evaluation import (
    LiveEvalResult,
    load_live_agent_cases,
    summarize_live_results,
)
from enterprise_business_agent.agent.live_readiness import (
    FAIL,
    FIRST_ROUND_IDS,
    HARD_MAX_MODEL_CALLS,
    PASS,
    WARN,
    config_snapshot,
    first_round_coverage,
    live_preflight,
)
from enterprise_business_agent.config import (
    Settings,
    dotenv_llm_declarations,
    get_settings,
)
from enterprise_business_agent.llm import LLMUnavailable, ModelCallBudgetExceeded

ROOT = Path(__file__).resolve().parents[1]
PHASE7 = ROOT / "tests" / "evals" / "phase7_cases.jsonl"
SCRIPT = ROOT / "scripts" / "run_phase7_evals.py"
SECRET = "sk-test-redaction-fixture-0000000000"
KEY_VARS = ("LLM_API_KEY", "ANTHROPIC_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY")
PROVIDER_VARS = ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL")


@contextmanager
def _env_snapshot():
    """Undo anything a test (or a loaded dotenv) adds to ``os.environ``."""

    before = dict(os.environ)
    try:
        yield
    finally:
        for name in set(os.environ) - set(before):
            os.environ.pop(name, None)
        for name, value in before.items():
            if os.environ.get(name) != value:
                os.environ[name] = value


def _clear_provider_env(monkeypatch) -> None:
    for name in KEY_VARS + PROVIDER_VARS:
        monkeypatch.delenv(name, raising=False)



def _settings(**overrides) -> Settings:
    values = dict(
        data_start=date(2024, 1, 1),
        data_months=24,
        llm_provider="anthropic",
        llm_model="claude-sonnet-5-5",
        llm_base_url="https://api.anthropic.com/v1/messages",
        llm_api_key=None,
    )
    values.update(overrides)
    return Settings(**values)


def _cases() -> list:
    return load_live_agent_cases(PHASE7)


def _first_round() -> list:
    wanted = set(FIRST_ROUND_IDS)
    return [case for case in _cases() if case.id in wanted]


def _status(readiness) -> dict[str, str]:
    return {item.name: item.status for item in readiness.checks}


def _clean_env() -> dict[str, str]:
    """An environment with no provider key, so a live run cannot start."""

    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "PYTHONPATH": "src",
        "EBA_LOAD_DOTENV": "0",
        "RUN_STORE_URL": "memory",
    }


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_preflight_blocks_without_authorization():
    readiness = live_preflight(
        _settings(llm_api_key=SECRET),
        _first_round(),
        allow_live_model=False,
        max_model_calls=40,
    )
    status = _status(readiness)
    assert status["authorization.live_model"] == FAIL
    assert readiness.blocking is True
    assert "不会发送任何请求" in next(
        item.message for item in readiness.failed
        if item.name == "authorization.live_model"
    )


def test_preflight_requires_a_confirmed_budget():
    for budget in (0, -5, len(_first_round()) - 1, HARD_MAX_MODEL_CALLS + 1):
        readiness = live_preflight(
            _settings(llm_api_key=SECRET),
            _first_round(),
            allow_live_model=True,
            max_model_calls=budget,
        )
        assert _status(readiness)["authorization.budget"] == FAIL, budget


def test_preflight_accepts_a_configured_run(tmp_path):
    readiness = live_preflight(
        _settings(llm_api_key=SECRET),
        _first_round(),
        allow_live_model=True,
        max_model_calls=40,
        preset="first-round-20",
        output_path=tmp_path / "run.json",
    )
    status = _status(readiness)
    assert readiness.failed == []
    assert status["authorization.live_model"] == PASS
    assert status["authorization.budget"] == PASS
    assert status["corpus.first_round_coverage"] == PASS
    assert status["provider.endpoint"] == PASS
    assert status["provider.api_key"] == PASS
    # Prices are optional; a missing price must not block, only warn.
    assert status["cost.prices"] == WARN


def test_preflight_flags_a_model_that_does_not_match_the_provider():
    readiness = live_preflight(
        _settings(llm_api_key=SECRET, llm_model="glm-4-flash"),
        _first_round(),
        allow_live_model=True,
        max_model_calls=40,
    )
    assert _status(readiness)["provider.endpoint"] == FAIL


def test_preflight_flags_an_unknown_provider():
    readiness = live_preflight(
        _settings(llm_api_key=SECRET, llm_provider="openai"),
        _first_round(),
        allow_live_model=True,
        max_model_calls=40,
    )
    assert _status(readiness)["provider.name"] == FAIL
    assert _status(readiness)["provider.endpoint"] == FAIL


def test_config_snapshot_records_no_secret(tmp_path):
    snapshot = config_snapshot(
        _settings(llm_api_key=SECRET),
        _first_round(),
        preset="first-round-20",
        dotenv_loaded=True,
    )
    rendered = json.dumps(snapshot, ensure_ascii=False)
    assert SECRET not in rendered
    assert "sk-" not in rendered
    assert snapshot["api_key_configured"] is True
    assert snapshot["api_key_env_names"] == ["LLM_API_KEY", "ANTHROPIC_API_KEY"]
    assert snapshot["dotenv_loaded"] is True
    assert snapshot["base_url_host"] == "api.anthropic.com"
    assert snapshot["prompt_version"]
    assert snapshot["preset"] == "first-round-20"
    assert len(snapshot["case_ids"]) == 20


def test_first_round_preset_covers_every_dimension():
    selected = _first_round()
    assert len(selected) == 20
    assert len({case.id for case in selected}) == 20
    coverage = first_round_coverage(selected)
    assert coverage == {
        "missing_ids": [],
        "missing_domains": [],
        "missing_categories": [],
    }


def test_first_round_preflight_detects_a_missing_dimension():
    selected = [case for case in _first_round() if case.category != "missing_data"]
    readiness = live_preflight(
        _settings(llm_api_key=SECRET),
        selected,
        allow_live_model=True,
        max_model_calls=40,
        preset="first-round-20",
    )
    status = _status(readiness)
    assert status["corpus.first_round_coverage"] == FAIL
    assert readiness.blocking is True


def test_runner_preflight_only_reports_without_a_request(tmp_path):
    completed = _run(
        "--preflight-only",
        "--preset",
        "first-round-20",
        "--output",
        str(tmp_path / "run.json"),
    )
    assert completed.returncode == 1
    assert "first-round-20 -> 20 cases" in completed.stdout
    assert "no request was sent" in completed.stdout
    assert "real LLM calls: 0" in completed.stdout
    assert not (tmp_path / "run.json").exists()


def test_runner_requires_a_budget_confirmation(tmp_path):
    completed = _run(
        "--allow-live-model", "--preset", "first-round-20",
        "--output", str(tmp_path / "run.json"),
    )
    assert completed.returncode == 3
    assert "max-model-calls is required" in completed.stdout
    assert "real LLM calls: 0" in completed.stdout
    assert not (tmp_path / "run.json").exists()


def test_runner_does_not_send_a_request_without_a_key(tmp_path):
    completed = _run(
        "--allow-live-model", "--max-model-calls", "40",
        "--preset", "first-round-20",
        "--output", str(tmp_path / "run.json"),
    )
    assert completed.returncode == 1
    assert "preflight has blocking issues" in completed.stdout
    assert "real LLM calls: 0" in completed.stdout
    assert not (tmp_path / "run.json").exists()


def test_runner_rejects_a_budget_above_the_hard_limit(tmp_path):
    completed = _run(
        "--allow-live-model", "--max-model-calls", str(HARD_MAX_MODEL_CALLS + 1),
        "--preset", "first-round-20",
        "--output", str(tmp_path / "run.json"),
    )
    assert completed.returncode == 1
    assert "超过硬上限" in completed.stdout
    assert not (tmp_path / "run.json").exists()


def _runner_module():
    spec = importlib.util.spec_from_file_location("run_phase7_evals", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_result(case, *, model_calls: int = 2, status: str = "completed", tokens=None):
    return LiveEvalResult(
        id=case.id,
        category=case.category,
        question=case.question,
        domain=case.domain or case.category,
        scored=case.scored,
        planning_correct=True,
        tool_selection_correct=True,
        tool_argument_correct=True,
        query_correct=True,
        evidence_sufficient=True,
        conclusion_overreach=False,
        task_completed=True,
        passed=True,
        status=status,
        model_calls=model_calls,
        llm_tokens=dict(tokens or {}),
    )


def _quiet(*_args, **_kwargs):
    return None


def test_executor_stops_on_eval_error_and_excludes_the_rest():
    module = _runner_module()
    cases = _cases()[:3]

    def run_case(case):
        if case.id == cases[0].id:
            return _fake_result(case)
        raise RuntimeError("boom")

    outcome = module._execute_cases(
        cases,
        run_case=run_case,
        max_model_calls=10,
        max_seconds=10,
        printer=_quiet,
    )

    assert outcome["executed_ids"] == [cases[0].id, cases[1].id]
    assert outcome["records"][1]["status"] == "eval_error"
    assert outcome["records"][1]["passed"] is False
    assert outcome["records"][1]["failure_reasons"] == ["eval_error"]
    assert outcome["stopped_reason"] == f"{cases[1].id}:eval_error"
    assert outcome["not_executed_cases"] == [cases[2].id]
    # The case that never ran is absent from the pass-rate denominator.
    summary = summarize_live_results(outcome["records"])
    assert summary["overall"]["scored_cases"] == 2


def test_executor_stops_when_the_model_call_budget_is_spent():
    module = _runner_module()
    cases = _cases()[:4]
    outcome = module._execute_cases(
        cases,
        run_case=lambda case: _fake_result(case, model_calls=2),
        max_model_calls=4,
        max_seconds=10,
        printer=_quiet,
    )

    assert outcome["model_calls"] == 4
    assert outcome["stopped_reason"] == "budget_exhausted"
    assert outcome["executed_ids"] == [case.id for case in cases[:2]]
    assert outcome["not_executed_cases"] == [case.id for case in cases[2:]]


def test_executor_stops_when_the_wall_clock_budget_is_spent():
    module = _runner_module()
    cases = _cases()[:3]
    ticks = iter([0.0, 0.0, 1_000.0])
    outcome = module._execute_cases(
        cases,
        run_case=lambda case: _fake_result(case),
        max_model_calls=10,
        max_seconds=500,
        clock=lambda: next(ticks, 1_000.0),
        printer=_quiet,
    )

    assert outcome["executed_ids"] == [cases[0].id]
    assert outcome["stopped_reason"] == "wall_time_budget_exhausted"
    assert outcome["not_executed_cases"] == [case.id for case in cases[1:]]


def test_executor_stops_on_a_transport_failure_status():
    module = _runner_module()
    cases = _cases()[:3]
    outcome = module._execute_cases(
        cases,
        run_case=lambda case: _fake_result(case, status="llm_unavailable"),
        max_model_calls=10,
        max_seconds=10,
        printer=_quiet,
    )

    assert outcome["executed_ids"] == [cases[0].id]
    assert outcome["stopped_reason"] == f"{cases[0].id}:llm_unavailable"
    assert outcome["not_executed_cases"] == [case.id for case in cases[1:]]


def test_executor_stops_after_three_consecutive_transport_failures():
    module = _runner_module()
    cases = _cases()[:4]
    outcome = module._execute_cases(
        cases,
        run_case=lambda case: _fake_result(case, status="llm_unavailable"),
        max_model_calls=10,
        max_seconds=10,
        printer=_quiet,
        continue_on_failure=True,
    )

    assert outcome["executed_ids"] == [case.id for case in cases[:3]]
    assert outcome["stopped_reason"] == "consecutive_transport_failures"
    assert outcome["not_executed_cases"] == [cases[3].id]


def test_executor_seeds_resumed_records_without_rerunning_them():
    module = _runner_module()
    cases = _cases()[:3]
    seeded = _fake_result(cases[0], model_calls=3).model_dump(mode="json")

    def run_case(case):
        assert case.id != cases[0].id, "a resumed case must not run again"
        return _fake_result(case)

    outcome = module._execute_cases(
        cases[1:],
        run_case=run_case,
        max_model_calls=10,
        max_seconds=10,
        printer=_quiet,
        initial_records=[seeded],
    )

    assert outcome["executed_ids"] == [case.id for case in cases]
    assert outcome["model_calls"] == 3 + 2 + 2
    assert outcome["not_executed_cases"] == []
    assert outcome["records"][0]["id"] == cases[0].id


def test_executor_can_continue_after_a_hard_failure_when_requested():
    module = _runner_module()
    cases = _cases()[:3]

    def run_case(case):
        if case.id == cases[0].id:
            return _fake_result(case, status="llm_timeout")
        return _fake_result(case)

    outcome = module._execute_cases(
        cases,
        run_case=run_case,
        max_model_calls=10,
        max_seconds=10,
        printer=_quiet,
        continue_on_failure=True,
    )

    assert outcome["executed_ids"] == [case.id for case in cases]
    assert outcome["hard_failure_ids"] == [cases[0].id]
    assert outcome["stopped_reason"] is None
    assert outcome["records"][0]["status"] == "llm_timeout"


def test_markdown_render_uses_the_archive_blocks():
    module = _runner_module()
    cases = _cases()[:3]
    outcome = module._execute_cases(
        cases,
        run_case=lambda case: _fake_result(case),
        max_model_calls=2,
        max_seconds=10,
        printer=_quiet,
    )
    settings = _settings()
    summary = summarize_live_results(
        outcome["records"],
        model="fake-offline-plan-llm",
        provider="anthropic",
        real_llm=False,
    )
    payload = {
        "generated_at": "2026-10-31T00:00:00+00:00",
        "summary": summary,
        "run": {
            "preset": "first-round-20",
            "selected_cases": 5,
            "executed_cases": len(outcome["records"]),
            "not_executed_cases": outcome["not_executed_cases"],
            "max_model_calls": 2,
            "stopped_reason": outcome["stopped_reason"],
        },
        "config": config_snapshot(settings, cases, preset="first-round-20"),
        "cost": module._cost_block(summary, settings),
        "records": outcome["records"],
    }
    text = module.render_markdown(payload)

    assert "本报告不是真实模型结果" in text
    assert "未执行用例：" in text and cases[2].id in text
    assert "budget_exhausted" in text
    assert "回答数字可溯源率" in text
    assert "调用预算上限 | 2" in text
    # The selected count must be the preset size, not the number of cases that
    # actually ran; otherwise a stopped run looks like a complete small run.
    assert "| 选中用例 | 5 |" in text
    assert f"| 实际执行 | {len(outcome['records'])} |" in text


def test_markdown_reports_dedupe_and_tokens_with_the_json_meanings():
    module = _runner_module()
    cases = _cases()[:1]
    record = _fake_result(cases[0], model_calls=1, tokens={"input": 10, "output": 5})
    record.repeated_tool_calls = 0
    record.duplicate_tool_calls = 1
    record.attempted_tool_calls = 2
    record.tool_calls = 1
    record.successful_data_tool_calls = 1
    settings = _settings()
    summary = summarize_live_results(
        [record.model_dump(mode="json")],
        model="fake-offline-plan-llm",
        provider="anthropic",
        real_llm=False,
    )
    payload = {
        "generated_at": "2026-11-02T00:00:00+00:00",
        "summary": summary,
        "run": {
            "preset": "first-round-20",
            "selected_cases": 1,
            "executed_cases": 1,
            "not_executed_cases": [],
            "max_model_calls": 10,
        },
        "config": config_snapshot(settings, cases, preset="first-round-20"),
        "cost": module._cost_block(summary, settings),
        "records": [record.model_dump(mode="json")],
    }
    text = module.render_markdown(payload)

    # The rows keep their separate meanings: plan repeats, model attempts,
    # executions, dedupe reuses, and actual data queries.
    assert "| 计划内重复工具调用 | 0 |" in text
    assert "| 模型发起的工具调用 | 2 |" in text
    assert "| 实际执行工具调用 | 1 |" in text
    assert "| 语义去重拦截（复用已有结果，无新查询） | 1 |" in text
    assert "| 成功的数据查询调用 | 1 |" in text
    assert "输入 / 输出 Token | 10 / 5 |" in text


def test_preflight_counts_agree_with_the_number_of_checks():
    scenarios = [
        {"allow_live_model": False, "max_model_calls": 0, "preset": None},
        {"allow_live_model": True, "max_model_calls": 40, "preset": None},
        {
            "allow_live_model": True,
            "max_model_calls": 40,
            "preset": "first-round-20",
        },
    ]
    for scenario in scenarios:
        readiness = live_preflight(
            _settings(llm_api_key=SECRET), _first_round(), **scenario
        )
        counts = readiness.counts()
        assert counts["total"] == len(readiness.checks)
        assert counts[PASS] + counts[WARN] + counts[FAIL] == counts["total"]
        assert {item.status for item in readiness.checks} <= {PASS, WARN, FAIL}
        # The first-round coverage check exists only for that preset.
        assert counts["total"] == (14 if scenario["preset"] else 13)


def test_preflight_separates_operator_gates_from_environment_blockers():
    readiness = live_preflight(
        _settings(llm_api_key=None),
        _first_round(),
        allow_live_model=False,
        max_model_calls=0,
    )
    assert [item.name for item in readiness.operator_gated] == [
        "authorization.live_model",
        "authorization.budget",
    ]
    assert [item.name for item in readiness.environment_blockers] == [
        "provider.api_key"
    ]
    assert readiness.blocking is True


def test_dotenv_declarations_report_names_never_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "export LLM_PROVIDER=glm\n"
        "LLM_MODEL=glm-5.3-flash\n"
        "GLM_API_KEY=sk-live-value\n"
        "ZHIPU_API_KEY=\n",
        encoding="utf-8",
    )
    declared = dotenv_llm_declarations(env)
    assert declared["provider"] == "glm"
    assert declared["model"] == "glm-5.3-flash"
    assert declared["key_names"] == ["GLM_API_KEY"]
    assert "sk-live-value" not in json.dumps(declared)


def test_preflight_explains_a_dotenv_that_was_not_loaded(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "LLM_PROVIDER=glm\nLLM_MODEL=glm-5.3-flash\nGLM_API_KEY=sk-live-value\n",
        encoding="utf-8",
    )
    readiness = live_preflight(
        _settings(llm_api_key=None),
        _first_round(),
        allow_live_model=True,
        max_model_calls=40,
        dotenv_path=env,
    )
    status = _status(readiness)
    assert status["config.dotenv"] == WARN
    assert status["provider.api_key"] == FAIL
    messages = " ".join(item.message for item in readiness.checks)
    assert "--load-dotenv" in messages
    assert "glm" in messages
    # The value is never echoed, not even in the diagnostic for a missing key.
    assert "sk-live-value" not in messages
    assert SECRET not in messages


def test_preflight_passes_when_the_dotenv_was_loaded(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "LLM_PROVIDER=anthropic\n"
        "LLM_MODEL=claude-sonnet-5-5\n"
        "ANTHROPIC_API_KEY=sk-live-value\n",
        encoding="utf-8",
    )
    readiness = live_preflight(
        _settings(llm_api_key=SECRET),
        _first_round(),
        allow_live_model=True,
        max_model_calls=40,
        dotenv_path=env,
        dotenv_loaded=True,
    )
    status = _status(readiness)
    assert status["config.dotenv"] == PASS
    assert status["provider.api_key"] == PASS
    assert readiness.blocking is False
    assert readiness.operator_gated == []


def test_get_settings_ignores_dotenv_unless_it_is_opted_in(tmp_path, monkeypatch):
    _clear_provider_env(monkeypatch)
    env = tmp_path / ".env"
    env.write_text(
        "LLM_PROVIDER=glm\n"
        "LLM_MODEL=glm-5.3-flash\n"
        "LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/\n"
        "GLM_API_KEY=sk-live-value\n",
        encoding="utf-8",
    )
    with _env_snapshot():
        default = get_settings()
        assert default.llm_provider == "anthropic"
        assert default.llm_enabled is False

        loaded = get_settings(load_dotenv=True, dotenv_path=env)
        assert loaded.llm_provider == "glm"
        assert loaded.llm_model == "glm-5.3-flash"
        assert loaded.llm_enabled is True
        assert loaded.llm_endpoint_issues() == []

    # A configured key never reaches a serialised settings dump.
    dumped = json.dumps(loaded.model_dump(mode="json"), ensure_ascii=False)
    assert "sk-live-value" not in dumped


def test_runner_load_dotenv_flag_reaches_the_preflight(
    tmp_path, monkeypatch, capsys
):
    module = _runner_module()
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=glm\n"
        "LLM_MODEL=glm-5.3-flash\n"
        "LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/\n"
        "GLM_API_KEY=sk-live-value\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "ROOT", tmp_path)
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_phase7_evals.py",
            "--preflight-only",
            "--load-dotenv",
            "--max-model-calls",
            "40",
            "--preset",
            "first-round-20",
            "--output",
            str(tmp_path / "run.json"),
        ],
    )
    with _env_snapshot():
        code = module.main()
    out = capsys.readouterr().out

    # The authorization gate is still closed, so nothing may run or be written.
    assert code == 1
    assert "dotenv: loaded .env" in out
    assert "provider='glm'" in out
    assert "PASS provider.api_key" in out
    assert "real LLM calls: 0" in out
    assert "sk-live-value" not in out
    assert not (tmp_path / "run.json").exists()


def test_cost_stays_null_when_prices_are_not_configured():
    module = _runner_module()
    case = _cases()[0]
    record = _fake_result(
        case, tokens={"input": 1_000_000, "output": 500_000}
    ).model_dump(mode="json")
    summary = summarize_live_results(
        [record], model="fake-offline-plan-llm", provider="anthropic", real_llm=False
    )

    unknown = module._cost_block(summary, _settings())
    assert unknown["estimated"] is False
    assert unknown["estimated_total"] is None
    assert unknown["estimated_per_case"] is None
    # Measured tokens are still reported; only the price-derived cost is null.
    assert unknown["tokens"] == {"input": 1_000_000, "output": 500_000}

    priced = module._cost_block(
        summary,
        _settings(
            model_input_price_per_million=2.0,
            model_output_price_per_million=8.0,
        ),
    )
    assert priced["estimated"] is True
    assert priced["estimated_total"] == 6.0
    assert priced["estimated_per_case"] == 6.0


def test_budget_guard_refuses_the_call_that_would_exceed_the_cap():
    module = _runner_module()
    forwarded: list[dict] = []

    class FakeClient:
        model = "fake-model"

        def complete_with_tools(self, **kwargs):
            forwarded.append(kwargs)
            return "ok"

    guarded = module._BudgetedClient(FakeClient(), 2)
    assert guarded.complete_with_tools(n=1) == "ok"
    assert guarded.complete_with_tools(n=2) == "ok"
    with pytest.raises(ModelCallBudgetExceeded):
        guarded.complete_with_tools(n=3)

    # The third call never reached the wrapped client.
    assert forwarded == [{"n": 1}, {"n": 2}]
    assert (guarded.sent, guarded.refused, guarded.remaining) == (2, 1, 0)
    assert guarded.model == "fake-model"


def test_budget_exhaustion_is_a_structured_stop_not_a_crash():
    # Subclassing LLMUnavailable is what turns the refusal into an
    # ``llm_unavailable`` run status instead of an unhandled exception.
    assert issubclass(ModelCallBudgetExceeded, LLMUnavailable)


def test_counting_llm_does_not_count_a_refused_call():
    from enterprise_business_agent.agent.evaluation import _CountingLLM

    class Refusing:
        model = "fake-model"

        def complete_with_tools(self, **kwargs):
            raise ModelCallBudgetExceeded("cap reached")

    counter = _CountingLLM(Refusing())
    with pytest.raises(ModelCallBudgetExceeded):
        counter.complete_with_tools()
    assert counter.calls == 0




@pytest.mark.parametrize("status", ["llm_timeout", "timeout"])
def test_executor_stops_on_either_timeout_status(status):
    module = _runner_module()
    cases = _cases()[:3]

    def run_case(case):
        result = _fake_result(case, status=status)
        return result.model_copy(update={"passed": False, "failure_reasons": [status]})

    outcome = module._execute_cases(
        cases,
        run_case=run_case,
        max_model_calls=10,
        max_seconds=10,
        printer=_quiet,
    )

    assert outcome["executed_ids"] == [cases[0].id]
    assert outcome["stopped_reason"] == f"{cases[0].id}:{status}"
    assert outcome["not_executed_cases"] == [case.id for case in cases[1:]]


def test_runner_rejects_an_out_of_range_case_budget(tmp_path):
    for value in ("0", "-5", "301"):
        completed = _run(
            "--allow-live-model", "--max-model-calls", "40",
            "--preset", "first-round-20",
            "--case-max-seconds", value,
            "--output", str(tmp_path / "run.json"),
        )
        assert completed.returncode == 3, value
        assert "--case-max-seconds must be greater than 0" in completed.stdout
        assert "no request was sent" in completed.stdout
        assert not (tmp_path / "run.json").exists()


def test_runner_applies_the_case_budget_to_preflight(tmp_path):
    completed = _run(
        "--preflight-only", "--case-max-seconds", "120",
        "--output", str(tmp_path / "run.json"),
    )
    assert "case budget: 120s per case" in completed.stdout
    assert ".env unchanged" in completed.stdout
    assert "单次 Run 上限 120.0s" in completed.stdout
    assert not (tmp_path / "run.json").exists()
