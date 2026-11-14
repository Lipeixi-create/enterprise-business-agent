from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from enterprise_business_agent.evidence import (
    EvidenceRecord,
    FindingEngine,
    ReportGenerator,
    ReportStore,
)
from enterprise_business_agent.llm import LLMTruncated


def _evidence(
    evidence_id: str,
    *,
    run_id: str = "run-1",
    evidence_type: str = "comparison",
    metric: str = "sales_amount",
    status: str = "valid",
) -> EvidenceRecord:
    if evidence_type == "comparison":
        result = {
            "metric": metric,
            "label": "销售额",
            "unit": "CNY",
            "items": [
                {
                    "current_period": "2025-12-01",
                    "baseline_period": "2025-11-01",
                    "current_value": "1000.00",
                    "baseline_value": "1250.00",
                    "absolute_change": "-250.00",
                    "change_rate": "-0.2",
                }
            ],
            "explain": {"source_table": "sales_fact"},
        }
    else:
        result = {
            "metric": metric,
            "label": "销售额",
            "dimension": "region",
            "unit": "CNY",
            "total_current": "1000.00",
            "total_baseline": "1250.00",
            "total_change": "-250.00",
            "residual": "0",
            "items": [
                {
                    "dimension_value": "EC",
                    "current_value": "600.00",
                    "baseline_value": "900.00",
                    "absolute_change": "-300.00",
                    "contribution": "-300.00",
                    "contribution_rate": "1.2",
                    "direction": "decrease",
                },
                {
                    "dimension_value": "NC",
                    "current_value": "400.00",
                    "baseline_value": "350.00",
                    "absolute_change": "50.00",
                    "contribution": "50.00",
                    "contribution_rate": "-0.2",
                    "direction": "increase",
                },
            ],
            "explain": {"source_table": "sales_fact"},
        }
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=run_id,
        tool_call_id=f"call-{evidence_id}",
        evidence_type=evidence_type,
        domain="Sales",
        metric_code=metric,
        metric_definition_version="2.0.0",
        time_range_start="2025-12-01",
        time_range_end="2025-12-01",
        baseline_period="2025-11-01",
        grain="month",
        filters=[{"dimension": "region", "operator": "eq", "value": "EC"}],
        dimensions={"region": "EC"},
        query_parameters={"metric": metric, "start": "2025-12-01", "end": "2025-12-01"},
        result=result,
        result_summary="销售额 1000.00",
        unit="CNY",
        source_tables=["sales_fact"],
        calculation_method="deterministic period comparison",
        status=status,
        fingerprint=hashlib.sha256(evidence_id.encode()).hexdigest(),
        created_at=datetime.now(timezone.utc),
        traceable=status == "valid",
    )


class _NarrativeLLM:
    """Section-aware fake: the generator now asks for one piece at a time."""

    model = "fake-narrative"

    def __init__(
        self,
        summary: str = "",
        domains: dict[str, str] | None = None,
        *,
        error: Exception | None = None,
    ):
        self.summary = summary
        self.domains = dict(domains or {})
        self.error = error
        self.calls: list[dict] = []

    def complete_json(self, *, system: str, prompt: str, max_tokens: int | None = None):
        self.calls.append({"system": system, "prompt": prompt, "max_tokens": max_tokens})
        if self.error is not None:
            raise self.error
        if "执行摘要" in system:
            return SimpleNamespace(data={"executive_summary": self.summary})
        domain = prompt.rsplit("业务域：", 1)[-1].strip()
        return SimpleNamespace(data={"narrative": self.domains.get(domain, "")})


@pytest.fixture()
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'run_store.db'}", future=True)
    try:
        yield ReportStore(engine)
    finally:
        engine.dispose()


def _generate(store, *, evidences=None, findings=None, llm=None, narrative=False, fixes=1):
    evidences = (
        [_evidence("e-cmp"), _evidence("e-con", evidence_type="contribution")] if evidences is None else evidences
    )
    findings = FindingEngine().build("run-1", evidences) if findings is None else findings
    generator = ReportGenerator(
        store,
        llm=llm,
        narrative_enabled=narrative,
        max_fix_attempts=fixes,
    )
    return generator.generate(
        run_id="run-1",
        question="分析华东 2025 年 12 月销售额下降原因",
        principal_id="key:test",
        role="analyst",
        evidences=evidences,
        findings=findings,
        provider="glm",
        model="glm-test",
        prompt_version="phase5-v1",
    )


def test_report_has_all_required_sections_and_links_to_run(store):
    stored = _generate(store)
    report = stored.report

    assert report.report_info.run_id == "run-1"
    assert report.report_info.version == 1
    assert report.report_info.question.startswith("分析华东")
    assert report.objective
    assert report.conclusions
    assert report.key_metrics
    assert report.domain_analysis
    assert report.anomalies_and_contributions
    assert report.cross_domain == []
    assert report.evidence_references
    assert report.risks_and_uncertainty
    assert report.recommendations
    assert report.next_validation
    assert report.data_scope.metric_codes == ["sales_amount"]
    assert report.data_scope.metric_definition_versions == ["2.0.0"]
    assert stored.status == "validated"
    assert stored.validation.passed is True


