from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from enterprise_business_agent.evidence import (
    Conclusion,
    EvidenceRecord,
    EvidenceReference,
    Finding,
    FindingEngine,
    NumericValidator,
    ReportGenerator,
    ReportStore,
    ValueReference,
)


def _evidence(evidence_id: str = "e-cmp", *, domain: str = "Sales") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id="run-1",
        tool_call_id=f"call-{evidence_id}",
        evidence_type="comparison",
        domain=domain,
        metric_code="sales_amount",
        metric_definition_version="2.0.0",
        time_range_start="2025-12-01",
        time_range_end="2025-12-01",
        baseline_period="2025-11-01",
        grain="month",
        filters=[{"dimension": "region", "operator": "eq", "value": "EC"}],
        dimensions={"region": "EC"},
        query_parameters={"metric": "sales_amount", "start": "2025-12-01", "end": "2025-12-01"},
        result={
            "metric": "sales_amount",
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
        },
        result_summary="销售额 1000.00",
        unit="CNY",
        source_tables=["sales_fact"],
        status="valid",
        fingerprint=hashlib.sha256(evidence_id.encode()).hexdigest(),
        created_at=datetime.now(timezone.utc),
        traceable=True,
    )


@pytest.fixture()
def fixture_report(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'run_store.db'}", future=True)
    try:
        store = ReportStore(engine)
        evidence = _evidence()
        findings = FindingEngine().build("run-1", [evidence])
        stored = ReportGenerator(store, narrative_enabled=False).generate(
            run_id="run-1",
            question="分析华东 2025 年 12 月销售额下降原因",
            principal_id="key:test",
            role="analyst",
            evidences=[evidence],
            findings=findings,
        )
        assert stored.validation.passed is True
        yield stored.report.model_copy(deep=True), evidence, findings
    finally:
        engine.dispose()


def _codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_baseline_report_passes(fixture_report):
    report, evidence, findings = fixture_report
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is True
    assert result.issues == []
    assert "no_unsourced_numbers" in result.checks


def test_user_objective_numbers_are_not_treated_as_report_claims(fixture_report):
    report, evidence, findings = fixture_report
    report.objective = "分析华东 2025 年 12 月销售额下降原因（目标 9999 万）"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is True, result.issues


def test_unknown_evidence_reference_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.evidence_references.append(
        EvidenceReference(
            evidence_id="does-not-exist",
            tool_call_id="call-x",
            evidence_type="metric_value",
            status="valid",
        )
    )
    result = NumericValidator().validate(report, [evidence], findings)
    assert "unknown_evidence" in _codes(result)


def test_unknown_finding_reference_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.conclusions[0].finding_ids.append("F-missing")
    result = NumericValidator().validate(report, [evidence], findings)
    assert "unknown_finding" in _codes(result)


def test_value_mismatch_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.conclusions[0].value_refs[0].value = "1.00"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "value_mismatch" in _codes(result)


def test_unresolved_field_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.conclusions[0].value_refs[0].field = "items[0].not_there"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "unresolved_value" in _codes(result)


def test_unit_mismatch_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.conclusions[0].value_refs[0].unit = "USD"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "unit_mismatch" in _codes(result)


def test_rate_reference_accepts_ratio_but_amounts_cannot(fixture_report):
    report, evidence, findings = fixture_report
    rate = next(
        ref
        for ref in report.conclusions[0].value_refs
        if ref.field.endswith("change_rate")
    )
    assert rate.unit == "ratio"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is True, result.issues

    report.conclusions[0].value_refs[0].unit = "ratio"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "unit_mismatch" in _codes(result)


def test_model_invented_number_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.executive_summary = "销售额下降 987654 元，已经确认。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "unsourced_number" in _codes(result)
    assert "987654" in result.unverified_numbers


def test_numbers_from_evidence_are_allowed_in_prose(fixture_report):
    report, evidence, findings = fixture_report
    report.executive_summary = "销售额由 1250.00 变为 1000.00，变化率 -0.2。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is True, result.issues


def test_tool_parameter_described_as_a_parameter_is_not_unsourced(fixture_report):
    report, evidence, findings = fixture_report
    evidence.query_parameters["contribution_threshold"] = "0.15"
    report.executive_summary = "贡献分析使用阈值 0.15，该值只作为工具参数。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is True, result.issues
    assert any(
        item.token == "0.15"
        and item.category == "tool_parameter"
        and item.allowed
        for item in result.numbers
    )


def test_tool_parameter_written_as_business_rate_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    evidence.query_parameters["contribution_threshold"] = "0.15"
    report.executive_summary = "贡献率为 15%，主要来自华东。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is False
    assert "tool_parameter_as_result" in _codes(result)
    assert any(
        item.token == "15"
        and item.category == "tool_parameter"
        and not item.allowed
        for item in result.numbers
    )


def test_contribution_threshold_0_2_is_a_tool_parameter(fixture_report):
    report, evidence, findings = fixture_report
    evidence.query_parameters["threshold"] = "0.2"
    report.executive_summary = "贡献分析使用阈值 0.2，仅用于筛选维度。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is True, result.issues
    assert any(
        item.token == "0.2"
        and item.category == "tool_parameter"
        and item.allowed
        for item in result.numbers
    )


