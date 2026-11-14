from __future__ import annotations

from datetime import date
from decimal import Decimal

from enterprise_business_agent.analysis.attribution import (
    attribution_from_evidence,
    build_attribution,
)
from enterprise_business_agent.analysis.contracts import (
    AnalysisStatus,
    ContributionItem,
    ContributionResponse,
)


def _response(**overrides) -> ContributionResponse:
    payload = dict(
        status=AnalysisStatus.OK,
        metric="sales_amount",
        label="销售额",
        dimension="region",
        unit="CNY",
        current_start=date(2025, 12, 1),
        current_end=date(2025, 12, 1),
        baseline_start=date(2025, 11, 1),
        baseline_end=date(2025, 11, 1),
        total_current=Decimal("1000.00"),
        total_baseline=Decimal("1250.00"),
        total_change=Decimal("-250.00"),
        allocatable=True,
        items=[
            ContributionItem(
                dimension_value="NC",
                current_value=Decimal("400.00"),
                baseline_value=Decimal("350.00"),
                absolute_change=Decimal("50.00"),
                contribution=Decimal("50.00"),
                contribution_rate=Decimal("-0.2"),
                direction="increase",
            ),
            ContributionItem(
                dimension_value="EC",
                current_value=Decimal("600.00"),
                baseline_value=Decimal("900.00"),
                absolute_change=Decimal("-300.00"),
                contribution=Decimal("-300.00"),
                contribution_rate=Decimal("1.2"),
                direction="decrease",
            ),
        ],
        sum_contributions=Decimal("-250.00"),
        residual=Decimal("0"),
    )
    payload.update(overrides)
    return ContributionResponse(**payload)


def test_attribution_distinguishes_absolute_relative_and_contribution_rate():
    result = build_attribution(_response())

    assert result.status == AnalysisStatus.OK
    assert result.absolute_change == Decimal("-250.00")
    assert result.relative_change == Decimal("-0.2")
    assert result.unit == "CNY"
    assert result.allocatable is True
    assert result.total_current == Decimal("1000.00")


def test_items_are_sorted_by_absolute_contribution_without_changing_values():
    result = build_attribution(_response())
    assert [item.dimension_value for item in result.items] == ["EC", "NC"]
    assert result.items[0].contribution == Decimal("-300.00")
    assert result.items[0].contribution_rate == Decimal("1.2")
    assert result.items[0].direction == "decrease"


def test_zero_baseline_is_explicitly_not_calculable():
    result = build_attribution(_response(total_baseline=Decimal("0"), total_change=None))
    assert result.relative_change is None
    assert "baseline_total" in result.missing_data
    assert any("基准期总量为 0 或缺失" in item for item in result.checks)


def test_missing_baseline_total_is_reported():
    result = build_attribution(_response(total_baseline=None))
    assert result.relative_change is None
    assert "baseline_total" in result.missing_data


def test_residual_beyond_tolerance_is_flagged():
    result = build_attribution(_response(residual=Decimal("10.00")))
    assert any("残差" in item for item in result.checks)
    assert any("维度成员可能不完整" in item for item in result.checks)


def test_residual_within_one_percent_is_reported_as_consistent():
    result = build_attribution(_response(residual=Decimal("1.00")))
    assert any("1% 容差内一致" in item for item in result.checks)


def test_non_allocatable_metric_marks_attribution_unavailable():
    result = build_attribution(_response(allocatable=False))
    assert "additive_metric" in result.missing_data
    assert any("不是可加指标" in item for item in result.checks)


def test_ok_without_items_becomes_insufficient_data():
    result = build_attribution(_response(items=[]))
    assert result.status == AnalysisStatus.INSUFFICIENT_DATA
    assert result.reason
    assert "contribution_items" in result.missing_data


def test_attribution_from_evidence_roundtrip():
    payload = _response().model_dump(mode="json")
    result = attribution_from_evidence(payload)
    assert result is not None
    assert result.relative_change == Decimal("-0.2")
    assert [item.dimension_value for item in result.items] == ["EC", "NC"]


def test_attribution_from_invalid_evidence_returns_none():
    assert attribution_from_evidence({}) is None
    assert attribution_from_evidence({"metric": "sales_amount"}) is None


def test_attribution_does_not_invent_missing_numbers():
    result = build_attribution(
        _response(items=[ContributionItem(dimension_value="EC")])
    )
    assert result.items[0].contribution is None
    assert result.items[0].absolute_change is None
