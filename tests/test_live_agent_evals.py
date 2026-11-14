"""Offline validation of the Phase 4.1 live-eval corpus and scoring logic.

The corpus is validated against the deterministic database. The scoring test
uses a scripted planner; it checks the evaluator, never real model quality.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import AgentRuntime, AnalysisGateway, ToolRegistry
from enterprise_business_agent.agent.evaluation import (
    ScriptedPlanLLM,
    _args_match,
    _canonical_check,
    load_live_agent_cases,
    run_live_agent_case,
)
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.security import AuthorizationPolicy, Principal

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "evals" / "live_agent_cases.jsonl"
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
    path = tmp_path_factory.mktemp("live-eval") / "eval.db"
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


def test_live_corpus_has_unique_scored_and_negative_cases():
    cases = load_live_agent_cases(CASES)
    assert len(cases) >= 26
    assert len({case.id for case in cases}) == len(cases)
    scored = [case for case in cases if case.scored]
    assert len(scored) >= 20
    categories = {case.category for case in cases}
    assert {
        "metric_query",
        "yoy",
        "mom",
        "top_n",
        "cross_metric",
        "unsupported",
        "ambiguous_predictive",
    } <= categories
    for case in scored:
        assert case.required_calls
        assert case.query_check is not None
        assert case.human_check
        assert case.expect_evidence is True


def test_first_five_cover_the_required_smoke_dimensions():
    cases = load_live_agent_cases(CASES)[:5]
    categories = [case.category for case in cases]
    assert "metric_query" in categories
    assert "yoy" in categories
    assert "top_n" in categories
    assert "cross_metric" in categories
    assert "unsupported" in categories


def test_every_canonical_value_matches_the_database(eval_env):
    _, registry = eval_env
    for case in load_live_agent_cases(CASES):
        if case.query_check is None:
            continue
        correct, observed, summary, error = _canonical_check(case.query_check, registry, PRINCIPAL)
        assert error is None, (case.id, error)
        assert correct, (case.id, observed, case.query_check)
        assert summary


def test_args_match_ignores_filter_order(eval_env):
    expected = {
        "filters": [
            {"dimension": "region", "operator": "eq", "value": "EC"},
            {"dimension": "product", "operator": "eq", "value": "P002"},
        ]
    }
    actual = {
        "filters": [
            {"dimension": "product", "operator": "eq", "value": "P002"},
            {"dimension": "region", "operator": "eq", "value": "EC"},
            {"dimension": "channel", "operator": "eq", "value": "ECOM"},
        ]
    }
    assert _args_match(actual, expected) is False  # list length must match
    actual["filters"].pop()
    assert _args_match(actual, expected) is True
    assert _args_match({"n": 5, "metric": "x"}, {"n": 4}) is False


def test_live_scoring_marks_all_dimensions_with_a_scripted_planner(eval_env):
    settings, registry = eval_env
    case = load_live_agent_cases(CASES)[0]
    runtime = AgentRuntime(llm_client=None, registry=registry, settings=settings)
    runtime.llm = ScriptedPlanLLM(case.required_calls)
    result = run_live_agent_case(case, runtime=runtime, registry=registry, principal=PRINCIPAL)

    assert result.planning_correct is True
    assert result.tool_argument_correct is True
    assert result.query_correct is True
    assert result.evidence_sufficient is True
    assert result.conclusion_overreach is False
    assert result.task_completed is True
    assert result.passed is True
    assert result.scored is True


def test_live_runner_refuses_without_a_key():
    script = ROOT / "scripts" / "run_agent_live_evals.py"
    env = dict(os.environ)
    for name in ("ANTHROPIC_API_KEY", "LLM_API_KEY"):
        env.pop(name, None)
    completed = subprocess.run(
        [sys.executable, str(script), "--limit", "1"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 1
    assert "real LLM calls: 0" in completed.stdout
    assert "FAIL" in completed.stdout
    assert "sk-" not in completed.stdout
