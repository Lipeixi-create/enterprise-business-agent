from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from enterprise_business_agent.agent import (
    AgentRuntime,
    AgentStatus,
    AnalysisGateway,
    ToolRegistry,
)
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base, make_engine, make_readonly_engine
from enterprise_business_agent.evidence import Phase5Pipeline
from enterprise_business_agent.observability.setup import (
    configure_tracing,
    force_flush_tracing,
    reset_tracing,
    shutdown_tracing,
)
from enterprise_business_agent.security import AuthorizationPolicy, Principal
from tests.agent_fakes import ScriptedLLM, finish_response, tool_response

ANALYST = Principal("key:test", "analyst", frozenset({"metrics:read", "agent:run"}))
QUESTION = "分析华东 2025 年 12 月销售额下降原因"


@pytest.fixture()
def env(tmp_path):
    settings = Settings(
        data_start=date(2024, 1, 1),
        data_months=24,
        agent_max_tool_calls=4,
        agent_max_seconds=10,
        agent_tool_timeout_seconds=2,
        agent_tool_max_retries=0,
        agent_max_consecutive_failures=2,
        phase5_narrative_enabled=False,
    )
    db_path = tmp_path / "business.db"
    write_engine = make_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(write_engine)
    with Session(write_engine) as seed:
        generate_data(seed, date(2024, 1, 1), 24, reset=False)
        seed.commit()
    read_engine = make_readonly_engine(f"sqlite:///{db_path}", settings.query_timeout_ms)
    factory = sessionmaker(bind=read_engine, class_=Session, expire_on_commit=False)
    run_engine = make_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    pipeline = Phase5Pipeline(run_engine, llm=None, narrative_enabled=False)
    try:
        yield SimpleNamespace(
            settings=settings,
            factory=factory,
            pipeline=pipeline,
            run_engine=run_engine,
        )
    finally:
        read_engine.dispose()
        write_engine.dispose()
        run_engine.dispose()


def _runtime(env, llm, *, settings: Settings | None = None) -> AgentRuntime:
    settings = settings or env.settings
    return AgentRuntime(
        llm_client=llm,
        registry=ToolRegistry(
            AnalysisGateway(env.factory, settings),
            AuthorizationPolicy(settings),
            settings,
        ),
        settings=settings,
        pipeline=env.pipeline,
    )


def _compare_call(call_id: str = "c1"):
    return tool_response(
        call_id,
        "compare_periods",
        {"metric": "sales_amount", "kind": "mom", "period": "2025-12-01"},
    )


def _contribution_call(call_id: str = "c2"):
    return tool_response(
        call_id,
        "contribution",
        {
            "metric": "sales_amount",
            "dimension": "region",
            "current_start": "2025-12-01",
            "current_end": "2025-12-01",
            "baseline_start": "2025-11-01",
            "baseline_end": "2025-11-01",
        },
    )


def test_agent_persists_evidence_findings_and_report(env):
    result = _runtime(env, ScriptedLLM([_compare_call(), _contribution_call(), finish_response("完成")])).run(
        QUESTION, ANALYST, run_id="run-int-1"
    )

    assert result.status == AgentStatus.COMPLETED
    assert result.report_id
    assert result.report_version == 1
    assert result.report_status == "validated"
    assert result.report_validation_passed is True
    assert result.finding_ids

    evidences = env.pipeline.ledger.list("run-int-1")
    assert [item.evidence_type for item in evidences] == ["comparison", "contribution"]
    assert [item.tool_call_id for item in evidences] == [call.call_id for call in result.tool_calls]
    assert all(item.run_id == "run-int-1" for item in evidences)

    findings = env.pipeline.finding_store.list("run-int-1")
    assert {item.finding_id for item in findings} == set(result.finding_ids)
    assert all(item.evidence_ids for item in findings)

    stored = env.pipeline.report_store.latest("run-int-1")
    assert stored is not None
    assert stored.report.report_info.run_id == "run-int-1"
    assert {item.evidence_id for item in stored.report.evidence_references} == {item.evidence_id for item in evidences}
    assert stored.validation.passed is True


