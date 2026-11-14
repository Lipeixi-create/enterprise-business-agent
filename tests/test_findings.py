from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine

from enterprise_business_agent.evidence import (
    EvidenceRecord,
    FindingEngine,
    FindingStore,
)


def _evidence(
    evidence_id: str,
    evidence_type: str,
    result: dict,
    *,
    run_id: str = "run-1",
    domain: str | None = "Sales",
    metric: str = "sales_amount",
    status: str = "valid",
    traceable: bool = True,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=run_id,
        tool_call_id=f"call-{evidence_id}",
        evidence_type=evidence_type,
        domain=domain,
        metric_code=metric,
        metric_definition_version="2.0.0",
        time_range_start="2025-12-01",
        time_range_end="2025-12-01",
        status=status,
        result=result,
        result_summary=result.get("summary"),
        traceable=traceable,
        fingerprint=hashlib.sha256(evidence_id.encode()).hexdigest(),
        created_at=datetime.now(timezone.utc),
    )


def _comparison(change_rate="0.25", *, metric="sales_amount") -> dict:
    return {
        "metric": metric,
        "label": "销售额",
        "unit": "CNY",
        "items": [
            {
                "current_period": "2025-12-01",
                "baseline_period": "2025-11-01",
                "current_value": "1250.00",
                "baseline_value": "1000.00",
                "absolute_change": "250.00",
                "change_rate": change_rate,
            }
        ],
        "explain": {"source_table": "sales_fact"},
    }


