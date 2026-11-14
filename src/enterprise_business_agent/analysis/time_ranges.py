from __future__ import annotations

import re
from datetime import date, datetime

_QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$", re.IGNORECASE)
_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_YEAR = re.compile(r"^(\d{4})$")


def shift_month(period: date, offset: int) -> date:
    year = period.year + (period.month - 1 + offset) // 12
    month = (period.month - 1 + offset) % 12 + 1
    return date(year, month, 1)


def month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def parse_period_value(value: date | datetime | str | int) -> date:
    """Parse ISO dates, YYYY-MM, YYYYQn and YYYY into a month-start date."""
    if isinstance(value, datetime):
        return date(value.year, value.month, 1)
    if isinstance(value, date):
        return month_start(value)
    if isinstance(value, int):
        return date(value, 1, 1)
    text = str(value).strip()
    match = _QUARTER.match(text)
    if match:
        year, quarter = int(match.group(1)), int(match.group(2))
        return date(year, (quarter - 1) * 3 + 1, 1)
    match = _MONTH.match(text)
    if match:
        return date(int(match.group(1)), int(match.group(2)), 1)
    match = _YEAR.match(text)
    if match:
        return date(int(match.group(1)), 1, 1)
    parsed = date.fromisoformat(text)
    return month_start(parsed)


def parse_period_end_value(value: date | datetime | str | int) -> date:
    """Parse a period expression as an inclusive end month.

    `2025Q4` means through December, `2025` means through December, while
    explicit dates keep their own month.
    """
    if isinstance(value, datetime):
        return date(value.year, value.month, 1)
    if isinstance(value, date):
        return month_start(value)
    if isinstance(value, int):
        return date(value, 12, 1)
    text = str(value).strip()
    match = _QUARTER.match(text)
    if match:
        year, quarter = int(match.group(1)), int(match.group(2))
        return date(year, quarter * 3, 1)
    match = _YEAR.match(text)
    if match:
        return date(int(match.group(1)), 12, 1)
    return parse_period_value(text)


def month_sequence(start: date, end: date) -> list[date]:
    start = month_start(start)
    end = month_start(end)
    if start > end:
        return []
    months: list[date] = []
    current = start
    while current <= end:
        months.append(current)
        current = shift_month(current, 1)
    return months


def quarter_start(value: date) -> date:
    value = month_start(value)
    return date(value.year, (value.month - 1) // 3 * 3 + 1, 1)


def previous_quarter_start(value: date) -> date:
    return shift_month(quarter_start(value), -3)


def quarter_months(year: int, quarter: int) -> list[date]:
    if quarter not in (1, 2, 3, 4):
        raise ValueError("quarter must be 1-4")
    first = (quarter - 1) * 3 + 1
    return [date(year, first + offset, 1) for offset in range(3)]


def quarter_label(value: date) -> str:
    return f"{value.year}Q{(value.month - 1) // 3 + 1}"


def bucket_key(value: date, grain: str):
    value = month_start(value)
    if grain == "month":
        return value
    if grain == "quarter":
        return (value.year, (value.month - 1) // 3 + 1)
    if grain == "year":
        return value.year
    if grain == "total":
        return "total"
    raise ValueError(f"unsupported time grain: {grain}")


def bucket_label(key, grain: str, start: date | None = None, end: date | None = None) -> str:
    if grain == "month":
        return key.strftime("%Y-%m")
    if grain == "quarter":
        return f"{key[0]}Q{key[1]}"
    if grain == "year":
        return str(key)
    if grain == "total":
        if start is None or end is None:
            return "total"
        return f"{start:%Y-%m}~{end:%Y-%m}"
    raise ValueError(f"unsupported time grain: {grain}")


def missing_months(present: set[date], start: date, end: date) -> list[date]:
    return [month for month in month_sequence(start, end) if month not in present]