def test_insufficient_data_call_is_recorded_but_not_turned_into_finding(env):
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {
                    "metric": "sales_amount",
                    "start": "2025-12-01",
                    "end": "2025-12-01",
                    "filters": [{"dimension": "region", "operator": "eq", "value": "ZZ"}],
                },
            ),
            finish_response("没有可用数据。"),
        ]
    )
    result = _runtime(env, llm).run(QUESTION, ANALYST, run_id="run-int-empty")

    assert result.status in {
        AgentStatus.COMPLETED,
        AgentStatus.COMPLETED_WITH_WARNINGS,
        AgentStatus.NO_EVIDENCE,
        AgentStatus.NO_DATA,
    }
    evidences = env.pipeline.ledger.list("run-int-empty")
    assert len(evidences) == 1
    assert evidences[0].status == "insufficient_data"
    assert evidences[0].is_valid is False
    assert env.pipeline.finding_store.list("run-int-empty") == []

    stored = env.pipeline.report_store.latest("run-int-empty")
    assert stored is not None
    assert stored.report.conclusions == []
    assert any("没有产生有效证据" in item for item in stored.report.risks_and_uncertainty)
    assert stored.validation.passed is True


def test_failed_tool_call_keeps_error_and_cannot_be_cited_as_fact(env):
    llm = ScriptedLLM(
        [
            tool_response(
                "c1",
                "query_metric",
                {"metric": "not_a_metric", "start": "2025-12-01", "end": "2025-12-01"},
            ),
            finish_response("工具调用失败。"),
        ]
    )
    result = _runtime(env, llm).run(QUESTION, ANALYST, run_id="run-int-failed")

    evidences = env.pipeline.ledger.list("run-int-failed")
    assert len(evidences) == 1
    assert evidences[0].status == "rejected"
    assert evidences[0].error_code == "unsupported"
    assert evidences[0].error_message
    assert evidences[0].is_valid is False
    assert evidences[0].traceable is False
    assert env.pipeline.finding_store.list("run-int-failed") == []
    stored = env.pipeline.report_store.latest("run-int-failed")
    assert stored.report.evidence_references[0].status == "rejected"


def test_budget_exhaustion_reports_existing_results_and_incompleteness(env):
    settings = env.settings.model_copy(update={"agent_max_tool_calls": 1})
    llm = ScriptedLLM([_compare_call(), _contribution_call(), finish_response("完成")])
    result = _runtime(env, llm, settings=settings).run(QUESTION, ANALYST, run_id="run-int-budget")

    assert result.status == AgentStatus.BUDGET_EXHAUSTED
    assert len(env.pipeline.ledger.list("run-int-budget")) == 1
    stored = env.pipeline.report_store.latest("run-int-budget")
    assert stored is not None
    assert stored.report.conclusions
    assert any("工具调用次数已用完" in item for item in stored.report.risks_and_uncertainty)
    assert "完成剩余分析步骤后重新生成报告" in stored.report.next_validation
    assert result.report_status == "validated"


def test_phase5_failure_does_not_break_the_agent_run(env, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr(env.pipeline, "finalize", boom)
    result = _runtime(env, ScriptedLLM([finish_response("完成")])).run(QUESTION, ANALYST, run_id="run-int-p5fail")
    assert result.status in {
        AgentStatus.COMPLETED,
        AgentStatus.NO_EVIDENCE,
        AgentStatus.COMPLETED_WITH_WARNINGS,
    }
    assert any("证据账本或报告生成失败" in item for item in result.warnings)
    assert result.report_id is None


def test_one_trace_covers_decision_tool_sql_evidence_and_report(env, tmp_path):
    trace_file = tmp_path / "spans.jsonl"
    settings = env.settings.model_copy(
        update={
            "otel_enabled": True,
            "otel_exporter": "file",
            "otel_trace_file": str(trace_file),
        }
    )
    reset_tracing()
    configure_tracing(settings)
    try:
        result = _runtime(
            env,
            ScriptedLLM([_compare_call(), _contribution_call(), finish_response("完成")]),
            settings=settings,
        ).run(QUESTION, ANALYST, run_id="run-int-trace")
        assert result.status == AgentStatus.COMPLETED
        force_flush_tracing()
    finally:
        shutdown_tracing()
        reset_tracing()

    spans = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    names = {item["name"] for item in spans}
    assert {
        "agent.run",
        "agent.decision",
        "llm.complete",
        "tool.call",
        "db.query",
        "evidence.collect",
        "finding.build",
        "report.generate",
        "approval.ensure",
    } <= names
    trace_ids = {item["context"]["trace_id"] for item in spans if item["name"] != "checkpoint.restore"}
    assert len(trace_ids) == 1, f"spans were split across traces: {trace_ids}"
