from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import (
    AgentRuntime,
    AnalysisGateway,
    ToolRegistry,
)
from enterprise_business_agent.agent.evaluation import load_agent_cases, run_agent_case
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.security import AuthorizationPolicy, Principal


CASES = Path(__file__).parent / "evals" / "agent_cases.jsonl"
PRINCIPAL = Principal("key:eval", "analyst", frozenset({"metrics:read", "agent:run"}))


@pytest.fixture(scope="module")
def eval_runtime(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("agent-eval")
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        agent_max_tool_calls=6,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        agent_tool_max_retries=0,
    )
    path = tmp_path / "eval.db"
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
    runtime = AgentRuntime(llm_client=None, registry=registry, settings=settings)
    try:
        yield runtime, registry
    finally:
        read_engine.dispose()
        write_engine.dispose()


def test_agent_eval_corpus_is_manually_reviewable():
    cases = load_agent_cases(CASES)
    assert len(cases) >= 6
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.question
        assert case.plan
        assert case.query_check.path
        assert case.conclusion_terms


def test_fake_planner_eval_separates_planning_query_and_conclusion(eval_runtime):
    runtime, registry = eval_runtime
    cases = load_agent_cases(CASES)
    results = [
        run_agent_case(case, runtime=runtime, registry=registry, principal=PRINCIPAL)
        for case in cases
    ]
    for item in results:
        assert item.planning_correct, item
        assert item.query_correct, item
        assert item.conclusion_correct, item
        assert item.detail["status_correct"], item
        assert item.detail["planner_is_fake"] is True

    # This is a control-flow check with a scripted fake, not real model quality.
    assert all(item.status == "completed" for item in results)
