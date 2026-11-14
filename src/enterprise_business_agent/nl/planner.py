from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from ..analysis.contracts import DimensionFilter, TimeGrain
from ..analysis.time_ranges import quarter_start, shift_month
from ..config import Settings
from ..llm.client import LLMClient, LLMError, LLMInvalidResponse, LLMTimeout, LLMUnavailable
from ..llm.schemas import IntentPlan
from ..metric_resolver import DimensionRegistry, MetricResolver
from ..semantic import load_dimensions, load_metrics, normalize_name


PlanningProvider = Literal["rule", "llm", "none"]

_REGION_VALUES = {
    "华东": "EC",
    "华北": "NC",
    "华南": "SC",
    "西部": "WC",
}
_DEPARTMENT_VALUES = {
    "销售部": "SALES",
    "市场部": "MARKETING",
    "财务部": "FINANCE",
    "供应链部": "SUPPLY_CHAIN",
    "人力资源部": "HR",
}
_CHANNEL_VALUES = {
    "直营": "DIRECT",
    "电商": "ECOM",
    "经销商": "DIST",
    "零售": "RETAIL",
}
CORE_PRODUCTS = ["P001", "P002", "P003"]

_CN_NUMBERS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_QUARTER_PATTERNS = [
    re.compile(r"(?P<year>\d{4})\s*[-年]?\s*Q\s*(?P<quarter>[1-4])", re.IGNORECASE),
    re.compile(r"(?P<year>\d{4})\s*年\s*第?\s*(?P<quarter>[1-4])\s*季度"),
]
_MONTH_RANGE = re.compile(
    r"(?P<year>\d{4})\s*年\s*(?P<m1>\d{1,2})\s*月?\s*(?:到|至|~|-)\s*(?P<m2>\d{1,2})\s*月"
)
_MONTH = re.compile(r"(?P<year>\d{4})\s*(?:年|[-/])\s*(?P<month>\d{1,2})\s*月?")
_YEAR = re.compile(r"(?P<year>\d{4})\s*年(?:度)?")
_RECENT_MONTHS = re.compile(r"(?:最近|近)\s*(?P<count>[一二两三四五六七八九十\d]+)\s*个?月")
_PERCENT = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*%")
_TOP_N = re.compile(r"(?:top|前)\s*(?P<n>\d+|[一二两三四五六七八九十])", re.IGNORECASE)


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _quarter_end(period: date) -> date:
    start = quarter_start(period)
    return shift_month(start, 2)


def _month_range_from_quarter(period: date) -> tuple[date, date]:
    start = quarter_start(period)
    return start, _quarter_end(period)


