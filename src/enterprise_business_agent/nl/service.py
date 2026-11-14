from __future__ import annotations

import hashlib
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from ..analysis.contracts import (
    AnalysisStatus,
    AnomalyRequest,
    ContributionRequest,
    DimensionFilter,
    MetricQueryRequest,
    PeriodComparisonRequest,
    TopNRequest,
    TrendRequest,
)
from ..analysis.engine import (
    analyze_contribution,
    analyze_trend,
    compare_periods,
    detect_anomalies,
    get_top_n,
    query_metric,
)
from ..config import Settings
from ..llm.client import LLMClient, LLMError, LLMTimeout, LLMUnavailable
from ..metric_resolver import DimensionRegistry, MetricResolver, ResolutionCandidate
from ..security import (
    AuthorizationError,
    AuthorizationPolicy,
    Principal,
    QueryErrorCode,
    SafeQueryExecutor,
    SecureQueryService,
    SQLGuard,
)
from .contracts import (
    ErrorInfo,
    InvestigationResponse,
    NaturalLanguageRequest,
    NaturalLanguageResponse,
    Observation,
    ResultType,
    SQLQueryResult,
)
from .planner import CORE_PRODUCTS, HybridPlanner, PlanningResult
from .sql_fallback import SQLFallbackRunner


def _status_for_planning_error(error: LLMError | None) -> AnalysisStatus:
    if isinstance(error, LLMTimeout):
        return AnalysisStatus.LLM_TIMEOUT
    if isinstance(error, LLMUnavailable):
        return AnalysisStatus.LLM_UNAVAILABLE
    return AnalysisStatus.UNSUPPORTED


def _error_info(error: LLMError | None) -> ErrorInfo | None:
    if error is None:
        return None
    return ErrorInfo(code=error.code, message=str(error), stage="planning")


_OUT_OF_SCOPE_MARKERS = ("预测", "预估", "预判", "未来", "下个月", "明年", "假设", "模拟")


def _format_number(value: Decimal | None, unit: str = "") -> str:
    if value is None:
        return "无可用值"
    return f"{value:,.2f}{unit}"


def _format_rate(value: Decimal | None) -> str:
    if value is None:
        return "无法计算变化率"
    return f"{value * 100:.2f}%"


