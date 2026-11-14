from datetime import date
from decimal import Decimal

from enterprise_business_agent.analysis.comparison import (
    ComparisonStatus,
    compare_mom,
    compare_qoq,
    compare_yoy,
    compare_yoy_quarter,
    quarter_months,
)


def test_mom_uses_exact_previous_month():
    result = compare_mom("revenue", {date(2025, 1, 1): 100, date(2025, 2, 1): 120}, date(2025, 2, 1))
    assert result.status == ComparisonStatus.OK
    assert result.baseline_period == date(2025, 1, 1)
    assert result.absolute_change == Decimal("20.000000")
    assert result.change_rate == Decimal("0.200000")


def test_yoy_uses_same_month_last_year():
    result = compare_yoy(
        "revenue",
        {date(2024, 11, 1): 100, date(2025, 11, 1): 80},
        date(2025, 11, 1),
    )
    assert result.status == ComparisonStatus.OK
    assert result.baseline_period == date(2024, 11, 1)
    assert result.absolute_change == Decimal("-20.000000")
    assert result.change_rate == Decimal("-0.200000")


def test_yoy_does_not_fall_back_to_adjacent_month():
    result = compare_yoy(
        "revenue",
        {date(2024, 12, 1): 100, date(2025, 11, 1): 80},
        date(2025, 11, 1),
    )
    assert result.status == ComparisonStatus.INSUFFICIENT_DATA
    assert result.change_rate is None
    assert "2024-11-01" in (result.reason or "")


def test_qoq_sums_each_full_quarter():
    series = {
        date(2025, 7, 1): 100,
        date(2025, 8, 1): 110,
        date(2025, 9, 1): 120,
        date(2025, 10, 1): 120,
        date(2025, 11, 1): 130,
        date(2025, 12, 1): 140,
    }
    result = compare_qoq("revenue", series, 2025, 4)
    assert result.status == ComparisonStatus.OK
    assert result.current_value == Decimal("390")
    assert result.baseline_value == Decimal("330")
    assert result.absolute_change == Decimal("60.000000")
    assert result.change_rate == Decimal("0.181818")


def test_qoq_returns_insufficient_when_baseline_quarter_is_incomplete():
    series = {
        date(2025, 8, 1): 110,
        date(2025, 9, 1): 120,
        date(2025, 10, 1): 120,
        date(2025, 11, 1): 130,
        date(2025, 12, 1): 140,
    }
    result = compare_qoq("revenue", series, 2025, 4)
    assert result.status == ComparisonStatus.INSUFFICIENT_DATA
    assert result.change_rate is None
    assert "2025-07-01" in (result.reason or "")


def test_quarter_on_quarter_yoy_requires_same_quarter_last_year():
    series = {
        date(2024, 10, 1): 100,
        date(2024, 11, 1): 100,
        date(2024, 12, 1): 100,
        date(2025, 10, 1): 120,
        date(2025, 11, 1): 130,
        date(2025, 12, 1): 140,
    }
    result = compare_yoy_quarter("revenue", series, 2025, 4)
    assert result.status == ComparisonStatus.OK
    assert result.baseline_period == date(2024, 10, 1)
    assert result.current_value == Decimal("390")
    assert result.baseline_value == Decimal("300")
    assert result.change_rate == Decimal("0.300000")


def test_zero_baseline_returns_invalid_denominator_without_rate():
    result = compare_mom("revenue", {date(2025, 1, 1): 0, date(2025, 2, 1): 120}, date(2025, 2, 1))
    assert result.status == ComparisonStatus.INVALID_DENOMINATOR
    assert result.change_rate is None
    assert result.absolute_change is None


def test_semi_additive_quarter_uses_period_end_value():
    series = {
        date(2025, 7, 1): 100,
        date(2025, 8, 1): 110,
        date(2025, 9, 1): 120,
        date(2025, 10, 1): 130,
        date(2025, 11, 1): 140,
        date(2025, 12, 1): 150,
    }
    result = compare_qoq("headcount", series, 2025, 4, method="last")
    assert result.status == ComparisonStatus.OK
    assert result.current_value == Decimal("150")
    assert result.baseline_value == Decimal("120")
    assert result.change_rate == Decimal("0.250000")


def test_quarter_months_are_exact_calendar_months():
    assert quarter_months(2025, 4) == [date(2025, 10, 1), date(2025, 11, 1), date(2025, 12, 1)]