def test_contribution_threshold_0_2_written_as_20_percent_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    # Keep the tool parameter as the only 0.2 in the ledger, so the assertion
    # tests provenance rather than an accidental collision with change_rate.
    evidence.result["items"][0]["change_rate"] = "-0.05"
    evidence.query_parameters["threshold"] = "0.2"
    report.executive_summary = "营销贡献率为 20%，主要来自华东。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is False
    assert "tool_parameter_as_result" in _codes(result)
    assert any(
        item.token == "20"
        and item.category == "tool_parameter"
        and item.allowed is False
        for item in result.numbers
    )


def test_year_like_amount_is_not_exempted_by_a_date_in_evidence(fixture_report):
    report, evidence, findings = fixture_report
    report.executive_summary = "新增成本 2025 元。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "unsourced_number" in _codes(result)
    assert "2025" in result.unverified_numbers


def test_date_forms_are_not_split_into_business_numbers(fixture_report):
    report, evidence, findings = fixture_report
    report.executive_summary = "观测期为 2025-06、2025-06-30 和 2025年6月。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is True, result.issues
    assert any(item.category == "date_component" for item in result.numbers)


def test_unsourced_amounts_and_ratios_are_still_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.executive_summary = "新增成本 987654.32 元，毛利率 77.7%。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is False
    assert "unsourced_number" in _codes(result)
    assert "987654.32" in result.unverified_numbers
    assert "77.7" in result.unverified_numbers
    assert all(
        item.category == "unsupported_number"
        for item in result.numbers
        if item.token in {"987654.32", "77.7"}
    )


def test_tool_parameter_used_as_business_result_without_percent_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    evidence.query_parameters["contribution_threshold"] = "0.15"
    report.executive_summary = "贡献率 0.15 表明营销贡献显著。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is False
    assert "tool_parameter_as_result" in _codes(result)


def test_hypothesis_written_as_fact_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    finding = Finding(
        finding_id="F-hyp",
        run_id="run-1",
        title="跨域假设",
        domain="Sales",
        finding_type="hypothesis",
        severity="context",
        statement="两个指标可能相关",
        evidence_ids=[evidence.evidence_id],
        confidence=0.3,
        confidence_basis="统计方向",
        created_at=datetime.now(timezone.utc),
    )
    report.conclusions.append(
        Conclusion(
            statement="两个指标存在确定关系。",
            finding_ids=["F-hyp"],
            evidence_ids=[evidence.evidence_id],
            certainty="fact",
        )
    )
    result = NumericValidator().validate(report, [evidence], findings + [finding])
    assert "hypothesis_as_fact" in _codes(result)


def test_causal_overreach_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.executive_summary = "销售额下降导致利润减少。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "causal_overreach" in _codes(result)


def test_hedged_association_is_allowed(fixture_report):
    report, evidence, findings = fixture_report
    report.executive_summary = "销售额下降可能相关，需要进一步验证。"
    result = NumericValidator().validate(report, [evidence], findings)
    assert result.passed is True, result.issues


def test_direction_contradiction_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    conclusion = report.conclusions[0]
    conclusion.statement = "销售额增长，需关注后续走势。"
    conclusion.value_refs = [
        ValueReference(
            evidence_id=evidence.evidence_id,
            field="items[0].change_rate",
            value="-0.2",
            unit="CNY",
        )
    ]
    result = NumericValidator().validate(report, [evidence], findings)
    assert "direction_contradiction" in _codes(result)


def test_scope_filter_mismatch_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.data_scope.filters = []
    result = NumericValidator().validate(report, [evidence], findings)
    assert "scope_filter_mismatch" in _codes(result)


def test_scope_time_range_mismatch_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.data_scope.time_ranges = []
    result = NumericValidator().validate(report, [evidence], findings)
    assert "scope_time_range_mismatch" in _codes(result)


def test_metric_card_mismatch_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.key_metrics[0].metric = "other_metric"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "metric_mismatch" in _codes(result)


def test_metric_card_period_outside_evidence_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    report.key_metrics[0].period = "2024-01-01"
    result = NumericValidator().validate(report, [evidence], findings)
    assert "period_mismatch" in _codes(result)


def test_cross_run_evidence_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    other = evidence.model_copy(update={"run_id": "run-2"})
    result = NumericValidator().validate(report, [other], findings)
    assert "cross_run_evidence" in _codes(result)
    assert result.passed is False


def test_cross_run_finding_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    other = findings[0].model_copy(
        update={"run_id": "run-2", "evidence_ids": [evidence.evidence_id]}
    )
    result = NumericValidator().validate(report, [evidence], [other])
    assert "cross_run_finding" in _codes(result)
    assert result.passed is False


def test_finding_with_unknown_evidence_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    other = findings[0].model_copy(update={"evidence_ids": ["e-missing"]})
    result = NumericValidator().validate(report, [evidence], [other])
    assert "finding_unknown_evidence" in _codes(result)
    assert result.passed is False


def test_duplicate_tool_call_id_is_rejected(fixture_report):
    report, evidence, findings = fixture_report
    duplicate = evidence.model_copy(
        update={"evidence_id": "e-second", "tool_call_id": evidence.tool_call_id}
    )
    result = NumericValidator().validate(report, [evidence, duplicate], findings)
    assert "duplicate_tool_call_id" in _codes(result)
    assert result.passed is False