def _contribution(
    *, residual="0", total_change="-250.00", dimension="region"
) -> dict:
    return {
        "metric": "sales_amount",
        "label": "销售额",
        "dimension": dimension,
        "unit": "CNY",
        "total_current": "1000.00",
        "total_baseline": "1250.00",
        "total_change": total_change,
        "residual": residual,
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


def test_comparison_produces_change_finding_bound_to_evidence():
    evidence = _evidence("e-cmp", "comparison", _comparison())
    findings = FindingEngine().build("run-1", [evidence])

    assert len(findings) == 1
    finding = findings[0]
    assert finding.finding_type == "change"
    assert finding.evidence_ids == ["e-cmp"]
    assert finding.severity == "primary"
    assert finding.confidence == 0.9
    assert finding.confidence_basis
    assert "2025-12-01" in finding.statement


def test_small_change_is_secondary_but_still_evidence_bound():
    evidence = _evidence("e-cmp", "comparison", _comparison(change_rate="0.02"))
    finding = FindingEngine().build("run-1", [evidence])[0]
    assert finding.severity == "secondary"
    assert finding.evidence_ids == ["e-cmp"]


def test_comparison_without_change_rate_is_lower_confidence_and_flagged():
    payload = _comparison()
    payload["items"][0]["change_rate"] = None
    finding = FindingEngine().build("run-1", [_evidence("e-cmp", "comparison", payload)])[0]
    assert finding.confidence == 0.7
    assert finding.limitations


def test_contribution_finding_uses_attribution_absolute_and_relative_change():
    payload = _contribution()
    payload.update(
        status="ok",
        current_start="2025-12-01",
        current_end="2025-12-01",
        baseline_start="2025-11-01",
        baseline_end="2025-11-01",
        allocatable=True,
    )
    evidence = _evidence("e-con-full", "contribution", payload)
    finding = FindingEngine().build("run-1", [evidence])[0]

    assert finding.finding_type == "contribution"
    assert "绝对变化 -250.00" in finding.statement
    assert "相对变化率 -0.2" in finding.statement
    assert finding.confidence == 0.85
    assert finding.evidence_ids == ["e-con-full"]


def test_contribution_finding_sorts_by_absolute_contribution_and_notes_residual():
    evidence = _evidence("e-con", "contribution", _contribution(residual="30.00"))
    finding = FindingEngine().build("run-1", [evidence])[0]

    assert finding.finding_type == "contribution"
    assert finding.evidence_ids == ["e-con"]
    assert finding.confidence == 0.85
    assert finding.statement.index("EC") < finding.statement.index("NC")
    assert any("残差" in item for item in finding.limitations)


def test_ranking_is_a_fact_with_tool_numbers():
    payload = {
        "metric": "sales_amount",
        "label": "销售额",
        "dimension": "region",
        "items": [
            {"rank": 1, "dimension_value": "EC", "value": "900.00"},
            {"rank": 2, "dimension_value": "NC", "value": "600.00"},
        ],
    }
    finding = FindingEngine().build("run-1", [_evidence("e-rank", "ranking", payload)])[0]
    assert finding.finding_type == "fact"
    assert finding.severity == "context"
    assert "900.00" in finding.statement


def test_anomaly_finding_is_not_presented_as_a_business_cause():
    payload = {
        "metric": "sales_amount",
        "label": "销售额",
        "points": [
            {
                "period": "2025-12-01",
                "value": "500.00",
                "change_rate": "-0.4",
                "direction": "down",
                "rule": "zscore",
                "threshold": "3.0",
            }
        ],
    }
    finding = FindingEngine().build("run-1", [_evidence("e-anom", "anomaly", payload)])[0]
    assert finding.finding_type == "anomaly"
    assert finding.confidence == 0.75
    assert any("不是业务原因" in item for item in finding.limitations)


def test_trend_unknown_direction_produces_no_finding():
    payload = {"metric": "sales_amount", "direction": "unknown", "points": []}
    assert FindingEngine().build("run-1", [_evidence("e-trend", "trend", payload)]) == []


def test_metric_value_finding_uses_tool_summary_and_traceability():
    payload = {"metric": "sales_amount", "label": "销售额", "summary": "销售额为 1200.00"}
    traceable = FindingEngine().build(
        "run-1", [_evidence("e-val", "metric_value", payload, traceable=True)]
    )[0]
    untraceable = FindingEngine().build(
        "run-1", [_evidence("e-val2", "metric_value", payload, traceable=False)]
    )[0]
    assert traceable.confidence == 0.9
    assert untraceable.confidence == 0.6
    assert untraceable.limitations


@pytest.mark.parametrize("status", ["insufficient_data", "not_calculable", "failed", "rejected"])
def test_invalid_evidence_never_becomes_a_finding(status):
    evidence = _evidence(
        "e-bad", "comparison", _comparison(), status=status, traceable=False
    )
    assert FindingEngine().build("run-1", [evidence]) == []


def test_no_evidence_produces_no_findings():
    assert FindingEngine().build("run-1", []) == []


def test_cross_domain_relationship_is_hedged_correlation():
    left = _evidence(
        "e-sales",
        "comparison",
        _comparison(change_rate="-0.2"),
        domain="Sales",
        metric="sales_amount",
    )
    right = _evidence(
        "e-marketing",
        "contribution",
        _contribution(total_change="-100.00"),
        domain="Marketing",
        metric="marketing_roi",
    )
    findings = FindingEngine().build("run-1", [left, right])
    cross = [item for item in findings if item.finding_type in {"correlation", "hypothesis"}]
    assert len(cross) == 1
    finding = cross[0]
    assert set(finding.evidence_ids) == {"e-sales", "e-marketing"}
    assert finding.confidence in {0.3, 0.35}
    assert "可能相关" in finding.statement or "可能互相抵消" in finding.statement
    assert "不能" in finding.statement
    assert "仅为统计关联，不构成因果关系" in finding.limitations


def test_cross_domain_requires_two_different_domains():
    left = _evidence("e1", "comparison", _comparison("-0.2"), domain="Sales")
    right = _evidence("e2", "comparison", _comparison("-0.2"), domain="Sales")
    findings = FindingEngine().build("run-1", [left, right])
    assert not [
        item for item in findings if item.finding_type in {"correlation", "hypothesis"}
    ]


def test_finding_ids_are_deterministic_for_same_inputs():
    evidence = _evidence("e-cmp", "comparison", _comparison())
    first = FindingEngine().build("run-1", [evidence])[0]
    second = FindingEngine().build("run-1", [evidence])[0]
    assert first.finding_id == second.finding_id


def test_finding_store_roundtrip_and_idempotent_save(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'run_store.db'}", future=True)
    try:
        store = FindingStore(engine)
        findings = FindingEngine().build(
            "run-1",
            [
                _evidence("e-cmp", "comparison", _comparison()),
                _evidence("e-con", "contribution", _contribution()),
            ],
        )
        saved = store.save_many(findings)
        assert len(saved) == len(findings)
        store.save_many(findings)
        listed = store.list("run-1")
        assert len(listed) == len(findings)
        one = store.get("run-1", listed[0].finding_id)
        assert one is not None
        assert one.evidence_ids == listed[0].evidence_ids
        assert store.get("other-run", listed[0].finding_id) is None
    finally:
        engine.dispose()


def test_recommendation_is_advice_only():
    finding = FindingEngine().recommendation(
        "run-1",
        action="复核华东区域折扣策略",
        rationale="贡献拆解显示该区域降幅最大",
        evidence_ids=["e-con"],
    )
    assert finding.finding_type == "recommendation"
    assert "不自动执行" in " ".join(finding.limitations)
    assert any("人工" in item for item in finding.suggested_validation)
