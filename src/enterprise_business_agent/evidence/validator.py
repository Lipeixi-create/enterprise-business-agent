from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .contracts import (
    EvidenceRecord,
    Finding,
    NumberClassification,
    ReportDocument,
    ValidationIssue,
    ValidationResult,
)

_NUMBER = re.compile(r"(?<![0-9.\-])-?\d[\d,]*(?:\.\d+)?")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_PATH = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[(\d+)\])?")
_RATE_KEY = re.compile(r"(?:rate|ratio|margin|pct|percent|share)$", re.IGNORECASE)

# Date and period expressions are removed before scanning prose. Otherwise
# "2025-06" becomes the unrelated numbers 2025 and 06. The patterns cover the
# formats accepted by the API and the formats models commonly write in prose.
_DATE_PATTERNS = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?"),
    re.compile(r"\d{4}/\d{1,2}/\d{1,2}"),
    re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}"),
    re.compile(r"\d{4}-\d{1,2}(?!\d)"),
    re.compile(r"\d{4}/\d{1,2}(?!\d)"),
    re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"),
    re.compile(r"\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?"),
    re.compile(r"(?:19|20)\d{2}\s*年"),
    re.compile(r"(?:\d{4}\s*[-]?\s*)?Q[1-4]", re.IGNORECASE),
    re.compile(r"第[一二三四1-4]季度"),
)

_PARAMETER_WORDS = (
    "阈值",
    "门槛",
    "参数",
    "设定",
    "配置",
    "条件",
    "过滤",
    "筛选",
    "参数值",
    "大于",
    "小于",
    "超过",
    "不低于",
    "不高于",
    "至少",
    "最多",
    "top",
    "前",
    "第",
    "名",
    "n=",
    ">=",
    "<=",
)
_RATE_MARKERS = (
    "%",
    "率",
    "比例",
    "占比",
    "贡献度",
    "贡献率",
    "增幅",
    "降幅",
)

_DECREASE_WORDS = ("下降", "减少", "降低", "下滑", "萎缩", "恶化")
_INCREASE_WORDS = ("上升", "增长", "提高", "增加", "扩大")
_CAUSAL_WORDS = ("导致", "致使", "引起", "证明", "必然", "一定")
_HEDGES = ("可能", "待验证", "需验证", "需要验证", "不能", "仅为统计关联", "建议")


def _decimal(token: Any) -> Decimal | None:
    if token is None or isinstance(token, bool):
        return None
    try:
        return Decimal(str(token).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _mask_dates(text: str) -> str:
    if not text:
        return ""
    masked = text
    for pattern in _DATE_PATTERNS:
        masked = pattern.sub(lambda match: " " * len(match.group(0)), masked)
    return masked


def _date_tokens(value: Any) -> set[Decimal]:
    found: set[Decimal] = set()
    if not isinstance(value, str):
        return found
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(value):
            for token in _NUMBER.findall(match.group(0)):
                parsed = _decimal(token)
                if parsed is not None:
                    found.add(parsed)
    for year in _YEAR.findall(value):
        parsed = _decimal(year)
        if parsed is not None:
            found.add(parsed)
    return found


def _walk_numbers(
    value: Any,
    out: set[Decimal],
    dates: set[Decimal],
    rates: set[Decimal] | None = None,
    key: str | None = None,
) -> None:
    """Collect numeric values without treating date components as results."""

    is_rate = rates is not None and key is not None and bool(_RATE_KEY.search(key))
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float, Decimal)):
        parsed = _decimal(value)
        if parsed is not None:
            out.add(parsed)
            if is_rate:
                rates.add(parsed)
        return
    if isinstance(value, str):
        dates.update(_date_tokens(value))
        text = value.replace(",", "").strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
            parsed = _decimal(text)
            if parsed is not None:
                out.add(parsed)
                if is_rate:
                    rates.add(parsed)
            return
        for token in _NUMBER.findall(_mask_dates(value)):
            parsed = _decimal(token)
            if parsed is not None:
                out.add(parsed)
                if is_rate:
                    rates.add(parsed)
        return
    if isinstance(value, dict):
        for name, item in value.items():
            _walk_numbers(item, out, dates, rates, str(name))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _walk_numbers(item, out, dates, rates, key)


@dataclass
class _EvidenceIndex:
    business: set[Decimal] = field(default_factory=set)
    business_rates: set[Decimal] = field(default_factory=set)
    tool_parameters: set[Decimal] = field(default_factory=set)
    date_components: set[Decimal] = field(default_factory=set)


