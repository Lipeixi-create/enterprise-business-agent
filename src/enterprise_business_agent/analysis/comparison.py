from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ComparisonKind(str, Enum):
    MOM = "mom"
    QOQ = "qoq"
    YOY = "yoy"


class ComparisonStatus(str, Enum):
    OK = "ok"
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_DENOMINATOR = "invalid_denominator"


class ComparisonResult(BaseModel):
    metric: str
    kind: ComparisonKind
    current_period: date
    baseline_period: date | None = None
    current_value: Decimal | None = None
    baseline_value: Decimal | None = None
    absolute_change: Decimal | None = None
    change_rate: Decimal | None = None
    status: ComparisonStatus
    reason: str | None = None


def shift_month(period: date, offset: int) -> date:
    year = period.year + (period.month - 1 + offset) // 12
    month = (period.month - 1 + offset) % 12 + 1
    return date(year, month, 1)


def previous_month(period: date) -> date:
    return shift_month(period, -1)


def same_month_last_year(period: date) -> date:
    return date(period.year - 1, period.month, 1)


def quarter_start(period: date) -> date:
    return date(period.year, (period.month - 1) // 3 * 3 + 1, 1)


def previous_quarter_start(period: date) -> date:
    return shift_month(quarter_start(period), -3)


def same_quarter_last_year(period: date) -> date:
    start = quarter_start(period)
    return date(start.year - 1, start.month, 1)


def quarter_months(year: int, quarter: int) -> list[date]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError("quarter must be 1-4")
    first_month = (quarter - 1) * 3 + 1
    return [date(year, first_month + offset, 1) for offset in range(3)]


def _to_decimal(value: Decimal | int | float | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _value_at(series: Mapping[date, Decimal | int | float | str | None], period: date) -> Decimal | None:
    if period not in series or series[period] is None:
        return None
    return _to_decimal(series[period])


def _aggregate(
    series: Mapping[date, Decimal | int | float | str | None],
    months: list[date],
    method: Literal["sum", "last"],
) -> tuple[Decimal | None, list[date]]:
    missing = [month for month in months if _value_at(series, month) is None]
    if missing:
        return None, missing
    values = [_value_at(series, month) for month in months]
    if method == "sum":
        return sum(values, Decimal("0")), []
    if method == "last":
        return values[-1], []
    raise ValueError("method must be 'sum' or 'last'")


def compare_values(
    metric: str,
    kind: ComparisonKind,
    current_period: date,
    baseline_period: date,
    current_value: Decimal | int | float | str | None,
    baseline_value: Decimal | int | float | str | None,
) -> ComparisonResult:
    if current_value is None or baseline_value is None:
        return ComparisonResult(
            metric=metric,
            kind=kind,
            current_period=current_period,
            baseline_period=baseline_period,
            current_value=None if current_value is None else _to_decimal(current_value),
            baseline_value=None if baseline_value is None else _to_decimal(baseline_value),
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason="current or baseline value is missing",
        )
    current = _to_decimal(current_value)
    baseline = _to_decimal(baseline_value)
    if baseline == 0:
        return ComparisonResult(
            metric=metric,
            kind=kind,
            current_period=current_period,
            baseline_period=baseline_period,
            current_value=current,
            baseline_value=baseline,
            status=ComparisonStatus.INVALID_DENOMINATOR,
            reason="baseline value is zero; change rate is undefined",
        )
    absolute_change = current - baseline
    return ComparisonResult(
        metric=metric,
        kind=kind,
        current_period=current_period,
        baseline_period=baseline_period,
        current_value=current,
        baseline_value=baseline,
        absolute_change=_quantize(absolute_change),
        change_rate=_quantize(absolute_change / baseline),
        status=ComparisonStatus.OK,
    )


def compare_mom(
    metric: str,
    series: Mapping[date, Decimal | int | float | str | None],
    period: date,
) -> ComparisonResult:
    baseline_period = previous_month(period)
    current_value = _value_at(series, period)
    baseline_value = _value_at(series, baseline_period)
    if current_value is None:
        return ComparisonResult(
            metric=metric,
            kind=ComparisonKind.MOM,
            current_period=period,
            baseline_period=baseline_period,
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason=f"current period {period.isoformat()} has no data",
        )
    if baseline_value is None:
        return ComparisonResult(
            metric=metric,
            kind=ComparisonKind.MOM,
            current_period=period,
            baseline_period=baseline_period,
            current_value=current_value,
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason=f"baseline period {baseline_period.isoformat()} has no data",
        )
    return compare_values(metric, ComparisonKind.MOM, period, baseline_period, current_value, baseline_value)


def compare_yoy(
    metric: str,
    series: Mapping[date, Decimal | int | float | str | None],
    period: date,
) -> ComparisonResult:
    baseline_period = same_month_last_year(period)
    current_value = _value_at(series, period)
    baseline_value = _value_at(series, baseline_period)
    if current_value is None:
        return ComparisonResult(
            metric=metric,
            kind=ComparisonKind.YOY,
            current_period=period,
            baseline_period=baseline_period,
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason=f"current period {period.isoformat()} has no data",
        )
    if baseline_value is None:
        return ComparisonResult(
            metric=metric,
            kind=ComparisonKind.YOY,
            current_period=period,
            baseline_period=baseline_period,
            current_value=current_value,
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason=f"baseline period {baseline_period.isoformat()} has no data",
        )
    return compare_values(metric, ComparisonKind.YOY, period, baseline_period, current_value, baseline_value)


def compare_qoq(
    metric: str,
    series: Mapping[date, Decimal | int | float | str | None],
    year: int,
    quarter: int,
    method: Literal["sum", "last"] = "sum",
) -> ComparisonResult:
    current_months = quarter_months(year, quarter)
    current_period = current_months[0]
    baseline_start = previous_quarter_start(current_period)
    baseline_months = quarter_months(baseline_start.year, (baseline_start.month - 1) // 3 + 1)
    current_value, missing_current = _aggregate(series, current_months, method)
    if missing_current:
        return ComparisonResult(
            metric=metric,
            kind=ComparisonKind.QOQ,
            current_period=current_period,
            baseline_period=baseline_start,
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason="current quarter is missing months: " + ", ".join(item.isoformat() for item in missing_current),
        )
    baseline_value, missing_baseline = _aggregate(series, baseline_months, method)
    if missing_baseline:
        return ComparisonResult(
            metric=metric,
            kind=ComparisonKind.QOQ,
            current_period=current_period,
            baseline_period=baseline_start,
            current_value=current_value,
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason="baseline quarter is missing months: " + ", ".join(item.isoformat() for item in missing_baseline),
        )
    return compare_values(metric, ComparisonKind.QOQ, current_period, baseline_start, current_value, baseline_value)


def compare_yoy_quarter(
    metric: str,
    series: Mapping[date, Decimal | int | float | str | None],
    year: int,
    quarter: int,
    method: Literal["sum", "last"] = "sum",
) -> ComparisonResult:
    current_months = quarter_months(year, quarter)
    current_period = current_months[0]
    baseline_months = quarter_months(year - 1, quarter)
    baseline_period = baseline_months[0]
    current_value, missing_current = _aggregate(series, current_months, method)
    if missing_current:
        return ComparisonResult(
            metric=metric,
            kind=ComparisonKind.YOY,
            current_period=current_period,
            baseline_period=baseline_period,
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason="current quarter is missing months: " + ", ".join(item.isoformat() for item in missing_current),
        )
    baseline_value, missing_baseline = _aggregate(series, baseline_months, method)
    if missing_baseline:
        return ComparisonResult(
            metric=metric,
            kind=ComparisonKind.YOY,
            current_period=current_period,
            baseline_period=baseline_period,
            current_value=current_value,
            status=ComparisonStatus.INSUFFICIENT_DATA,
            reason="baseline quarter is missing months: " + ", ".join(item.isoformat() for item in missing_baseline),
        )
    return compare_values(metric, ComparisonKind.YOY, current_period, baseline_period, current_value, baseline_value)