def test_conclusions_cite_findings_and_evidence(store):
    stored = _generate(store)
    findings = FindingEngine().build(
        "run-1",
        [_evidence("e-cmp"), _evidence("e-con", evidence_type="contribution")],
    )
    known_evidence = {"e-cmp", "e-con"}
    known_findings = {item.finding_id for item in findings}

    for conclusion in stored.report.conclusions:
        assert conclusion.finding_ids
        assert set(conclusion.finding_ids) <= known_findings
        for evidence_id in conclusion.evidence_ids:
            assert evidence_id in known_evidence
        for ref in conclusion.value_refs:
            assert ref.evidence_id in known_evidence


def test_every_report_number_is_a_value_reference(store):
    stored = _generate(store)
    for card in stored.report.key_metrics:
        assert card.value.field
        assert card.value.value
        if card.change:
            assert card.change.field
    for conclusion in stored.report.conclusions:
        assert conclusion.value_refs, conclusion.statement


def test_rate_references_are_dimensionless_not_currency(store):
    stored = _generate(store)
    refs = list(stored.report.key_metrics)
    assert refs
    for card in refs:
        if card.change and card.change.field.endswith("change_rate"):
            assert card.change.unit == "ratio"
    for conclusion in stored.report.conclusions:
        for ref in conclusion.value_refs:
            if ref.field.endswith("_rate"):
                assert ref.unit == "ratio", (ref.field, ref.unit)


def test_recommendations_are_advisory_and_require_approval(store):
    stored = _generate(store)
    assert stored.report.recommendations
    for item in stored.report.recommendations:
        assert item.requires_approval is True
        assert item.evidence_ids
        assert item.rationale
        assert item.expected_direction
        assert item.risk
        assert item.validation_metric


def test_evidence_references_include_invalid_evidence_but_no_foreign_runs(store):
    evidences = [
        _evidence("e-cmp"),
        _evidence("e-bad", status="insufficient_data"),
    ]
    stored = _generate(store, evidences=evidences, findings=[])
    ids = [item.evidence_id for item in stored.report.evidence_references]
    assert "e-cmp" in ids and "e-bad" in ids
    assert all(item.status in {"valid", "insufficient_data"} for item in stored.report.evidence_references)


def test_report_is_append_only_and_versions_increase(store):
    first = _generate(store)
    second = _generate(store)
    assert first.version == 1
    assert second.version == 2
    assert first.report.report_info.report_id != second.report.report_info.report_id
    assert [item.version for item in store.versions("run-1")] == [1, 2]
    assert store.get("run-1", 1).report.report_info.report_id == first.report.report_info.report_id
    assert store.get("other-run", 1) is None


def test_no_valid_evidence_report_states_insufficient_data(store):
    stored = _generate(store, evidences=[], findings=[])
    report = stored.report
    assert report.conclusions == []
    assert "数据不足" in report.executive_summary or "不足以" in report.executive_summary
    assert any("没有产生有效证据" in item for item in report.risks_and_uncertainty)
    assert any("补充" in item for item in report.next_validation)
    assert stored.validation.passed is True


def test_llm_narrative_used_when_it_passes_validation(store):
    llm = _NarrativeLLM(
        summary="销售额出现变化，需结合业务事件进一步验证。",
        domains={"Sales": "销售额在当期发生明显变化，原因仍需验证。"},
    )
    stored = _generate(store, llm=llm, narrative=True)
    assert stored.report.narrative_mode == "llm"
    assert "结合业务事件" in stored.report.executive_summary
    assert stored.status == "validated"
    assert llm.calls
    # Summary and domain text are separate, independently bounded requests.
    assert {call["max_tokens"] for call in llm.calls} == {800, 700}


def test_invented_number_in_narrative_is_rejected_and_falls_back(store):
    llm = _NarrativeLLM(
        summary="销售额下降 987654 元，已经确认。",
        domains={"Sales": "销售额下降 987654 元，已经确认。"},
    )
    stored = _generate(store, llm=llm, narrative=True, fixes=1)
    assert stored.report.narrative_mode == "deterministic"
    assert "987654" not in stored.report.executive_summary
    assert stored.report.warnings
    assert "未通过数值校验" in stored.report.warnings[0]
    assert stored.validation.passed is True
    # Summary plus one domain, each with one bounded fix attempt.
    assert len(llm.calls) == 4


def test_partial_narrative_keeps_valid_sections_and_warns(store):
    llm = _NarrativeLLM(
        summary="销售额出现变化，需结合业务事件进一步验证。",
        domains={},  # every domain request returns an empty narrative
    )
    stored = _generate(store, llm=llm, narrative=True, fixes=0)
    assert stored.report.narrative_mode == "partial"
    assert "结合业务事件" in stored.report.executive_summary
    assert any("domain:Sales" in item for item in stored.report.warnings)
    assert stored.status == "validated"


def test_truncated_narrative_is_reported_as_truncation(store):
    llm = _NarrativeLLM(error=LLMTruncated("reached the output token cap", status_code=None))
    stored = _generate(store, llm=llm, narrative=True)
    assert stored.report.narrative_mode == "deterministic"
    assert any("截断" in item for item in stored.report.warnings)
    assert stored.validation.passed is True


def test_unavailable_narrative_falls_back_with_accurate_warning(store):
    llm = _NarrativeLLM(error=RuntimeError("provider unavailable"))
    stored = _generate(store, llm=llm, narrative=True)
    assert stored.report.narrative_mode == "deterministic"
    assert len(stored.report.warnings) == 1
    assert "模型叙述生成失败" in stored.report.warnings[0]
    assert "RuntimeError" in stored.report.warnings[0]
    assert stored.validation.passed is True