def _collect_allowed(evidences: Iterable[EvidenceRecord]) -> _EvidenceIndex:
    index = _EvidenceIndex()
    for evidence in evidences:
        # Business results come only from the tool's own result payload and
        # summary. Query parameters are intentionally kept separate.
        _walk_numbers(
            evidence.result,
            index.business,
            index.date_components,
            index.business_rates,
        )
        _walk_numbers(evidence.result_summary, index.business, index.date_components)
        _walk_numbers(
            evidence.query_parameters,
            index.tool_parameters,
            index.date_components,
        )
        for field_name, value in (
            ("time_range_start", evidence.time_range_start),
            ("time_range_end", evidence.time_range_end),
            ("baseline_period", evidence.baseline_period),
            ("baseline_time_range_start", evidence.baseline_time_range_start),
            ("baseline_time_range_end", evidence.baseline_time_range_end),
        ):
            if value:
                _walk_numbers(
                    value,
                    index.date_components,
                    index.date_components,
                    index.business_rates if "rate" in field_name else None,
                    field_name,
                )
        derived = _derived(evidence.result, "relative_change")
        if derived is not None:
            index.business.add(derived)
            index.business_rates.add(derived)
    return index


def _derived(payload: Any, name: str) -> Decimal | None:
    """Deterministic values computed from stored evidence fields."""

    if name != "relative_change" or not isinstance(payload, dict):
        return None
    change = _decimal(payload.get("total_change"))
    baseline = _decimal(payload.get("total_baseline"))
    if change is None or baseline in (None, 0):
        return None
    return change / baseline


def _extract_numbers(text: str) -> list[str]:
    return [token for token in _NUMBER.findall(text or "")]


def _parameter_context(before: str, after: str) -> bool:
    left = before.lower()[-10:]
    right = after.lower()[:10]
    return any(word in left or word in right for word in _PARAMETER_WORDS)


def _rate_claim(percent: bool, before: str, after: str) -> bool:
    if _parameter_context(before, after) and not any(word in before[-8:] + after[:8] for word in _RATE_MARKERS):
        return False
    return percent or any(word in before[-10:] + after[:10] for word in _RATE_MARKERS)


def _year_context(token: str, before: str, after: str) -> bool:
    """True only when a bare four-digit token is used as a date, not an amount."""

    if not re.fullmatch(r"(?:19|20)\d{2}", token):
        return False
    return bool(re.match(r"\s*(?:年|[-/.]\s*\d{1,2}\b)", after) or re.search(r"(?:年|[-/.]|第)\s*$", before))


def _classify_number(
    token: str,
    parsed: Decimal,
    *,
    percent: bool,
    before: str,
    after: str,
    index: _EvidenceIndex,
) -> tuple[str, bool, str | None, str | None]:
    """Return (category, allowed, issue_code, message)."""

    ratio = parsed / Decimal(100) if percent else None
    if percent:
        if ratio in index.business_rates or -ratio in index.business_rates:
            return "business_result", True, None, None
    elif parsed in index.business:
        return "business_result", True, None, None
    if parsed in index.date_components and _year_context(token, before, after):
        return "date_component", True, None, None

    in_parameters = ratio in index.tool_parameters if percent else parsed in index.tool_parameters
    if in_parameters:
        if _parameter_context(before, after) and not _rate_claim(percent, before, after):
            return "tool_parameter", True, None, None
        return (
            "tool_parameter",
            False,
            "tool_parameter_as_result",
            "只出现在工具参数中，不能作为没有证据的业务结果",
        )
    return (
        "unsupported_number",
        False,
        "unsourced_number",
        "无法在任何证据中找到",
    )