def _parse_int(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if value.startswith("十") and len(value) == 2:
        return 10 + (_CN_NUMBERS.get(value[1]) or 0)
    if value.endswith("十") and len(value) == 2:
        return (_CN_NUMBERS.get(value[0]) or 0) * 10
    if value == "二十":
        return 20
    if len(value) == 1:
        return _CN_NUMBERS.get(value)
    return None


@dataclass
class PlanningResult:
    intent: IntentPlan | None
    provider: PlanningProvider
    model: str | None = None
    warnings: list[str] = field(default_factory=list)
    error: LLMError | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


class RuleBasedPlanner:
    """Deterministic Chinese question planner used offline and as LLM fallback."""

    def __init__(
        self,
        settings: Settings | None = None,
        resolver: MetricResolver | None = None,
        dimensions: DimensionRegistry | None = None,
    ):
        self.settings = settings or Settings()
        self.resolver = resolver or MetricResolver()
        self.dimensions = dimensions or DimensionRegistry()
        self._metric_aliases: list[tuple[int, str, str]] = []
        for metric in self.resolver.metrics:
            for alias in (metric.name, metric.label, *metric.synonyms):
                normalized = normalize_name(alias)
                if normalized:
                    self._metric_aliases.append((len(normalized), normalized, metric.name))
        self._metric_aliases.sort(key=lambda item: item[0], reverse=True)
        self._dimension_aliases: list[tuple[int, str, str]] = []
        for dimension in self.dimensions.dimensions:
            if dimension.kind != "categorical":
                continue
            for alias in (dimension.name, dimension.label, *dimension.synonyms):
                normalized = normalize_name(alias)
                if normalized:
                    self._dimension_aliases.append((len(normalized), normalized, dimension.name))
        self._dimension_aliases.sort(key=lambda item: item[0], reverse=True)

    def plan(
        self,
        question: str,
        *,
        as_of: date | None = None,
        request_start: date | None = None,
        request_end: date | None = None,
    ) -> IntentPlan | None:
        text = _nfkc(question)
        normalized = normalize_name(text)
        as_of = as_of or self.settings.data_end
        metric, metric_ambiguous, metric_candidates = self._detect_metric(normalized)
        operation = self._detect_operation(text, normalized)
        if operation == "investigation":
            return self._investigation_plan(text, as_of, request_start, request_end)

        if metric is None and not metric_ambiguous and not metric_candidates:
            return None

        start, end, period, year, quarter, grain = self._detect_time(text, as_of)
        detected_time = start is not None
        if request_start is not None:
            start = request_start
            period = request_start
        if request_end is not None:
            end = request_end
        if request_start is not None and request_end is None:
            end = request_start
        if start is not None and end is not None and start > end:
            start, end = end, start

        dimensions = self._detect_dimensions(normalized)
        filters = self._extract_filters(text)
        dimension = self._choose_analysis_dimension(metric, dimensions)
        n = self._detect_top_n(text)
        ascending = any(word in text for word in ("最低", "最差", "倒数", "bottom"))
        threshold = self._detect_threshold(text)
        kind = self._detect_comparison(text, quarter is not None or grain == TimeGrain.QUARTER)
        if (
            operation == "compare"
            and kind == "yoy"
            and detected_time
            and quarter is None
            and grain == TimeGrain.YEAR
        ):
            kind = "yoy_year"

        effective_end = min(as_of, self.settings.data_end)
        if start is None:
            start = self.settings.data_start
        if end is None:
            end = effective_end
        if operation == "contribution" and not detected_time:
            start = date(as_of.year, 1, 1)
            end = effective_end
        if operation == "compare":
            if kind in {"qoq", "yoy_quarter"}:
                if period is None:
                    period = quarter_start(as_of)
                    year = period.year
                    quarter = (period.month - 1) // 3 + 1
                else:
                    period = quarter_start(period)
                    year = period.year
                    quarter = (period.month - 1) // 3 + 1
            elif period is None:
                period = end if end is not None else as_of
                period = date(period.year, period.month, 1)

        baseline_start = None
        baseline_end = None
        if operation == "contribution" and start is not None and end is not None:
            baseline_start = date(start.year - 1, start.month, 1)
            baseline_end = date(end.year - 1, end.month, 1)

        return IntentPlan(
            operation=operation,
            metric=metric if not metric_ambiguous else self._matched_ambiguous_phrase(normalized),
            metric_candidates=metric_candidates,
            start=start,
            end=end,
            period=period,
            year=year,
            quarter=quarter,
            grain=grain,
            dimensions=[] if operation in {"trend", "anomaly"} else dimensions,
            filters=filters,
            comparison_kind=kind if operation == "compare" else None,
            dimension=dimension if operation in {"top_n", "contribution"} else None,
            n=n,
            ascending=ascending,
            threshold=threshold,
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            rationale="rule-based deterministic parse",
        )

    def _detect_metric(self, normalized: str) -> tuple[str | None, bool, list[str]]:
        if "库存风险" in normalized or "缺货风险" in normalized:
            return "stockout_rate", False, []
        matches = [
            (length, alias, metric)
            for length, alias, metric in self._metric_aliases
            if alias in normalized
        ]
        if not matches:
            return None, False, []
        longest = matches[0][0]
        candidates = [item for item in matches if item[0] == longest]
        metric_names = sorted({item[2] for item in candidates})
        if len(metric_names) == 1:
            return candidates[0][2], False, []
        return None, True, metric_names

    def _matched_ambiguous_phrase(self, normalized: str) -> str:
        # "利润" / "利润率" are the intended ambiguous phrases in the semantic layer.
        if "利润率" in normalized:
            return "利润率"
        if "利润" in normalized:
            return "利润"
        for _length, alias, _metric in self._metric_aliases:
            if alias in normalized:
                return alias
        return normalized

    def _detect_operation(self, text: str, normalized: str) -> str:
        if "是否" in text and ("缺货" in text or "库存" in text) and ("销售" in text or "下降" in text):
            return "investigation"
        if "贡献" in text:
            return "contribution"
        if "异常" in text:
            return "anomaly"
        if any(word in text.lower() for word in ("top", "排名", "最高", "最低", "最差", "倒数")):
            return "top_n"
        if re.search(r"前\s*(?:\d+|[一二两三四五六七八九十])", text):
            return "top_n"
        if "趋势" in text:
            return "trend"
        if any(word in text for word in ("环比", "同比", "变化", "对比")):
            return "compare"
        return "query"

    def _detect_time(self, text: str, as_of: date):
        text = re.sub(
            r"([一二三四])季度",
            lambda match: f"{_CN_NUMBERS[match.group(1)]}季度",
            text,
        )
        text = re.sub(
            r"(\d{4}\s*年\s*)([一二三四五六七八九十]{1,3})月",
            lambda match: f"{match.group(1)}{_parse_int(match.group(2))}月",
            text,
        )
        quarter_match = None
        for pattern in _QUARTER_PATTERNS:
            quarter_match = pattern.search(text)
            if quarter_match:
                break
        if quarter_match:
            year = int(quarter_match.group("year"))
            quarter = int(quarter_match.group("quarter"))
            start, end = _month_range_from_quarter(date(year, (quarter - 1) * 3 + 1, 1))
            return start, end, start, year, quarter, TimeGrain.QUARTER

        range_match = _MONTH_RANGE.search(text)
        if range_match:
            year = int(range_match.group("year"))
            m1 = int(range_match.group("m1"))
            m2 = int(range_match.group("m2"))
            start = date(year, m1, 1)
            end = date(year, m2, 1)
            return start, end, start, year, None, TimeGrain.MONTH

        if "本季度" in text or "当前季度" in text or "本季" in text:
            start, end = _month_range_from_quarter(as_of)
            q = (start.month - 1) // 3 + 1
            return start, end, start, start.year, q, TimeGrain.QUARTER
        if "上季度" in text:
            previous = shift_month(quarter_start(as_of), -3)
            start, end = _month_range_from_quarter(previous)
            q = (start.month - 1) // 3 + 1
            return start, end, start, start.year, q, TimeGrain.QUARTER
        if "本月" in text:
            start = date(as_of.year, as_of.month, 1)
            return start, start, start, as_of.year, None, TimeGrain.MONTH
        if "上月" in text:
            start = shift_month(as_of, -1)
            return start, start, start, start.year, None, TimeGrain.MONTH
        if "今年" in text:
            start = date(as_of.year, 1, 1)
            end = date(as_of.year, 12, 1)
            return start, end, start, as_of.year, None, TimeGrain.YEAR
        if "去年" in text:
            start = date(as_of.year - 1, 1, 1)
            end = date(as_of.year - 1, 12, 1)
            return start, end, start, as_of.year - 1, None, TimeGrain.YEAR

        recent = _RECENT_MONTHS.search(text)
        if recent:
            count = _parse_int(recent.group("count")) or 6
            start = shift_month(as_of, -(count - 1))
            return start, as_of, start, None, None, TimeGrain.MONTH

        month_match = _MONTH.search(text)
        if month_match:
            year = int(month_match.group("year"))
            month = int(month_match.group("month"))
            if 1 <= month <= 12:
                start = date(year, month, 1)
                return start, start, start, year, None, TimeGrain.MONTH

        year_match = _YEAR.search(text)
        if year_match:
            year = int(year_match.group("year"))
            grain = TimeGrain.YEAR
            if "按月" in text:
                grain = TimeGrain.MONTH
            elif "按季度" in text:
                grain = TimeGrain.QUARTER
            return date(year, 1, 1), date(year, 12, 1), date(year, 1, 1), year, None, grain

        grain = TimeGrain.MONTH
        if "按季度" in text or "季度趋势" in text:
            grain = TimeGrain.QUARTER
        elif "按年" in text or "年度趋势" in text:
            grain = TimeGrain.YEAR
        return None, None, None, None, None, grain

    def _detect_dimensions(self, normalized: str) -> list[str]:
        found: list[tuple[int, str]] = []
        for length, alias, name in self._dimension_aliases:
            if alias in normalized:
                found.append((length, name))
        return list(dict.fromkeys(name for _length, name in found))

    def _choose_analysis_dimension(self, metric: str | None, dimensions: list[str]) -> str | None:
        if dimensions:
            return dimensions[0]
        if metric in {"ROI", "marketing_spend", "campaign_revenue"}:
            return "campaign"
        if metric == "stockout_rate":
            return "product"
        if metric == "labor_cost":
            return "department"
        definition = self.resolver.get(metric or "")
        if definition is not None:
            available = [item for item in definition.dimensions if item != "period"]
            if len(available) == 1:
                return available[0]
        return None

    def _extract_filters(self, text: str) -> list[DimensionFilter]:
        filters: list[DimensionFilter] = []
        region_codes = [code for name, code in _REGION_VALUES.items() if name in text]
        if region_codes:
            filters.append(DimensionFilter(dimension="region", operator="eq", value=region_codes[0]))
        department_codes = [code for name, code in _DEPARTMENT_VALUES.items() if name in text]
        if department_codes:
            filters.append(DimensionFilter(dimension="department", operator="eq", value=department_codes[0]))
        channel_codes = [code for name, code in _CHANNEL_VALUES.items() if name in text]
        if channel_codes:
            filters.append(DimensionFilter(dimension="channel", operator="eq", value=channel_codes[0]))
        product_codes = sorted(set(re.findall(r"P\d{3}", text, re.IGNORECASE)))
        if "核心商品" in text:
            product_codes = sorted(set(product_codes) | set(CORE_PRODUCTS))
        if product_codes:
            filters.append(
                DimensionFilter(
                    dimension="product",
                    operator="eq" if len(product_codes) == 1 else "in",
                    value=product_codes[0] if len(product_codes) == 1 else product_codes,
                )
            )
        return filters

    def _detect_top_n(self, text: str) -> int | None:
        match = _TOP_N.search(text)
        if match:
            return _parse_int(match.group("n")) or 5
        if any(word in text.lower() for word in ("top", "排名", "最高", "最低", "最差", "倒数")):
            return 5
        return None

    def _detect_threshold(self, text: str) -> float:
        match = _PERCENT.search(text)
        if match:
            return max(float(match.group("value")) / 100, 0.000001)
        return 0.20

    def _detect_comparison(self, text: str, quarter_context: bool) -> str:
        if "同比" in text:
            return "yoy_quarter" if quarter_context else "yoy"
        if "环比" in text:
            return "qoq" if quarter_context else "mom"
        if quarter_context:
            return "qoq"
        return "mom"

    def _investigation_plan(
        self,
        text: str,
        as_of: date,
        request_start: date | None,
        request_end: date | None,
    ) -> IntentPlan:
        period = quarter_start(as_of)
        if request_start is not None:
            period = quarter_start(request_start)
        end = _quarter_end(period)
        if request_end is not None:
            end = request_end
        region = "EC"
        for name, code in _REGION_VALUES.items():
            if name in text:
                region = code
                break
        return IntentPlan(
            operation="investigation",
            metric="sales_amount",
            start=period,
            end=end,
            period=period,
            year=period.year,
            quarter=(period.month - 1) // 3 + 1,
            region=region,
            products=CORE_PRODUCTS,
            filters=[DimensionFilter(dimension="region", operator="eq", value=region)],
            rationale="rule-based east-region inventory investigation",
        )


class HybridPlanner:
    """Semantic-first planner: LLM when configured, deterministic parser as fallback."""

    def __init__(
        self,
        settings: Settings,
        llm_client: LLMClient | None = None,
        rule_planner: RuleBasedPlanner | None = None,
    ):
        self.settings = settings
        self.llm_client = llm_client
        self.rule_planner = rule_planner or RuleBasedPlanner(settings)
        self.resolver = self.rule_planner.resolver
        self.dimensions = self.rule_planner.dimensions

    def plan(
        self,
        question: str,
        *,
        as_of: date | None = None,
        request_start: date | None = None,
        request_end: date | None = None,
    ) -> PlanningResult:
        as_of = as_of or self.settings.data_end
        rule_intent = self.rule_planner.plan(
            question,
            as_of=as_of,
            request_start=request_start,
            request_end=request_end,
        )
        if self.llm_client is None:
            return PlanningResult(
                intent=rule_intent,
                provider="rule" if rule_intent is not None else "none",
                warnings=["llm_not_configured_using_rule_planner"],
            )

        try:
            result = self.llm_client.complete_json(
                system=self._system_prompt(),
                prompt=self._user_prompt(question, as_of),
            )
            data = result.data
            if isinstance(data.get("plan"), dict):
                data = data["plan"]
            intent = IntentPlan.model_validate(data)
            intent = self._fill_missing_defaults(intent, as_of, request_start, request_end)
            return PlanningResult(
                intent=intent,
                provider="llm",
                model=result.model,
                latency_ms=result.latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        except LLMTimeout as exc:
            return self._fallback(rule_intent, exc, "llm_timeout_using_rule_planner")
        except LLMUnavailable as exc:
            return self._fallback(rule_intent, exc, "llm_unavailable_using_rule_planner")
        except (LLMInvalidResponse, ValueError) as exc:
            return self._fallback(rule_intent, LLMInvalidResponse(str(exc)), "llm_invalid_using_rule_planner")
        except LLMError as exc:
            return self._fallback(rule_intent, exc, "llm_error_using_rule_planner")

    def _fallback(self, intent: IntentPlan | None, error: LLMError, warning: str) -> PlanningResult:
        return PlanningResult(
            intent=intent,
            provider="rule" if intent is not None else "none",
            warnings=[warning],
            error=error,
        )

    def _fill_missing_defaults(
        self,
        intent: IntentPlan,
        as_of: date,
        request_start: date | None,
        request_end: date | None,
    ) -> IntentPlan:
        if request_start is not None:
            intent.start = request_start
            if intent.operation == "compare" and intent.period is None:
                intent.period = request_start
        if request_end is not None:
            intent.end = request_end
        if intent.start is None:
            intent.start = self.settings.data_start
        if intent.end is None:
            intent.end = self.settings.data_end
        if intent.operation == "compare" and intent.comparison_kind is None:
            intent.comparison_kind = (
                "yoy_quarter" if intent.quarter is not None else "yoy"
            )
        if intent.operation == "compare" and intent.period is None:
            if intent.comparison_kind in {"qoq", "yoy_quarter"}:
                intent.period = quarter_start(as_of)
                intent.year = intent.period.year
                intent.quarter = (intent.period.month - 1) // 3 + 1
            else:
                intent.period = as_of
        return intent

    def _system_prompt(self) -> str:
        return (
            "You convert Chinese business questions into a strict JSON intent for a semantic layer. "
            "Return JSON only. Never write SQL. Never invent metrics or dimensions. "
            "Use only metric and dimension names from the supplied catalog. "
            "If a metric name is ambiguous, keep the phrase so the resolver can return candidates. "
            "Dates must be ISO YYYY-MM-DD or quarter forms such as 2025Q4."
        )

    def _user_prompt(self, question: str, as_of: date) -> str:
        metric_catalog = [
            {
                "name": metric.name,
                "label": metric.label,
                "synonyms": metric.synonyms,
                "dimensions": metric.dimensions,
            }
            for metric in self.resolver.metrics
        ]
        dimension_catalog = [
            {
                "name": dimension.name,
                "label": dimension.label,
                "synonyms": dimension.synonyms,
                "kind": dimension.kind,
            }
            for dimension in self.dimensions.dimensions
        ]
        schema = IntentPlan.model_json_schema()
        return (
            f"as_of: {as_of.isoformat()}\n"
            f"data_start: {self.settings.data_start.isoformat()}\n"
            f"data_end: {self.settings.data_end.isoformat()}\n"
            f"question: {question}\n"
            f"metric_catalog: {json.dumps(metric_catalog, ensure_ascii=False)}\n"
            f"dimension_catalog: {json.dumps(dimension_catalog, ensure_ascii=False)}\n"
            f"json_schema: {json.dumps(schema, ensure_ascii=False)}\n"
            "Return one JSON object matching IntentPlan. Do not include markdown fences."
        )
