"""Deterministic checks that relational claims match the tool data the model saw.

The checks are intentionally narrow: a claim is only judged when the answer
names a dimension member that exists in a trusted, model-visible dataset and
the compared field can be identified. That keeps the checks reproducible and
avoids turning ordinary prose into failures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

_MAX_MESSAGE_ROWS = 50

_SUPERLATIVE_MAX = (
    "最大",
    "最高",
    "最多",
    "最强",
    "居首",
    "榜首",
    "首位",
    "排名第一",
    "位列第一",
    "排在第一",
    "highest",
    "largest",
    "maximum",
    "most",
)
_SUPERLATIVE_MIN = (
    "最小",
    "最低",
    "最少",
    "最弱",
    "垫底",
    "末位",
    "lowest",
    "smallest",
    "minimum",
    "least",
)
_UNIQUE_MARKERS = ("唯一", "仅一个", "只有一个", "仅有", "unique")
_FIELD_HINTS = (
    ("采购金额", "numerator"),
    ("采购额", "numerator"),
    ("采购数量", "denominator"),
    ("采购量", "denominator"),
    ("投入金额", "denominator"),
    ("投入", "denominator"),
    ("花费", "denominator"),
    ("分母", "denominator"),
    ("分子", "numerator"),
    ("denominator", "denominator"),
    ("numerator", "numerator"),
    ("贡献率", "contribution_rate"),
    ("贡献", "contribution"),
    ("contribution_rate", "contribution_rate"),
    ("contribution", "contribution"),
    ("变化率", "change_rate"),
    ("变化", "absolute_change"),
    ("下降", "absolute_change"),
    ("下滑", "absolute_change"),
    ("减少", "absolute_change"),
    ("降幅", "absolute_change"),
    ("跌幅", "absolute_change"),
    ("absolute_change", "absolute_change"),
    ("change_rate", "change_rate"),
    ("change", "absolute_change"),
    ("当前值", "current_value"),
    ("本期", "current_value"),
    ("基期", "baseline_value"),
    ("上期", "baseline_value"),
    ("current_value", "current_value"),
    ("baseline_value", "baseline_value"),
)
_VALUE_HINTS = (
    "天数",
    "金额",
    "收入",
    "利润",
    "数量",
    "数值",
    "占比",
    "值",
    "days",
    "amount",
    "revenue",
    "profit",
    "value",
    "count",
)
# A claim that the whole population has an extremum cannot be verified from a
# truncated result, even when the displayed rows happen to be ordered.
_GLOBAL_MARKERS = (
    "全局",
    "全部",
    "所有",
    "整体",
    "总体",
    "全量",
    "各产品",
    "各区域",
    "全部产品",
    "全部区域",
    "all ",
    "every",
    "entire",
)
_CONTRAST_TERMS = (
    "次之",
    "其次",
    "第二",
    "仅次于",
    "次高",
    "次低",
    "second",
    "next",
)
# "最大拖累"/"最大降幅" is the most negative contribution/change, not the
# algebraically largest value.
_NEGATIVE_EXTREMA = (
    "拖累",
    "降幅",
    "跌幅",
    "减少",
    "下滑",
    "下降",
    "负向",
    "亏损",
    "least favorable",
)
_NEGATION_SUFFIXES = (
    "不是",
    "并非",
    "并不是",
    "而非",
    "不能称为",
    "不能算作",
    "不能说是",
    "不算",
    "称不上",
)
_COUNT_RE = re.compile(r"(?:共|合计|总计|一共)\s*(\d{1,4})\s*(?:个|条|项|名|种)")
_RANK_RE = re.compile(
    r"(?:排名|位列|排第|第)\s*(?:第)?\s*([0-9]{1,3}|[一二三四五六七八九十])\s*(?:名|位)?"
    r"(?!\s*(?:季度|月|年|周|日|天|期|度))"
)
_RANK_COMPARISON_PREFIX_RE = re.compile(r"(?:与|和|跟|同|比|较|相比|对比|相对于)\s*$")
_RANK_COMPARISON_SUFFIX_RE = re.compile(r"^\s*(?:相差|差距|相比|对比|之差|相减|低|高|少|多)")
_RANK_RANGE_PREFIX_RE = re.compile(r"(?:至|到|~|～|—|–|-)\s*(?:第)?\s*$")
_RANK_RANGE_SUFFIX_RE = re.compile(r"^\s*(?:至|到|~|～|—|–|-)\s*(?:第)?\s*(?:[0-9]{1,3}|[一二三四五六七八九十])")
# "第一名与第四名相差 0.007" compares two ranks; it does not claim the
# sentence's subject is either rank.
_RANK_PAIR_COMPARISON_RE = re.compile(
    r"^\s*(?:与|和|跟|同|及)\s*(?:第)?\s*(?:[0-9]{1,3}|[一二三四五六七八九十]+)"
    r"\s*(?:名|位)\s*(?:相差|差距|相比|对比|之差|相减|低|高|少|多|更)"
)
# "按排名列第二" states the ranked subject; the member is before the idiom.
_RANK_LIST_VERB_RE = re.compile(r"(?:按|根据|依据|在)?(?:排名|排序|名次)(?:中)?\s*(?:列|排|位|居)\s*$")
_RANKING_CUE_RE = re.compile(
    r"(?:排名|排序|名次|从高到低|从低到高|由高到低|由低到高|依次|顺次|"
    r"高于|低于|大于|小于|优于|劣于|领先|落后|超过|"
    r"第\s*[一二三四五六七八九十0-9]+\s*名|top\s*\d|bottom\s*\d)",
    re.IGNORECASE,
)
_FIELD_COMPARISON_PREFIX_RE = re.compile(r"(?:低于|高于|大于|小于|少于|多于|超过|不及|相当于|不足)\s*$")
_NUMBER_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")
_MEMBER_CODE_RE = re.compile(r"\b[A-Z]{1,4}\d{2,8}\b")
_SEGMENT_RE = re.compile(r"[。！？\n；;]+")
_CLAUSE_BREAK_RE = re.compile(r"[，,。！？；;：:]")
_BLOCK_RE = re.compile(r"\n\s*\n")
_CLAUSE_SEPARATORS = "，,：:"
_PARENTHETICAL_RE = re.compile(r"[（(][^）)]*[）)]")
# "最低的三个活动" / "相对最低的三名" rank a set; they do not claim that
# every named member is the extremum.
_TOP_N_TAIL_RE = re.compile(
    r"^\s*的?\s*(?:前|后|第)?\s*(?:[一二三四五六七八九十两]|\d+)\s*"
    r"(?:名|个|项|位|种|家|条|只)"
)
_TOP_N_PREFIX_RE = re.compile(
    r"(?:top|bottom|first|last|\b(?:one|two|three|four|five|six|seven|eight|"
    r"nine|ten|\d+))\s+$",
    re.IGNORECASE,
)
_LIST_CONNECTOR_RE = re.compile(r"^[\s、,，和与及以及&/+]*$")
# Words that may sit between two names that share one superlative claim but do
# not end the claim: "P003 and P009 have the highest ...".
_ENGLISH_FILLER = frozenset(
    {
        "the",
        "a",
        "an",
        "both",
        "and",
        "have",
        "has",
        "had",
        "are",
        "is",
        "was",
        "were",
        "with",
        "by",
        "of",
        "records",
        "record",
        "rows",
        "row",
        "values",
        "value",
        "products",
        "product",
    }
)
_CHINESE_RANK = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_ORDINAL_ADJECTIVES = frozenset("高低大小多少")

_VALUE_FIELDS = (
    "value",
    "current_value",
    "baseline_value",
    "absolute_change",
    "contribution",
    "contribution_rate",
    "change",
    "change_rate",
    "share",
    "numerator",
    "denominator",
    "zscore",
    "threshold",
    "rank",
)


@dataclass(frozen=True)
class _Row:
    member: str | None
    label: str | None
    values: Mapping[str, Decimal]
    period: str | None = None


@dataclass(frozen=True)
class _Dataset:
    tool: str
    metric: str | None
    label: str | None
    rows: tuple[_Row, ...]
    row_count: int
    displayed_count: int
    truncated: bool
    ascending: bool | None
    primary_field: str

    def values(self, field: str) -> list[tuple[_Row, Decimal]]:
        return [(row, row.values[field]) for row in self.rows if field in row.values]

    def member_names(self) -> list[str]:
        names: list[str] = []
        for row in self.rows:
            if row.member:
                names.append(row.member)
            if row.label and row.label != row.member:
                names.append(row.label)
        return names


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        try:
            candidate = Decimal(str(value))
        except InvalidOperation:
            return None
        return candidate if candidate.is_finite() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            candidate = Decimal(text)
        except InvalidOperation:
            return None
        return candidate if candidate.is_finite() else None
    return None


def _row_member(row: Mapping[str, Any]) -> str | None:
    dimensions = row.get("dimensions")
    if isinstance(dimensions, Mapping) and len(dimensions) == 1:
        return str(next(iter(dimensions.values())))
    if row.get("dimension_value") is not None:
        return str(row["dimension_value"])
    if row.get("dimension") is not None:
        return str(row["dimension"])
    return None


def _row_label(row: Mapping[str, Any]) -> str | None:
    labels = row.get("dimension_labels")
    if isinstance(labels, Mapping) and len(labels) == 1:
        return str(next(iter(labels.values())))
    if row.get("dimension_label") is not None:
        return str(row["dimension_label"])
    return None


def _row_period(row: Mapping[str, Any]) -> str | None:
    for key in ("period", "month", "date"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _dataset_from_message(message: Any) -> _Dataset | None:
    if not getattr(message, "trusted", False):
        return None
    view = getattr(message, "visible_data", None)
    if not isinstance(view, Mapping):
        return None
    raw_rows = view.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return None
    rows: list[_Row] = []
    for raw in raw_rows[:_MAX_MESSAGE_ROWS]:
        if not isinstance(raw, Mapping):
            continue
        values: dict[str, Decimal] = {}
        for field in _VALUE_FIELDS:
            number = _decimal(raw.get(field))
            if number is not None:
                values[field] = number
        rows.append(_Row(_row_member(raw), _row_label(raw), values, _row_period(raw)))
    if not rows:
        return None
    primary = next(
        (
            field
            for field in ("value", "current_value", "contribution", "absolute_change")
            if any(field in row.values for row in rows)
        ),
        "value",
    )
    arguments = getattr(message, "arguments", None) or {}
    ascending: bool | None = None
    if getattr(message, "tool", "") == "top_n":
        ascending = bool(arguments.get("ascending", False))
    return _Dataset(
        tool=str(getattr(message, "tool", "")),
        metric=view.get("metric"),
        label=view.get("label"),
        rows=tuple(rows),
        row_count=int(view.get("row_count") or len(rows)),
        displayed_count=int(view.get("displayed_count") or len(rows)),
        truncated=bool(view.get("truncated")),
        ascending=ascending,
        primary_field=primary,
    )


def trusted_datasets(messages: Iterable[Any]) -> list[_Dataset]:
    datasets = []
    for message in messages or ():
        dataset = _dataset_from_message(message)
        if dataset is not None:
            datasets.append(dataset)
    return datasets


def _section_numbers(value: Any) -> list[str]:
    """Numbers in a non-row visible section (totals, details)."""

    found: list[str] = []
    if isinstance(value, Mapping):
        for item in value.values():
            found.extend(_section_numbers(item))
        return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_section_numbers(item))
        return found
    if isinstance(value, Decimal):
        return [format(value, "f")]
    if isinstance(value, (int, float)):
        return [format(Decimal(str(value)), "f")]
    if isinstance(value, str):
        return [match.replace(",", "") for match in _NUMBER_RE.findall(value)]
    return found


def trusted_displayed_numbers(messages: Iterable[Any]) -> list[str]:
    """Numbers from rows and summary sections the model actually saw.

    ``totals``/``details`` are part of the trusted tool message, so a number
    the model quotes from ``totals.sum_contributions`` is traceable even though
    it is not a table row.
    """

    found: list[str] = []
    for message in messages or ():
        if not getattr(message, "trusted", False):
            continue
        view = getattr(message, "visible_data", None)
        if not isinstance(view, Mapping):
            continue
        for dataset in trusted_datasets([message]):
            for row in dataset.rows:
                for value in row.values.values():
                    text = format(value, "f")
                    if text not in found:
                        found.append(text)
        for section in ("totals", "details"):
            for text in _section_numbers(view.get(section)):
                if text not in found:
                    found.append(text)
    return found


def _mentions(text: str, dataset: _Dataset) -> list[tuple[int, int, str]]:
    """Return (start, end, member) for every named member in ``text``.

    The longest span wins when two names overlap, and a span contained in a
    longer one is dropped, so a label like "专业麦克风" cannot also match a
    shorter member inside it.
    """

    found: list[tuple[int, int, str]] = []
    for row in dataset.rows:
        member = row.member or row.label
        if not member:
            continue
        for name in {row.member, row.label}:
            if not name:
                continue
            spans: list[tuple[int, int]] = []
            if name.isascii():
                pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
                spans = [match.span() for match in pattern.finditer(text)]
            else:
                start = 0
                while True:
                    index = text.find(name, start)
                    if index < 0:
                        break
                    spans.append((index, index + len(name)))
                    start = index + len(name)
            found.extend((start, end, member) for start, end in spans)

    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    result: list[tuple[int, int, str]] = []
    for start, end, member in found:
        contained = any(start >= kept_start and end <= kept_end for kept_start, kept_end, _ in result)
        if contained:
            continue
        result.append((start, end, member))
    return result


def _ordered_members(hits: list[tuple[int, int, str]]) -> list[str]:
    members: list[str] = []
    for _, _, member in sorted(hits):
        if member not in members:
            members.append(member)
    return members


_RANK_CONNECTOR_RE = re.compile(r"^[\s、,，/]+$")
_PERIOD_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})(?!\d)")
_PERIOD_MONTH_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月")
_PER_MEMBER_CLAIM_RE = re.compile(
    r"各|每(?:个|一)?|均|都|分别|各自|自身|"
    r"其[^。！？\n；;，,]{0,12}(?:三个月|各月|月份|月内|期间|范围内|中)"
)


def _period_from_text(text: str) -> str | None:
    """A single month named by the claim, if the wording is unambiguous."""

    iso = {f"{int(year):04d}-{int(month):02d}" for year, month in _PERIOD_ISO_RE.findall(text) if 1 <= int(month) <= 12}
    if len(iso) == 1:
        return next(iter(iso))
    if iso:
        return None
    months = {int(month) for month in _PERIOD_MONTH_RE.findall(text) if 1 <= int(month) <= 12}
    return f"month:{next(iter(months))}" if len(months) == 1 else None


def _row_matches_period(row: _Row, period: str) -> bool:
    raw = row.period or ""
    iso = _PERIOD_ISO_RE.search(raw)
    if iso is None:
        return False
    row_period = f"{int(iso.group(1)):04d}-{int(iso.group(2)):02d}"
    if period.startswith("month:"):
        return int(iso.group(2)) == int(period.split(":", 1)[1])
    return row_period == period


def _is_per_member_claim(text: str, marker: int) -> bool:
    head = text[:marker]
    cut = max(head.rfind(char) for char in _CLAUSE_SEPARATORS)
    return bool(_PER_MEMBER_CLAIM_RE.search(head[cut + 1 :]))


def _extremum_value(values: list[Decimal], direction: str, magnitude: bool) -> Decimal | None:
    if not values:
        return None
    if magnitude:
        return (max if direction == "max" else min)(abs(value) for value in values)
    return (max if direction == "max" else min)(values)


def _ranked_enumeration(text: str, hits: list[tuple[int, int, str]]) -> bool:
    """True when members are joined only by list punctuation.

    ``最高的是 A（0.72）、B（0.71）、C（0.70）`` is a ranked top-N list: only
    the first entry claims the extremum. A tie claim joined by 和/与/及/and
    still applies to every named member and is checked in full.
    """

    if len(hits) < 2:
        return False
    for left, right in zip(hits, hits[1:]):
        between = _PARENTHETICAL_RE.sub("", text[left[1] : right[0]])
        between = re.sub(r"[\d.,%]+", " ", between).strip()
        if between and not _RANK_CONNECTOR_RE.fullmatch(between):
            return False
    return True


def _connected(text: str, hits: list[tuple[int, int, str]]) -> bool:
    for left, right in zip(hits, hits[1:]):
        between = text[left[1] : right[0]]
        between = _PARENTHETICAL_RE.sub("", between)
        between = re.sub(r"[\d.,%]+", " ", between).strip()
        if not between or _LIST_CONNECTOR_RE.fullmatch(between):
            continue
        words = re.findall(r"[A-Za-z']+", between)
        residue = re.sub(r"[A-Za-z'\s]+", "", between)
        if words and residue == "" and all(word.lower() in _ENGLISH_FILLER for word in words):
            continue
        return False
    return True


def _member_is_negated(text: str, hit: tuple[int, int, str]) -> bool:
    tail = text[hit[1] : hit[1] + 16]
    match = _CLAUSE_BREAK_RE.search(tail)
    clause = tail[: match.start()] if match else tail
    return any(neg in clause for neg in _NEGATION_SUFFIXES) and any(
        marker in clause for marker in _SUPERLATIVE_MAX + _SUPERLATIVE_MIN
    )


def _marker_end(text: str, marker: int) -> int:
    """End offset of the superlative marker that starts at ``marker``."""

    for candidate in _SUPERLATIVE_MAX + _SUPERLATIVE_MIN:
        if candidate.isascii():
            if text[marker : marker + len(candidate)].lower() == candidate.lower():
                return marker + len(candidate)
        elif text.startswith(candidate, marker):
            return marker + len(candidate)
    return marker


def _is_negated(text: str, marker: int) -> bool:
    window = text[max(0, marker - 20) : marker].rstrip()
    for prefix in _NEGATION_SUFFIXES:
        position = window.rfind(prefix)
        if position >= 0 and len(window) - (position + len(prefix)) <= 4:
            return True
    return False


def _is_top_n_phrase(text: str, marker: int) -> bool:
    """True for "最低的三名"/"the three lowest", which rank a set."""

    tail = text[_marker_end(text, marker) :]
    if _TOP_N_TAIL_RE.match(tail):
        return True
    return bool(_TOP_N_PREFIX_RE.search(text[:marker]))


def _claim_members(text: str, marker: int, dataset: _Dataset) -> list[str]:
    """All members a superlative in ``text`` at ``marker`` applies to.

    Both common Chinese/English shapes are covered:

    - ``P004 和 P006 最高`` / ``P004 and P006 have the highest ...``
    - ``最高的是 P004 和 P006`` / ``the highest is P004 and P006``

    A member listed after a contrast word (``次之``/``第二``/``second``) is not
    part of the claim, so "A 最高，B 次之" is not treated as claiming B.
    """

    # Parentheticals explain or qualify the claim, but they often name other
    # members ("无线键鼠套装（P002 之外最低的 P003）"). Treating those names as
    # claimants makes the subject ambiguous, so blank them while keeping
    # offsets stable.
    original = text
    text = _mask_parentheticals(text)
    head = text[:marker]
    cut = max(head.rfind(ch) for ch in _CLAUSE_SEPARATORS)
    subject = head[cut + 1 :]
    prefix_hits = _mentions(subject, dataset)
    prefix = _ordered_members(prefix_hits)
    if len(prefix) > 1 and not _connected(subject, prefix_hits):
        prefix = [prefix[-1]]

    tail = text[_marker_end(text, marker) :]
    body: str | None = None
    match = re.match(r"^\s*的?\s*(?:是|为|：|:)\s*", tail)
    if match:
        body = tail[match.end() :]
    else:
        # "EC 与最高的 NC 相差 ..." puts the superlative before the member as
        # an attributive ("the highest NC"); the prefix is the compared
        # subject, not a co-claimant.
        match = re.match(r"^\s*的\s*", tail)
        if match:
            body = tail[match.end() :]
        else:
            match = re.match(r"^\s*(?:is|are)\b\s*", tail, re.IGNORECASE)
            if match:
                body = tail[match.end() :]
    suffix: list[str] = []
    if body is not None:
        for term in _CONTRAST_TERMS:
            index = body.lower().find(term.lower()) if term.isascii() else body.find(term)
            if index >= 0:
                body = body[:index]
        suffix_hits = _mentions(body, dataset)
        suffix_hits = [hit for hit in suffix_hits if not _member_is_negated(body, hit)]
        suffix = _ordered_members(suffix_hits)
        # "最高的是 A（a），最低的是 B（b）" is two claims in one sentence: the
        # members after the comma are not part of the max claim. Only names
        # joined by a list connector stay together.
        if len(suffix) > 1 and not _connected(body, suffix_hits):
            suffix = [suffix[0]]
        elif len(suffix) > 1 and _ranked_enumeration(body, suffix_hits):
            suffix = suffix[:1]

    if suffix:
        claimed = suffix
    else:
        claimed = prefix
    if not claimed:
        nearest = _nearest_member(_mask_parentheticals(original), marker, dataset)
        if nearest is None:
            nearest = _nearest_member(original, marker, dataset)
        return [nearest] if nearest else []
    return claimed


def _nearest_member(text: str, marker: int, dataset: _Dataset) -> str | None:
    hits = _mentions(text, dataset)
    if not hits:
        return None
    # Chinese rank wording puts the member before the marker ("华南...第二").
    # Looking only at start distance can pick the member of the next clause,
    # so prefer the last member in the same clause as the marker.
    preceding = [
        item
        for item in hits
        if item[1] <= marker and marker - item[1] <= 20 and not _CLAUSE_BREAK_RE.search(text[item[1] : marker])
    ]
    if preceding:
        return max(preceding, key=lambda item: item[1])[2]
    index, end, member = min(hits, key=lambda item: (abs(item[0] - marker), -(item[1] - item[0])))
    return member


def _field_for(text: str, marker: int, dataset: _Dataset) -> str:
    """Pick the compared field from the hint nearest the superlative marker.

    Using proximity keeps a segment that mentions the numerator in one clause
    and the denominator in another from silently comparing the wrong column.
    """

    # Supporting detail in brackets ("（分子 86652 / 分母 120858）") does not
    # describe the claim's compared field.
    text = _mask_parentheticals(text)
    best: tuple[tuple[int, int], str] | None = None
    lower = text.lower()
    for hint, field in _FIELD_HINTS:
        if not any(field in row.values for row in dataset.rows):
            continue
        haystack = lower if hint.isascii() else text
        needle = hint.lower() if hint.isascii() else hint
        start = 0
        while True:
            index = haystack.find(needle, start)
            if index < 0:
                break
            start = index + len(needle)
            # "收入低于投入" compares against investment; it does not make
            # investment the field whose extremum is being claimed.
            if _FIELD_COMPARISON_PREFIX_RE.search(text[:index]):
                continue
            distance = abs(index - marker)
            key = (distance, -len(hint))
            if best is None or key < best[0]:
                best = (key, field)
    metric_distance: int | None = None
    for needle in (dataset.label, dataset.metric):
        if not needle:
            continue
        haystack = lower if needle.isascii() else text
        position = haystack.find(needle.lower() if needle.isascii() else needle)
        if position >= 0:
            distance = abs(position - marker)
            if metric_distance is None or distance < metric_distance:
                metric_distance = distance
    # A nearby metric name ("ROI 最低") scopes the comparison to the primary
    # value; generic column hints ("营销投入") are supporting detail and must
    # not override it just because they appear in the same paragraph.
    if metric_distance is not None and (best is None or metric_distance <= best[0][0]):
        return dataset.primary_field
    if best is not None:
        return best[1]
    if dataset.label and dataset.label in text:
        return dataset.primary_field
    if dataset.metric and dataset.metric in text:
        return dataset.primary_field
    if any((hint in lower if hint.isascii() else hint in text) for hint in _VALUE_HINTS):
        return dataset.primary_field
    return dataset.primary_field


def _numbers(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for raw in _NUMBER_RE.findall(text):
        number = _decimal(raw)
        if number is not None:
            values.append(number)
    return values


def _issue(
    kind: str,
    message: str,
    *,
    dataset: _Dataset,
    member: str | None = None,
    field: str | None = None,
    claimed: Decimal | None = None,
    actual: Decimal | None = None,
    actual_member: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "message": message,
        "tool": dataset.tool,
        "member": member,
        "field": field,
        "claimed": None if claimed is None else format(claimed, "f"),
        "actual": None if actual is None else format(actual, "f"),
        "actual_member": actual_member,
        "truncated": dataset.truncated,
    }


def _subset_scope_members(text: str, marker: int, dataset: _Dataset) -> set[str]:
    """Members a "其中 … 最小" claim is scoped to.

    ``SC、NC、WC 三个区域…其中 SC 数值最小`` compares SC only against the
    subset named before ``其中``; SC is not being claimed as the global minimum.
    """

    head = text[:marker]
    index = head.rfind("其中")
    if index < 0:
        return set()
    prefix = head[:index]
    starts = [prefix.rfind(token) for token in ("其余", "其他", "其它", "另外", "剩下", "余下")]
    latest = max(starts)
    if latest >= 0:
        token_len = next(
            len(token) for token in ("其余", "其他", "其它", "另外", "剩下", "余下") if prefix.rfind(token) == latest
        )
        prefix = prefix[latest + token_len :]
    members = set(_ordered_members(_mentions(prefix, dataset)))
    return members if len(members) >= 2 else set()


def _check_superlative(
    segment: str,
    dataset: _Dataset,
    *,
    direction: str,
    marker_index: int,
) -> list[dict[str, Any]]:
    if _is_negated(segment, marker_index):
        return []
    if _is_top_n_phrase(segment, marker_index):
        return []
    members = _claim_members(segment, marker_index, dataset)
    if not members:
        return []
    field = _field_for(segment, marker_index, dataset)
    values = dataset.values(field)
    if not values:
        return []
    scope = _subset_scope_members(segment, marker_index, dataset)
    if scope:
        scoped_values = [(row, value) for row, value in values if (row.member or row.label) in scope]
        if len(scoped_values) >= 2:
            values = scoped_values
    effective_direction = direction
    magnitude = field in {"contribution", "absolute_change", "change"} and any(
        term in segment for term in _NEGATIVE_EXTREMA
    )
    if (
        direction == "max"
        and field in {"contribution", "absolute_change", "change"}
        and any(term in segment for term in _NEGATIVE_EXTREMA)
        and not magnitude
    ):
        effective_direction = "min"
    if (
        direction == "max"
        and field in {"contribution", "absolute_change", "change"}
        and not magnitude
        and values
        and all(value <= 0 for _, value in values)
    ):
        # "其中华北贡献最大" over an all-negative contribution table means the
        # largest decline, not the largest signed number (the value closest to
        # zero). No separate word like "下降"/"拖累" is needed when every row
        # has the same negative direction.
        effective_direction = "min"
    if magnitude:
        # "降幅最小" describes the smallest drop by magnitude, not the most
        # negative signed contribution. Compare absolute values so a signed
        # contribution (-10385249) is not treated as larger than -12159846.
        extremum_key = (max if effective_direction == "max" else min)(abs(value) for _, value in values)
        winners = [row.member for row, value in values if abs(value) == extremum_key]
        winner_value = next((value for _, value in values if abs(value) == extremum_key), None)
    else:
        extremum_key = (max if effective_direction == "max" else min)(value for _, value in values)
        winners = [row.member for row, value in values if value == extremum_key]
        winner_value = next((value for _, value in values if value == extremum_key), None)
    extremum = winner_value
    actual_member = winners[0] if winners else None
    label = "最大" if effective_direction == "max" else "最小"
    scope = "已展示的" if dataset.truncated else ""

    issues: list[dict[str, Any]] = []
    if dataset.truncated and any(
        (marker in segment.lower() if marker.isascii() else marker in segment) for marker in _GLOBAL_MARKERS
    ):
        issues.append(
            _issue(
                "truncated_scope_overreach",
                (
                    f"结果被截断（已展示 {dataset.displayed_count}/{dataset.row_count} 行），"
                    f"{label}值只能按已展示行判断，不能推断全局"
                ),
                dataset=dataset,
                field=field,
            )
        )

    for member in members:
        member_rows = [row for row in dataset.rows if member in {row.member, row.label} and field in row.values]
        if not member_rows:
            continue
        member_scoped = False
        if len(member_rows) == 1:
            checks = [(member_rows[0].values[field], extremum, actual_member)]
        else:
            period = _period_from_text(segment)
            if period is None:
                # Several periods share this member and the sentence does not
                # name one, so no single row can stand for the claim.
                continue
            selected = [row for row in member_rows if _row_matches_period(row, period)]
            if not selected:
                continue
            if _is_per_member_claim(segment, marker_index):
                member_scoped = True
                reference = _extremum_value(
                    [row.values[field] for row in selected],
                    effective_direction,
                    magnitude,
                )
                checks = [(row.values[field], reference, member) for row in selected]
            else:
                checks = [(row.values[field], extremum, actual_member) for row in selected]
        for claimed, reference, reference_member in checks:
            if reference is None:
                continue
            equal = abs(claimed) == abs(reference) if magnitude else claimed == reference
            if equal:
                continue
            if member_scoped:
                message = (
                    f"{member} 的 {field} 为 {format(claimed, 'f')}，"
                    f"但其自身在所选期间的{label}值为 {format(reference, 'f')}"
                )
            elif len(members) > 1:
                message = (
                    f"{member} 被同时声称是{label}值，但其 {field} 为 "
                    f"{format(claimed, 'f')}；{scope} {len(values)} 行中{label}值是 "
                    f"{format(reference, 'f')}（{reference_member}）"
                )
            else:
                message = (
                    f"{member} 的 {field} 为 {format(claimed, 'f')}，"
                    f"但{scope} {len(values)} 行中{label}值是 "
                    f"{format(reference, 'f')}（{reference_member}）"
                )
            issues.append(
                _issue(
                    f"{effective_direction}_mismatch",
                    message,
                    dataset=dataset,
                    member=member,
                    field=field,
                    claimed=claimed,
                    actual=reference,
                    actual_member=reference_member,
                )
            )

    if (
        not issues
        and len(winners) > 1
        and any((marker in segment.lower() if marker.isascii() else marker in segment) for marker in _UNIQUE_MARKERS)
    ):
        issues.append(
            _issue(
                "unique_claim_violated",
                f"{members[0]} 被描述为唯一{label}值，但{len(winners)} 个成员同为 {format(extremum, 'f')}",
                dataset=dataset,
                member=members[0],
                field=field,
                claimed=extremum,
                actual=extremum,
                actual_member=winners[0] if winners else None,
            )
        )
    return issues


def _check_count(segment: str, dataset: _Dataset) -> list[dict[str, Any]]:
    match = _COUNT_RE.search(segment)
    if match is None:
        return []
    claimed = int(match.group(1))
    if claimed == dataset.row_count:
        return []
    return [
        _issue(
            "count_mismatch",
            f"结论称共 {claimed} 个成员，但工具返回 {dataset.row_count} 行",
            dataset=dataset,
            claimed=Decimal(claimed),
            actual=Decimal(dataset.row_count),
        )
    ]


def _check_rank(segment: str, dataset: _Dataset, match: re.Match[str] | None = None) -> list[dict[str, Any]]:
    if match is None:
        match = _RANK_RE.search(segment)
    if match is None:
        return []
    if _RANK_COMPARISON_PREFIX_RE.search(segment[: match.start()]) or (
        _RANK_COMPARISON_SUFFIX_RE.search(segment[match.end() :])
    ):
        # "EC 与第一名相差 0.007266" compares against rank 1; it does not
        # claim that EC is rank 1.
        return []
    if _RANK_RANGE_PREFIX_RE.search(segment[: match.start()]) or (_RANK_RANGE_SUFFIX_RE.match(segment[match.end() :])):
        # "第 2 至第 5 名" names a range, not a single rank.
        return []
    if _RANK_PAIR_COMPARISON_RE.match(segment[match.end() :]):
        # "第一名与第四名相差" compares two ranks.
        return []
    raw = match.group(1)
    rank = _CHINESE_RANK.get(raw) if raw in _CHINESE_RANK else int(raw)
    if rank is None:
        return []
    member = None
    following = [item for item in _mentions(segment, dataset) if item[0] >= match.end()]
    if following:
        nearest = min(following, key=lambda item: item[0])
        filler = segment[match.end() : nearest[0]]
        if filler.strip(" 的") == "":
            # "第三名企业路由器" names the ranked member right after the
            # ordinal. Prefer it over a member mentioned in the preceding
            # explanation ("会议摄像头 ... 第三名").
            member = nearest[2]
    if member is None and segment[match.end() : match.end() + 1] in _ORDINAL_ADJECTIVES:
        # "第二高的华南" names the member right after the ordinal; a comma
        # ("华南缺货率第二高，西部...") keeps the preceding member as subject.
        following = [item for item in _mentions(segment, dataset) if item[0] >= match.end() + 1]
        if following:
            nearest = min(following, key=lambda item: item[0])
            filler = segment[match.end() + 1 : nearest[0]]
            if filler.strip(" 的") == "":
                member = nearest[2]
    if member is None and _RANK_LIST_VERB_RE.search(segment[: match.start()]):
        # "SC 数值最小，按排名列第二" ranks the preceding subject; the next
        # clause may mention other members that are farther from the idiom.
        preceding = [item for item in _mentions(segment, dataset) if item[1] <= match.start()]
        if preceding:
            member = max(preceding, key=lambda item: item[1])[2]
    if member is None:
        member = _nearest_member(segment, match.start(), dataset)
    if member is None:
        return []
    row = next((item for item in dataset.rows if member in {item.member, item.label}), None)
    if row is None:
        return []
    if "rank" in row.values and int(row.values["rank"]) != rank:
        return [
            _issue(
                "rank_mismatch",
                f"{member} 被描述为第 {rank} 名，但工具排名为 {int(row.values['rank'])}",
                dataset=dataset,
                member=member,
                field="rank",
                claimed=Decimal(rank),
                actual=row.values["rank"],
            )
        ]
    return []


def _check_top_n_order(dataset: _Dataset) -> list[dict[str, Any]]:
    if dataset.tool != "top_n" or not dataset.rows:
        return []
    values = [row.values.get("value") for row in dataset.rows]
    if any(value is None for value in values):
        return []
    ascending = bool(dataset.ascending)
    ordered = list(values)
    expected = sorted(ordered, key=lambda value: value, reverse=not ascending)
    issues: list[dict[str, Any]] = []
    if ordered != expected:
        issues.append(
            _issue(
                "top_n_order_mismatch",
                "top_n 行未按请求的排序方向排列",
                dataset=dataset,
                field="value",
            )
        )
    ranks = [row.values.get("rank") for row in dataset.rows]
    if all(rank is not None for rank in ranks):
        expected_ranks = [Decimal(index) for index in range(1, len(ranks) + 1)]
        if [int(rank) for rank in ranks] != [int(value) for value in expected_ranks]:
            issues.append(
                _issue(
                    "top_n_rank_mismatch",
                    "top_n 排名不是从 1 开始的连续序列",
                    dataset=dataset,
                    field="rank",
                )
            )
    return issues


def _answer_member_order(answer: str, dataset: _Dataset) -> list[str]:
    found: list[tuple[int, str]] = []
    for row in dataset.rows:
        for name in (row.member, row.label):
            if not name:
                continue
            index = answer.find(name)
            if index >= 0:
                found.append((index, row.member or name))
    found.sort()
    order: list[str] = []
    for _, member in found:
        if member not in order:
            order.append(member)
    return order


def _superlative_claim_members(answer: str, dataset: _Dataset) -> set[str]:
    """Members named as an extremum, not as entries in a ranking list."""

    claimed: set[str] = set()
    masked = _mask_parentheticals(answer)
    for markers in (_SUPERLATIVE_MAX, _SUPERLATIVE_MIN):
        for marker in markers:
            start = 0
            while True:
                if marker.isascii():
                    match = re.search(
                        r"(?<![A-Za-z])" + re.escape(marker) + r"(?![A-Za-z])",
                        masked[start:],
                        re.IGNORECASE,
                    )
                    index = start + match.start() if match else -1
                    step = start + match.end() if match else len(masked)
                else:
                    index = masked.find(marker, start)
                    step = index + len(marker) if index >= 0 else len(masked)
                if index < 0:
                    break
                claimed.update(_claim_members(masked, index, dataset))
                start = max(step, start + 1)
    return claimed


def _check_answer_order(answer: str, dataset: _Dataset) -> list[dict[str, Any]]:
    if dataset.tool != "top_n" or len(dataset.rows) < 2:
        return []
    # Only an answer that talks about ranking makes member order a claim.
    # A list of values or of members in another clause ("完整名单为 …") is
    # not asserting the result's order.
    if not _RANKING_CUE_RE.search(answer):
        return []
    values = {row.member: row.values.get("value") for row in dataset.rows if row.member is not None}
    if any(value is None for value in values.values()):
        return []
    if len(set(values.values())) != len(values):
        return []
    dataset_order = [row.member for row in dataset.rows if row.member in values]
    # A multi-metric answer may put another metric's list first. Member order
    # only counts from the point where this dataset's metric is named.
    metric_positions = [
        answer.find(token) for token in (dataset.label, dataset.metric) if token and answer.find(token) >= 0
    ]
    if metric_positions:
        answer = answer[min(metric_positions) :]
    answer_order = [member for member in _answer_member_order(answer, dataset) if member in values]
    # A sentence may first name the extremum subject ("华东的缺货率最高：...")
    # and then enumerate the remaining entries in the result's order. The
    # subject is not an out-of-order entry.
    extremum_members = _superlative_claim_members(answer, dataset)
    if extremum_members:
        answer_order = [member for member in answer_order if member not in extremum_members]
    if len(answer_order) < 2:
        return []
    ranking = {member: index for index, member in enumerate(dataset_order)}
    for left, right in zip(answer_order, answer_order[1:]):
        if ranking[left] > ranking[right]:
            return [
                _issue(
                    "answer_order_mismatch",
                    f"结论中 {left} 排在 {right} 之前，与 top_n 的排序方向相反",
                    dataset=dataset,
                    member=left,
                    field="value",
                )
            ]
    return []


def _mask_parentheticals(text: str) -> str:
    """Blank parenthetical spans while keeping offsets stable.

    A comparison inside brackets ("P003 最高（比最低的 P009 高 9000）") is not a
    claim about the answer's main subject, so its superlative is not checked.
    """

    return _PARENTHETICAL_RE.sub(lambda match: " " * len(match.group(0)), text)


_TRUNCATED_ABSENCE_RE = re.compile(
    r"(?:其余|剩余|其他|其它|余下)\s*(?:的)?\s*(?:\d{1,4}\s*)?"
    r"(?:个|项|条|名|种|家)?[^。！？\n；;]*?"
    r"(?:均|都|全部|一律|没有|并无|无|不存在|未发现|未出现|不含|不包含)"
)
_ENGLISH_TRUNCATED_ABSENCE_RE = re.compile(
    r"\b(?:remaining|other|rest)\b[^.\n;]*\b(?:no|none|not|without)\b",
    re.IGNORECASE,
)


def _names_dataset(text: str, dataset: _Dataset) -> bool:
    return bool((dataset.label and dataset.label in text) or (dataset.metric and dataset.metric in text))


def _dataset_distance(text: str, dataset: _Dataset, marker: int) -> int:
    """Gap between the marker and the nearest occurrence of the dataset name.

    Measuring from the occurrence's edge (rather than its start) keeps
    "毛利率最低且缺货率最高" attached to the metric it qualifies: "毛利率"
    ends exactly at the marker while "缺货率" starts after it. Start-distance
    ties these two and lets a short label win by accident.
    """

    gaps: list[int] = []
    for token in (dataset.label, dataset.metric):
        if not token:
            continue
        start = 0
        while True:
            index = text.find(token, start)
            if index < 0:
                break
            end = index + len(token)
            gap = marker - end if index < marker else index - marker
            gaps.append(max(gap, 0))
            start = end
    return min(gaps) if gaps else 10**9


def _marker_dataset(
    text: str,
    marker: int,
    datasets: list[_Dataset],
    active: _Dataset | None,
) -> _Dataset | None:
    """Pick the dataset a claim at ``marker`` belongs to.

    A two-metric answer can contain "毛利率最低" and "缺货率最高" in one
    sentence. The nearest metric name decides which rows the claim is about;
    a preceding heading supplies context for bullets below it; and a unique
    member name is the last resort.
    """

    named = [dataset for dataset in datasets if _names_dataset(text, dataset)]
    if named:
        return min(
            named,
            key=lambda dataset: (
                _dataset_distance(text, dataset, marker),
                -(len(dataset.label or "") + len(dataset.metric or "")),
            ),
        )
    if active is not None:
        return active
    if len(datasets) == 1:
        return datasets[0]
    member_datasets = [dataset for dataset in datasets if _mentions(text, dataset)]
    if len(member_datasets) == 1:
        return member_datasets[0]
    return None


def _scoped_answer(answer: str, dataset: _Dataset, datasets: list[_Dataset]) -> str:
    """Limit answer-order checks to blocks that name the dataset's metric.

    Splitting on blank lines keeps a markdown table or an ordered list intact;
    splitting on every newline would drop the rows under a metric heading and
    leave only unrelated prose mentions to order.
    """

    blocks = _BLOCK_RE.split(answer)
    relevant = [block for block in blocks if _names_dataset(block, dataset)]
    if relevant:
        return "\n\n".join(relevant)
    return answer if len(datasets) == 1 else ""


def _truncated_absence_issues(answer: str, messages: list[Any]) -> list[dict[str, Any]]:
    """Flag absence claims about rows a truncated result never showed."""

    truncated: list[tuple[Any, int, int, Mapping[str, Any]]] = []
    for message in messages:
        view = getattr(message, "visible_data", None)
        if not isinstance(view, Mapping):
            continue
        row_count = int(getattr(message, "row_count", 0) or view.get("row_count") or 0)
        displayed = int(getattr(message, "displayed_count", 0) or view.get("displayed_count") or 0)
        is_truncated = bool(getattr(message, "truncated", False) or view.get("truncated"))
        if is_truncated and row_count > displayed:
            truncated.append((message, row_count, displayed, view))
    if not truncated or not answer:
        return []

    issues: list[dict[str, Any]] = []
    for segment in _SEGMENT_RE.split(answer):
        if not (_TRUNCATED_ABSENCE_RE.search(segment) or _ENGLISH_TRUNCATED_ABSENCE_RE.search(segment)):
            continue
        for message, row_count, displayed, view in truncated:
            dataset = _Dataset(
                tool=str(getattr(message, "tool", "")),
                metric=view.get("metric"),
                label=view.get("label"),
                rows=(),
                row_count=row_count,
                displayed_count=displayed,
                truncated=True,
                ascending=None,
                primary_field="value",
            )
            issues.append(
                _issue(
                    "truncated_absence_overreach",
                    (f"结果被截断（已展示 {displayed}/{row_count} 行），不能断言未展示行中不存在该结论"),
                    dataset=dataset,
                )
            )
            break
    return issues


def _unique_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for issue in issues:
        key = (
            issue["kind"],
            issue["tool"],
            issue["member"],
            issue["field"],
            issue["claimed"],
            issue["actual"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)
    return unique


def relation_issues(answer: str, messages: Iterable[Any]) -> list[dict[str, Any]]:
    """Return deterministic relational-claim mismatches for a final answer."""

    if not answer:
        return []
    materialized = list(messages or ())
    datasets = trusted_datasets(materialized)
    issues = _truncated_absence_issues(answer, materialized)
    if not datasets:
        return _unique_issues(issues)

    for dataset in datasets:
        issues.extend(_check_top_n_order(dataset))
        issues.extend(_check_answer_order(_scoped_answer(answer, dataset, datasets), dataset))

    active: _Dataset | None = None
    for segment in _SEGMENT_RE.split(answer):
        text = segment.strip()
        if not text:
            continue
        named = [dataset for dataset in datasets if _names_dataset(text, dataset)]
        if len(named) == 1:
            active = named[0]
        elif named:
            active = None
        for dataset in named:
            issues.extend(_check_count(text, dataset))
        if not named and active is not None:
            issues.extend(_check_count(text, active))

        for match in _RANK_RE.finditer(text):
            dataset = _marker_dataset(text, match.start(), datasets, active)
            if dataset is not None:
                issues.extend(_check_rank(text, dataset, match))

        masked = _mask_parentheticals(text)
        max_index = _first_marker_index(masked, _SUPERLATIVE_MAX)
        if max_index is not None:
            dataset = _marker_dataset(text, max_index, datasets, active)
            if dataset is not None:
                issues.extend(_check_superlative(text, dataset, direction="max", marker_index=max_index))
        min_index = _first_marker_index(masked, _SUPERLATIVE_MIN)
        if min_index is not None:
            dataset = _marker_dataset(text, min_index, datasets, active)
            if dataset is not None:
                issues.extend(_check_superlative(text, dataset, direction="min", marker_index=min_index))
    return _unique_issues(issues)


def _first_marker_index(text: str, markers: tuple[str, ...]) -> int | None:
    """Earliest marker position, with word boundaries for English markers."""

    best: int | None = None
    lower = text.lower()
    for marker in markers:
        if marker.isascii():
            match = re.search(
                r"(?<![A-Za-z])" + re.escape(marker) + r"(?![A-Za-z])",
                lower,
            )
            index = match.start() if match else -1
        else:
            index = text.find(marker)
        if index >= 0 and (best is None or index < best):
            best = index
    return best