def _resolve(payload: Any, path: str) -> Any:
    if path.startswith("@derived."):
        return _derived(payload, path.split(".", 1)[1])
    current = payload
    position = 0
    for match in _PATH.finditer(path):
        if match.start() != position and path[position : match.start()] not in {".", ""}:
            return None
        name, index = match.group(1), match.group(2)
        if not isinstance(current, dict) or name not in current:
            return None
        current = current[name]
        if index is not None:
            if not isinstance(current, list):
                return None
            position_index = int(index)
            if position_index >= len(current):
                return None
            current = current[position_index]
        position = match.end()
        if position < len(path) and path[position] == ".":
            position += 1
    if position != len(path):
        return None
    return current


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class NumericValidator:
    """Check a structured report against the evidence ledger.

    The validation is structural where possible: report numbers are value
    references resolved against stored tool output. Free text is additionally
    scanned for numbers that do not appear anywhere in the evidence, which is
    how model-written prose is prevented from inventing figures.
    """

    def validate(
        self,
        report: ReportDocument,
        evidences: list[EvidenceRecord],
        findings: list[Finding],
    ) -> ValidationResult:
        issues: list[ValidationIssue] = []
        checks: list[str] = []
        by_id = {item.evidence_id: item for item in evidences}
        findings_by_id = {item.finding_id: item for item in findings}
        index = _collect_allowed(evidences)
        unverified: list[str] = []
        numbers: list[NumberClassification] = []

        checks.append("evidence_reference_exists")
        referenced_ids = set()
        for conclusion in self._all_conclusions(report):
            referenced_ids.update(conclusion.evidence_ids)
            referenced_ids.update(ref.evidence_id for ref in conclusion.value_refs)
        for ref in report.evidence_references:
            referenced_ids.add(ref.evidence_id)
        for item in report.key_metrics:
            referenced_ids.add(item.value.evidence_id)
            if item.change:
                referenced_ids.add(item.change.evidence_id)
        for section in report.domain_analysis:
            referenced_ids.update(section.evidence_ids)
        for recommendation in report.recommendations:
            referenced_ids.update(recommendation.evidence_ids)
        for evidence_id in sorted(referenced_ids):
            if evidence_id not in by_id:
                issues.append(
                    ValidationIssue(
                        code="unknown_evidence",
                        message=f"报告引用了当前 run 中不存在的 evidence_id {evidence_id!r}",
                        evidence_id=evidence_id,
                    )
                )

        checks.append("evidence_belongs_to_report_run")
        for evidence in evidences:
            if evidence.run_id != report.report_info.run_id:
                issues.append(
                    ValidationIssue(
                        code="cross_run_evidence",
                        message=(
                            f"证据 {evidence.evidence_id!r} 属于 run "
                            f"{evidence.run_id!r}，但报告属于 run "
                            f"{report.report_info.run_id!r}"
                        ),
                        evidence_id=evidence.evidence_id,
                    )
                )

        checks.append("finding_reference_exists")
        finding_ids = set()
        for conclusion in self._all_conclusions(report):
            finding_ids.update(conclusion.finding_ids)
        for section in report.domain_analysis:
            finding_ids.update(section.finding_ids)
        for recommendation in report.recommendations:
            finding_ids.update(recommendation.finding_ids)
        for finding_id in sorted(finding_ids):
            if finding_id not in findings_by_id:
                issues.append(
                    ValidationIssue(
                        code="unknown_finding",
                        message=f"报告引用了当前 run 中不存在的 finding_id {finding_id!r}",
                    )
                )

        checks.append("findings_belong_to_report_run")
        for finding in findings:
            if finding.run_id != report.report_info.run_id:
                issues.append(
                    ValidationIssue(
                        code="cross_run_finding",
                        message=(
                            f"Finding {finding.finding_id!r} 属于 run "
                            f"{finding.run_id!r}，但报告属于 run "
                            f"{report.report_info.run_id!r}"
                        ),
                    )
                )
            unknown = sorted(set(finding.evidence_ids) - set(by_id))
            if unknown:
                issues.append(
                    ValidationIssue(
                        code="finding_unknown_evidence",
                        message=(f"Finding {finding.finding_id!r} 引用了当前 run 不存在的 evidence_id：{unknown}"),
                    )
                )

        checks.append("evidence_tool_call_ids_are_unique")
        seen_calls: dict[str, str] = {}
        for evidence in evidences:
            previous = seen_calls.get(evidence.tool_call_id)
            if previous is not None and previous != evidence.evidence_id:
                issues.append(
                    ValidationIssue(
                        code="duplicate_tool_call_id",
                        message=(
                            f"证据 {evidence.evidence_id!r} 与 {previous!r} "
                            f"使用了相同的 tool_call_id {evidence.tool_call_id!r}"
                        ),
                        evidence_id=evidence.evidence_id,
                    )
                )
            else:
                seen_calls[evidence.tool_call_id] = evidence.evidence_id

        checks.append("value_reference_matches_evidence")
        for location, ref in self._iter_value_refs(report):
            evidence = by_id.get(ref.evidence_id)
            if evidence is None:
                continue
            resolved = _resolve(evidence.result, ref.field)
            if resolved is None:
                issues.append(
                    ValidationIssue(
                        code="unresolved_value",
                        message=f"{ref.field!r} 在证据 {ref.evidence_id} 中不存在",
                        location=location,
                        evidence_id=ref.evidence_id,
                    )
                )
                continue
            expected, actual = _decimal(ref.value), _decimal(resolved)
            if expected is None or actual is None or expected != actual:
                issues.append(
                    ValidationIssue(
                        code="value_mismatch",
                        message=(
                            f"{location} 的值 {ref.value!r} 与证据 {ref.evidence_id} "
                            f"的 {ref.field!r}={resolved!r} 不一致"
                        ),
                        location=location,
                        evidence_id=ref.evidence_id,
                    )
                )
            if ref.unit and evidence.unit and ref.unit != evidence.unit:
                if not (ref.unit == "ratio" and _is_rate_field(ref.field)):
                    issues.append(
                        ValidationIssue(
                            code="unit_mismatch",
                            message=f"{location} 单位 {ref.unit!r} 与证据单位 {evidence.unit!r} 不一致",
                            location=location,
                            evidence_id=ref.evidence_id,
                        )
                    )

        checks.append("metric_card_period_within_evidence")
        for card in report.key_metrics:
            evidence = by_id.get(card.value.evidence_id)
            if evidence is None:
                continue
            if card.metric != (evidence.metric_code or card.metric):
                issues.append(
                    ValidationIssue(
                        code="metric_mismatch",
                        message=f"指标卡 {card.metric!r} 与证据 {card.value.evidence_id} 的指标 {evidence.metric_code!r} 不一致",
                        location=f"key_metrics[{card.metric}]",
                        evidence_id=card.value.evidence_id,
                    )
                )
            if evidence.time_range_start and evidence.time_range_end:
                period = _month(card.period)
                start = _month(evidence.time_range_start)
                end = _month(evidence.time_range_end)
                if period and start and end and not (start <= period <= end):
                    issues.append(
                        ValidationIssue(
                            code="period_mismatch",
                            message=f"指标卡期间 {card.period!r} 不在证据时间范围 {evidence.time_range_start}..{evidence.time_range_end} 内",
                            location=f"key_metrics[{card.metric}]",
                            evidence_id=card.value.evidence_id,
                        )
                    )

        checks.append("scope_filters_match_evidence")
        observed_filters = {_canonical(item) for evidence in evidences for item in evidence.filters}
        declared_filters = {_canonical(item) for item in report.data_scope.filters}
        if declared_filters != observed_filters:
            missing = sorted(observed_filters - declared_filters)
            extra = sorted(declared_filters - observed_filters)
            issues.append(
                ValidationIssue(
                    code="scope_filter_mismatch",
                    message=f"报告筛选条件与证据不一致：缺少 {missing}，多出 {extra}",
                    location="data_scope.filters",
                )
            )

        checks.append("scope_time_range_matches_evidence")
        observed_ranges = {
            (evidence.time_range_start, evidence.time_range_end)
            for evidence in evidences
            if evidence.time_range_start and evidence.time_range_end
        }
        declared_ranges = {(item.get("start"), item.get("end")) for item in report.data_scope.time_ranges}
        if declared_ranges != observed_ranges:
            issues.append(
                ValidationIssue(
                    code="scope_time_range_mismatch",
                    message="报告时间范围与证据时间范围不一致",
                    location="data_scope.time_ranges",
                )
            )

        checks.append("no_unsourced_numbers")
        for location, text in self._iter_text(report):
            for token in sorted(_date_tokens(text), key=lambda item: str(item)):
                numbers.append(
                    NumberClassification(
                        token=str(token),
                        category="date_component",
                        allowed=True,
                        location=location,
                    )
                )
            masked = _mask_dates(text)
            for match in _NUMBER.finditer(masked):
                token = match.group(0)
                parsed = _decimal(token)
                if parsed is None:
                    continue
                before = text[max(0, match.start() - 24) : match.start()]
                after = text[match.end() : match.end() + 24]
                percent = after.lstrip().startswith("%")
                category, allowed, code, reason = _classify_number(
                    token,
                    parsed,
                    percent=percent,
                    before=before,
                    after=after,
                    index=index,
                )
                numbers.append(
                    NumberClassification(
                        token=token,
                        category=category,
                        allowed=allowed,
                        location=location,
                    )
                )
                if allowed:
                    continue
                unverified.append(token)
                issues.append(
                    ValidationIssue(
                        code=code or "unsourced_number",
                        message=f"{location} 中的数字 {token!r} {reason}",
                        location=location,
                        category=category,
                    )
                )

        checks.append("hypothesis_not_asserted_as_fact")
        for index, conclusion in enumerate(report.conclusions):
            for finding_id in conclusion.finding_ids:
                finding = findings_by_id.get(finding_id)
                if finding is None:
                    continue
                if (
                    finding.finding_type in {"hypothesis", "correlation", "recommendation"}
                    and conclusion.certainty == "fact"
                ):
                    issues.append(
                        ValidationIssue(
                            code="hypothesis_as_fact",
                            message=f"发现 {finding_id} 是 {finding.finding_type}，不能作为确定事实写入结论",
                            location=f"conclusions[{index}]",
                        )
                    )

        checks.append("no_causal_overreach")
        for location, text in self._iter_text(report):
            if any(word in text for word in _CAUSAL_WORDS) and not any(hedge in text for hedge in _HEDGES):
                issues.append(
                    ValidationIssue(
                        code="causal_overreach",
                        message=f"{location} 使用了因果断言措辞，但关联类证据不足以支持因果结论",
                        location=location,
                    )
                )

        checks.append("no_direction_contradiction")
        for location, conclusion in self._iter_conclusions_with_refs(report):
            direction = _direction_of(conclusion.statement)
            if direction == 0:
                continue
            for ref in conclusion.value_refs:
                if "change" not in ref.field:
                    continue
                evidence = by_id.get(ref.evidence_id)
                if evidence is None:
                    continue
                resolved = _decimal(_resolve(evidence.result, ref.field))
                if resolved is None or resolved == 0:
                    continue
                if (resolved > 0) != (direction > 0):
                    issues.append(
                        ValidationIssue(
                            code="direction_contradiction",
                            message=(f"{location} 表述方向与证据 {ref.evidence_id} 的 {ref.field}={resolved} 相反"),
                            location=location,
                            evidence_id=ref.evidence_id,
                        )
                    )

        return ValidationResult(
            passed=not issues,
            checks=checks,
            issues=issues,
            unverified_numbers=sorted(set(unverified)),
            numbers=numbers,
        )

    # -- iteration helpers ---------------------------------------------
    @staticmethod
    def _all_conclusions(report: ReportDocument):
        yield from report.conclusions
        yield from report.anomalies_and_contributions
        yield from report.cross_domain

    def _iter_value_refs(self, report: ReportDocument):
        for index, conclusion in enumerate(report.conclusions):
            for ref in conclusion.value_refs:
                yield f"conclusions[{index}]", ref
        for index, card in enumerate(report.key_metrics):
            yield f"key_metrics[{index}].value", card.value
            if card.change:
                yield f"key_metrics[{index}].change", card.change

    def _iter_text(self, report: ReportDocument):
        # ``objective`` echoes the user's question and is not a data claim.
        yield "executive_summary", report.executive_summary
        for index, conclusion in enumerate(self._all_conclusions(report)):
            yield f"conclusion[{index}]", conclusion.statement
        for index, section in enumerate(report.domain_analysis):
            yield f"domain_analysis[{index}]", section.narrative
        for index, item in enumerate(report.risks_and_uncertainty):
            yield f"risks_and_uncertainty[{index}]", item
        for index, item in enumerate(report.recommendations):
            yield f"recommendations[{index}].action", item.action
            yield f"recommendations[{index}].rationale", item.rationale
            yield f"recommendations[{index}].expected_direction", item.expected_direction
            yield f"recommendations[{index}].risk", item.risk
            yield f"recommendations[{index}].validation_metric", item.validation_metric
        for index, item in enumerate(report.next_validation):
            yield f"next_validation[{index}]", item

    def _iter_conclusions_with_refs(self, report: ReportDocument):
        for index, conclusion in enumerate(self._all_conclusions(report)):
            yield f"conclusion[{index}]", conclusion


def _direction_of(text: str) -> int:
    decrease = any(word in text for word in _DECREASE_WORDS)
    increase = any(word in text for word in _INCREASE_WORDS)
    if decrease and not increase:
        return -1
    if increase and not decrease:
        return 1
    return 0


def _is_rate_field(field: str) -> bool:
    """Rates are dimensionless even when their metric carries a currency unit."""

    name = field.rsplit(".", 1)[-1]
    return name.endswith("_rate") or name in {"rate", "relative_change"}


def _month(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)
    if len(text) >= 7 and text[4] == "-":
        return text[:7]
    return None
