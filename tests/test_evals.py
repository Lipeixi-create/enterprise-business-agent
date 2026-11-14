from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base
from enterprise_business_agent.evaluation import check_analyze, check_guard, check_plan, load_cases
from enterprise_business_agent.nl import NaturalLanguageAnalyzer, RuleBasedPlanner
from enterprise_business_agent.security import SQLGuard

CASES = load_cases(Path(__file__).parent / "evals" / "nl_cases.jsonl")


@pytest.fixture(scope="module")
def eval_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    generate_data(session, date(2024, 1, 1), 24, reset=False)
    yield session
    session.close()


@pytest.fixture(scope="module")
def eval_planner():
    return RuleBasedPlanner(Settings())


@pytest.fixture(scope="module")
def eval_analyzer(eval_session):
    return NaturalLanguageAnalyzer(eval_session, Settings())


@pytest.fixture(scope="module")
def eval_guard():
    return SQLGuard(max_rows=200, max_offset=10_000)


def test_eval_set_has_enough_cases():
    assert len(CASES) >= 30
    assert {case["category"] for case in CASES} >= {
        "metric_mapping",
        "ambiguity",
        "time_range",
        "investigation",
        "sql_injection",
    }


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_offline_eval_case(case, eval_planner, eval_analyzer, eval_guard):
    if case["kind"] == "plan":
        failures = check_plan(case, eval_planner)
    elif case["kind"] == "analyze":
        failures = check_analyze(case, eval_analyzer)
    elif case["kind"] == "guard":
        failures = check_guard(case, eval_guard)
    else:
        pytest.fail(f"unknown case kind: {case['kind']}")
    assert not failures, f"{case['id']}: {'; '.join(failures)}"