class NaturalLanguageAnalyzer:
    """Semantic-first NL pipeline.

    Order: intent -> metric resolution -> Phase 2 request -> analysis engine.
    Candidate SQL is an explicit opt-in fallback and never replaces the
    semantic path when a metric can be resolved.
    """

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        llm_client: LLMClient | None = None,
        planner: HybridPlanner | None = None,
        query_service: SecureQueryService | None = None,
        principal: Principal | None = None,
        authorization: AuthorizationPolicy | None = None,
    ):
        self.session = session
        self.settings = settings
        self.llm_client = llm_client
        self.planner = planner or HybridPlanner(settings, llm_client)
        self.resolver: MetricResolver = self.planner.resolver
        self.dimensions: DimensionRegistry = self.planner.dimensions
        self.principal = principal
        self.authorization = authorization
        self.query_service = query_service
        if self.query_service is None:
            engine = session.get_bind()
            engine = getattr(engine, "engine", engine)
            executor = SafeQueryExecutor(
                engine,
                max_rows=settings.query_max_rows,
                timeout_ms=settings.query_timeout_ms,
            )
            self.query_service = SecureQueryService(
                SQLGuard(
                    max_rows=settings.query_max_rows,
                    max_offset=settings.sql_max_offset,
                ),
                executor,
            )
        self.sql_fallback = SQLFallbackRunner(
            settings,
            llm_client,
            self.query_service,
            self.query_service.executor.dialect,
        )

    def analyze(
        self,
        request: NaturalLanguageRequest,
        *,
        request_id: str | None = None,
    ) -> NaturalLanguageResponse:
        request_id = request_id or str(uuid.uuid4())
        if any(marker in request.question for marker in _OUT_OF_SCOPE_MARKERS):
            return NaturalLanguageResponse(
                request_id=request_id,
                question=request.question,
                status=AnalysisStatus.UNSUPPORTED,
                planner="none",
                explanation="本服务只分析数据库中的历史事实，不支持预测、假设或模拟类问题。",
                error=ErrorInfo(
                    code="unsupported_question",
                    message="forecasting and simulation questions are outside the supported scope",
                    stage="planning",
                ),
            )
        planning = self.planner.plan(
            request.question,
            as_of=request.as_of or self.settings.data_end,
            request_start=request.start,
            request_end=request.end,
        )
        intent = planning.intent
        base = NaturalLanguageResponse(
            request_id=request_id,
            question=request.question,
            status=AnalysisStatus.UNSUPPORTED,
            planner=planning.provider,
            planner_model=planning.model,
            intent=intent,
            warnings=list(planning.warnings),
            llm_latency_ms=planning.latency_ms,
            llm_tokens={
                key: value
                for key, value in {
                    "input": planning.input_tokens,
                    "output": planning.output_tokens,
                }.items()
                if value is not None
            },
        )

        if intent is None:
            error = _error_info(planning.error)
            if request.allow_sql_fallback:
                return self._run_sql_fallback(request, base, request_id, previous_error=error.message if error else None)
            base.status = _status_for_planning_error(planning.error)
            base.error = error or ErrorInfo(
                code="unsupported_question",
                message="semantic layer could not resolve the question to a supported metric and operation",
                stage="planning",
            )
            return base

        if len(intent.metric_candidates) > 1:
            base.status = AnalysisStatus.AMBIGUOUS
            base.candidates = self._candidate_models(intent.metric_candidates)
            base.error = ErrorInfo(
                code="ambiguous_metric",
                message="the question names more than one metric; choose one metric or split the question",
                stage="resolution",
                detail={"candidates": intent.metric_candidates},
            )
            return base

        metric_name = intent.metric
        if metric_name is None and len(intent.metric_candidates) == 1:
            metric_name = intent.metric_candidates[0]
        resolution = self.resolver.resolve(metric_name or "")
        if resolution.status == "ambiguous":
            base.status = AnalysisStatus.AMBIGUOUS
            base.metric = metric_name
            base.candidates = resolution.candidates
            base.error = ErrorInfo(
                code="ambiguous_metric",
                message=resolution.message or "metric is ambiguous",
                stage="resolution",
                detail={"candidates": [item.name for item in resolution.candidates]},
            )
            return base
        if resolution.status != "resolved" or resolution.metric is None:
            base.status = AnalysisStatus.UNSUPPORTED
            base.metric = metric_name
            base.error = ErrorInfo(
                code="unknown_metric",
                message=resolution.message or "metric is not defined in the semantic layer",
                stage="resolution",
            )
            if request.allow_sql_fallback:
                return self._run_sql_fallback(
                    request,
                    base,
                    request_id,
                    previous_error=base.error.message if base.error else None,
                )
            return base

        metric = resolution.metric
        base.metric = metric.name
        base.metric_label = metric.label

        if self.authorization is not None and self.principal is not None:
            try:
                self.authorization.require_request(
                    self.principal,
                    metric=metric.name,
                    dimensions=intent.dimensions,
                    filters=intent.filters,
                    metric_domain=getattr(metric, "domain", None),
                    metric_source=getattr(metric, "source_table", None),
                )
            except AuthorizationError as exc:
                base.status = AnalysisStatus.FORBIDDEN
                base.error = ErrorInfo(
                    code="forbidden",
                    message=str(exc),
                    stage="authorization",
                    detail=exc.detail,
                )
                return base

        try:
            result, result_type = self._execute_intent(intent, metric, question=request.question)
        except (ValueError, TypeError) as exc:
            base.status = AnalysisStatus.INVALID_REQUEST
            base.error = ErrorInfo(code="invalid_request", message=str(exc), stage="request_build")
            return base

        base.result = result
        base.result_type = result_type
        base.status = result.status
        base.explanation = self._explain(result_type, result)
        if result_type == ResultType.INVESTIGATION:
            base.data_sources = ["sales_fact", "inventory_snapshot"]
        else:
            base.data_sources = self._data_sources(result, metric)
        if result.status != AnalysisStatus.OK:
            message = getattr(result, "reason", None)
            if message is None and result_type == ResultType.INVESTIGATION:
                message = "investigation observations are incomplete"
            base.error = ErrorInfo(
                code=result.status.value,
                message=message or "analysis did not produce a complete result",
                stage="analysis",
            )
            if result.status in {AnalysisStatus.UNSUPPORTED, AnalysisStatus.INVALID_REQUEST} and request.allow_sql_fallback:
                return self._run_sql_fallback(
                    request,
                    base,
                    request_id,
                    previous_error=base.error.message if base.error else None,
                )
        return base

    def _run_sql_fallback(
        self,
        request: NaturalLanguageRequest,
        base: NaturalLanguageResponse,
        request_id: str,
        *,
        previous_error: str | None = None,
    ) -> NaturalLanguageResponse:
        if (
            self.authorization is not None
            and self.principal is not None
            and not self.authorization.allows_sql_fallback(self.principal)
        ):
            # SQL Guard proves read-only, not data entitlement. A role with
            # metric/domain/region limits must stay on the semantic path where
            # AuthorizationPolicy can evaluate those limits.
            base.status = AnalysisStatus.FORBIDDEN
            base.error = ErrorInfo(
                code="sql_fallback_forbidden",
                message=(
                    "this role has data-scope restrictions and cannot use the "
                    "guarded SQL fallback"
                ),
                stage="authorization",
                detail={"role": self.principal.role},
            )
            return base
        result, error = self.sql_fallback.run(
            request.question,
            previous_error=previous_error,
            max_rows=request.max_rows,
        )
        if result is not None:
            base.status = result.status
            base.result_type = ResultType.SQL_QUERY
            base.result = result
            base.explanation = result.explanation
            base.sql_hash = result.sql_hash
            base.data_sources = list(result.tables)
            base.error = None
            return base
        base.error = error or ErrorInfo(
            code="sql_fallback_failed",
            message="guarded SQL fallback did not produce a result",
            stage="sql_fallback",
        )
        base.status = self._fallback_status(base.error.code)
        return base

    @staticmethod
    def _fallback_status(code: str) -> AnalysisStatus:
        if code == "llm_timeout":
            return AnalysisStatus.LLM_TIMEOUT
        if code == "llm_unavailable":
            return AnalysisStatus.LLM_UNAVAILABLE
        if code == "timeout":
            return AnalysisStatus.TIMEOUT
        if code in {
            QueryErrorCode.INVALID_SQL.value,
            QueryErrorCode.MULTIPLE_STATEMENTS.value,
            QueryErrorCode.FORBIDDEN_STATEMENT.value,
            QueryErrorCode.FORBIDDEN_EXPRESSION.value,
            QueryErrorCode.FORBIDDEN_FUNCTION.value,
            QueryErrorCode.UNKNOWN_TABLE.value,
            QueryErrorCode.UNKNOWN_COLUMN.value,
            QueryErrorCode.SELECT_STAR.value,
            QueryErrorCode.UNBOUND_VALUE.value,
            QueryErrorCode.INVALID_PARAMETER.value,
            QueryErrorCode.LIMIT_NOT_LITERAL.value,
            QueryErrorCode.OFFSET_EXCEEDED.value,
            QueryErrorCode.NO_TABLE.value,
            QueryErrorCode.DIALECT_ERROR.value,
            QueryErrorCode.READ_ONLY_VIOLATION.value,
        }:
            return AnalysisStatus.GUARD_REJECTED
        if code in {
            QueryErrorCode.PERMISSION_DENIED.value,
            QueryErrorCode.DATABASE_ERROR.value,
            QueryErrorCode.ROW_LIMIT.value,
        }:
            return AnalysisStatus.EXECUTION_ERROR
        return AnalysisStatus.UNSUPPORTED

    def _execute_intent(self, intent, metric, *, question: str | None = None):
        start = intent.start or self.settings.data_start
        end = intent.end or self.settings.data_end
        filters = list(intent.filters)
        dimensions = self._canonical_dimensions(intent.dimensions, metric)
        operation = intent.operation

        if operation == "investigation":
            if len(intent.metric_candidates) > 1 or metric.name != "sales_amount":
                raise ValueError("investigation requires the sales_amount metric and a single question scope")
            return self._investigate(intent, question=question), ResultType.INVESTIGATION

        if operation == "query":
            request = MetricQueryRequest(
                metric=metric.name,
                start=start,
                end=end,
                grain=intent.grain,
                dimensions=dimensions,
                filters=filters,
            )
            return query_metric(self.session, request), ResultType.METRIC_QUERY

        if operation == "compare":
            kind = intent.comparison_kind or "yoy"
            if kind in {"mom", "yoy"}:
                period = intent.period or end
                request = PeriodComparisonRequest(
                    metric=metric.name,
                    kind=kind,
                    period=period,
                    dimensions=dimensions,
                    filters=filters,
                )
            else:
                request = PeriodComparisonRequest(
                    metric=metric.name,
                    kind=kind,
                    period=intent.period,
                    year=intent.year,
                    quarter=intent.quarter,
                    dimensions=dimensions,
                    filters=filters,
                )
            return compare_periods(self.session, request), ResultType.COMPARISON

        if operation == "top_n":
            dimension = self._require_dimension(intent.dimension, metric, allow_single=True)
            request = TopNRequest(
                metric=metric.name,
                dimension=dimension,
                n=intent.n or 5,
                start=start,
                end=end,
                filters=filters,
                ascending=intent.ascending,
            )
            return get_top_n(self.session, request), ResultType.TOP_N

        if operation == "contribution":
            dimension = self._require_dimension(intent.dimension, metric, allow_single=True)
            if intent.baseline_start is None or intent.baseline_end is None:
                current_year_start = date(start.year, 1, 1)
                current_year_end = date(start.year, 12, 1)
                baseline_start = date(start.year - 1, 1, 1)
                baseline_end = date(start.year - 1, 12, 1)
                if start.month != 1 or end.month != 12:
                    baseline_start = date(start.year - 1, start.month, 1)
                    baseline_end = date(end.year - 1, end.month, 1)
                request = ContributionRequest(
                    metric=metric.name,
                    dimension=dimension,
                    current_start=current_year_start if start.month == 1 and end.month == 12 else start,
                    current_end=current_year_end if start.month == 1 and end.month == 12 else end,
                    baseline_start=baseline_start,
                    baseline_end=baseline_end,
                    filters=filters,
                )
            else:
                request = ContributionRequest(
                    metric=metric.name,
                    dimension=dimension,
                    current_start=start,
                    current_end=end,
                    baseline_start=intent.baseline_start,
                    baseline_end=intent.baseline_end,
                    filters=filters,
                )
            return analyze_contribution(self.session, request), ResultType.CONTRIBUTION

        if operation == "trend":
            request = TrendRequest(
                metric=metric.name,
                start=start,
                end=end,
                grain=intent.grain,
                filters=filters,
            )
            return analyze_trend(self.session, request), ResultType.TREND

        if operation == "anomaly":
            request = AnomalyRequest(
                metric=metric.name,
                start=start,
                end=end,
                method=intent.method,
                threshold=intent.threshold,
                filters=filters,
            )
            return detect_anomalies(self.session, request), ResultType.ANOMALY

        raise ValueError(f"unsupported operation: {operation}")

    def _investigate(self, intent, *, question: str | None = None) -> InvestigationResponse:
        period = intent.period
        if period is None or intent.year is None or intent.quarter is None:
            raise ValueError("investigation requires a quarter period")
        region = intent.region or "EC"
        products = list(intent.products) or list(CORE_PRODUCTS)
        control_regions = [code for code in ("NC", "SC", "WC") if code != region]

        east_filter = [DimensionFilter(dimension="region", operator="eq", value=region)]
        control_filter = [
            DimensionFilter(dimension="region", operator="in", value=control_regions)
        ]
        product_filter = [
            DimensionFilter(dimension="product", operator="in", value=products)
        ]

        east_sales = self._comparison_observation(
            "east_sales",
            "sales_amount",
            intent,
            east_filter,
            filters={"region": region},
        )
        east_core_sales = self._comparison_observation(
            "east_core_product_sales",
            "sales_amount",
            intent,
            east_filter + product_filter,
            filters={"region": region, "products": products},
        )
        east_stockout = self._comparison_observation(
            "east_core_stockout_rate",
            "stockout_rate",
            intent,
            east_filter + product_filter,
            filters={"region": region, "products": products},
        )
        east_inventory = self._comparison_observation(
            "east_core_inventory_days",
            "inventory_days",
            intent,
            east_filter + product_filter,
            filters={"region": region, "products": products},
        )
        control_stockout = self._comparison_observation(
            "other_regions_core_stockout_rate",
            "stockout_rate",
            intent,
            control_filter + product_filter,
            filters={"regions": control_regions, "products": products},
        )
        control_sales = self._comparison_observation(
            "other_regions_sales",
            "sales_amount",
            intent,
            control_filter,
            filters={"regions": control_regions},
        )
        observations = [
            east_sales,
            east_core_sales,
            east_stockout,
            east_inventory,
            control_stockout,
            control_sales,
        ]

        central_complete = (
            east_sales.status == AnalysisStatus.OK
            and east_stockout.status
            in {AnalysisStatus.OK, AnalysisStatus.NOT_CALCULABLE}
            and east_stockout.value is not None
            and east_stockout.baseline_value is not None
        )
        status = AnalysisStatus.OK if central_complete else AnalysisStatus.INSUFFICIENT_DATA
        inference, limitations, disclaimers = self._investigation_conclusion(
            east_sales,
            east_stockout,
            east_inventory,
            control_stockout,
            central_complete=central_complete,
        )

        scope = {
            "region": region,
            "control_regions": control_regions,
            "products": products,
            "metric": "sales_amount",
            "comparison": "qoq",
            "current_period": east_sales.period,
            "baseline_period": east_sales.baseline_period,
        }
        return InvestigationResponse(
            status=status,
            question=question or "跨指标经营调查",
            scope=scope,
            observations=observations,
            inference=inference,
            inference_type="observational",
            limitations=limitations,
            disclaimers=disclaimers,
        )

    def _comparison_observation(
        self,
        name: str,
        metric_name: str,
        intent,
        dimension_filters: list[DimensionFilter],
        *,
        filters: dict[str, Any],
    ) -> Observation:
        request = PeriodComparisonRequest(
            metric=metric_name,
            kind="qoq",
            year=intent.year,
            quarter=intent.quarter,
            filters=dimension_filters,
        )
        response = compare_periods(self.session, request)
        if not response.items:
            return Observation(
                name=name,
                status=response.status,
                metric=response.metric,
                label=response.label,
                period=response.current_period or None,
                baseline_period=response.baseline_period or None,
                unit=response.unit,
                filters=filters,
                reason=response.reason or "comparison did not produce an item",
            )
        item = response.items[0]
        sql_hash = None
        if response.explain is not None:
            sql_hash = hashlib.sha256(response.explain.sql.encode("utf-8")).hexdigest()[:16]
        return Observation(
            name=name,
            status=item.status,
            metric=response.metric,
            label=response.label,
            period=item.current_period,
            baseline_period=item.baseline_period,
            value=item.current_value,
            baseline_value=item.baseline_value,
            change_rate=item.change_rate,
            unit=response.unit,
            filters=filters,
            reason=item.reason,
            sql_hash=sql_hash,
        )

    def _investigation_conclusion(
        self,
        east_sales: Observation,
        east_stockout: Observation,
        east_inventory: Observation,
        control_stockout: Observation,
        *,
        central_complete: bool,
    ) -> tuple[str, list[str], list[str]]:
        disclaimers = [
            "本分析使用演示数据库中的模拟数据；模拟数据呈现的关联不代表真实商业世界的因果关系。",
            "观测到同期变化不等于因果证明，本服务不会据此断言缺货导致了销售下降。",
        ]
        limitations = [
            "仅比较季度环比，未控制价格、促销、渠道结构、需求季节性等其他因素。",
            "未采用实验、准实验或时间序列因果识别方法，结论仅为同期观测描述。",
        ]
        if not central_complete:
            limitations.append("华东销售额或核心商品缺货率存在缺失或不可计算，无法形成相关性判断。")
            return (
                "关键观测数据不完整，不能就‘销售下降是否与缺货有关’形成可靠判断。",
                limitations,
                disclaimers,
            )

        sales_rate = east_sales.change_rate
        stockout_rate_value = east_stockout.value
        stockout_baseline = east_stockout.baseline_value
        stockout_change = east_stockout.change_rate

        def pct(value: Decimal | None) -> str:
            return "N/A" if value is None else f"{value * 100:.2f}%"

        if sales_rate is None:
            limitations.append("华东销售额缺少基期值，无法计算变化率。")
            return (
                "华东销售额缺少基期值，无法判断其变化方向，因此不能推断与缺货的关系。",
                limitations,
                disclaimers,
            )

        if sales_rate >= 0:
            inference = (
                f"华东销售额环比变化为 {pct(sales_rate)}，并未下降；"
                "问题中‘销售下降’的前提在本期数据中不成立，因此不能据此讨论缺货关联。"
            )
            return inference, limitations, disclaimers

        point_change = None
        if stockout_rate_value is not None and stockout_baseline is not None:
            point_change = (stockout_rate_value - stockout_baseline) * 100
        if stockout_change is None:
            movement = (
                f"由 {pct(stockout_baseline)} 变为 {pct(stockout_rate_value)}"
                f"（{point_change:+.2f} 个百分点）"
                if point_change is not None
                else f"为 {pct(stockout_rate_value)}"
            )
        else:
            movement = (
                f"为 {pct(stockout_rate_value)}（基期 {pct(stockout_baseline)}，"
                f"相对变化 {pct(stockout_change)}）"
            )
        prefix = f"华东销售额环比下降 {pct(abs(sales_rate))}；同期核心商品缺货率{movement}。"
        if east_inventory.status == AnalysisStatus.OK and east_inventory.change_rate is not None:
            prefix += (
                f"华东核心商品库存天数由 {east_inventory.baseline_value} 变为 "
                f"{east_inventory.value}（变化 {pct(east_inventory.change_rate)}）。"
            )
        if (
            control_stockout.status
            in {AnalysisStatus.OK, AnalysisStatus.NOT_CALCULABLE}
            and control_stockout.value is not None
            and control_stockout.baseline_value is not None
        ):
            control_points = (
                control_stockout.value - control_stockout.baseline_value
            ) * 100
            prefix += (
                "作为参照，其他区域核心商品缺货率由 "
                f"{pct(control_stockout.baseline_value)} 变为 "
                f"{pct(control_stockout.value)}（{control_points:+.2f} 个百分点）。"
            )
        else:
            limitations.append("其他区域对照组缺货率数据不完整，无法进行区域间对照。")

        if stockout_change is not None:
            stockout_up = stockout_change > 0
        elif stockout_rate_value is not None and stockout_baseline is not None:
            stockout_up = stockout_rate_value > stockout_baseline
        else:
            stockout_up = False
        if stockout_up:
            conclusion = (
                "以上观测与‘核心商品缺货与华东销售下降相关’的假设方向一致，"
                "但属于同期观测相关性，不能证明缺货是销售下降的原因。"
            )
        else:
            conclusion = (
                "同期核心商品缺货率未上升，数据不支持‘缺货导致销售下降’的简单解释，"
                "需要进一步检查价格、促销、渠道和需求等因素。"
            )
        return prefix + conclusion, limitations, disclaimers

    def _canonical_dimensions(self, names: list[str], metric) -> list[str]:
        result: list[str] = []
        for name in names:
            dimension = self.dimensions.resolve(name)
            if dimension is None or dimension.kind == "time":
                raise ValueError(f"unknown or non-categorical dimension: {name}")
            if dimension.name not in metric.dimensions:
                raise ValueError(f"metric {metric.name} does not support dimension {dimension.name}")
            if dimension.name not in result:
                result.append(dimension.name)
        return result

    def _require_dimension(self, name: str | None, metric, *, allow_single: bool) -> str:
        if name:
            dimension = self.dimensions.resolve(name)
            if dimension is None or dimension.kind == "time":
                raise ValueError(f"unknown or non-categorical dimension: {name}")
            if dimension.name not in metric.dimensions:
                raise ValueError(f"metric {metric.name} does not support dimension {dimension.name}")
            return dimension.name
        available = [item for item in metric.dimensions if item != "period"]
        if allow_single and len(available) == 1:
            return available[0]
        raise ValueError(
            f"metric {metric.name} needs an explicit dimension; candidates: {', '.join(available) or 'none'}"
        )

    def _candidate_models(self, names: list[str]) -> list[ResolutionCandidate]:
        candidates = []
        for name in names:
            definition = self.resolver.get(name)
            if definition is not None:
                candidates.append(
                    ResolutionCandidate(
                        name=definition.name,
                        label=definition.label,
                        description=definition.description,
                    )
                )
        return candidates

    def _data_sources(self, result, metric) -> list[str]:
        explain = getattr(result, "explain", None)
        if explain is not None:
            return [explain.source_table]
        return [metric.source_table]

    def _explain(self, result_type: ResultType, result) -> str:
        if result_type == ResultType.INVESTIGATION:
            return result.inference
        if result_type == ResultType.SQL_QUERY:
            return result.explanation or "受控 SQL 补充查询已完成。"
        if result.status != AnalysisStatus.OK:
            return getattr(result, "reason", None) or f"分析状态：{result.status.value}"
        if result_type == ResultType.COMPARISON:
            items = [item for item in result.items if item.status == AnalysisStatus.OK]
            if not items:
                return f"{result.label} 的对比结果不完整。"
            item = items[0]
            return (
                f"{result.label}在 {result.current_period} 为 {_format_number(item.current_value, result.unit)}，"
                f"基期 {result.baseline_period} 为 {_format_number(item.baseline_value, result.unit)}，"
                f"变化 {_format_number(item.absolute_change, result.unit)}，变化率 {_format_rate(item.change_rate)}。"
                "以上为确定性对比，不解释业务原因。"
            )
        if result_type == ResultType.TOP_N:
            names = "、".join(
                f"{item.dimension_label or item.dimension_value}={_format_number(item.value, result.unit)}"
                for item in result.items[:5]
            )
            return f"{result.label}在 {result.start} 至 {result.end} 的 Top {len(result.items)}：{names}。"
        if result_type == ResultType.CONTRIBUTION:
            top = result.items[0] if result.items else None
            detail = (
                f"最大贡献项为 {top.dimension_label or top.dimension_value}"
                f"（{_format_number(top.contribution, result.unit)}，"
                f"占比 {_format_rate(top.contribution_rate)}）。"
                if top is not None
                else ""
            )
            return (
                f"{result.label}总变化 {_format_number(result.total_change, result.unit)}；"
                f"可加总贡献 {_format_number(result.sum_contributions, result.unit)}，"
                f"残差 {_format_number(result.residual, result.unit)}。{detail}"
            )
        if result_type == ResultType.TREND:
            return f"{result.label}在 {result.start} 至 {result.end} 的趋势方向为 {result.direction}。"
        if result_type == ResultType.ANOMALY:
            return f"{result.label}在 {result.start} 至 {result.end} 检出 {len(result.points)} 个异常点。"
        points = result.points
        return f"{result.label}返回 {len(points)} 个时间点，指标口径来源为 {result.source_table}。"
