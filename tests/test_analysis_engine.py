from datetime import date
from decimal import Decimal

from enterprise_business_agent.analysis.contracts import (
    AnalysisStatus,
    AnomalyRequest,
    ContributionRequest,
    DimensionFilter,
    MetricQueryRequest,
    PeriodComparisonRequest,
    TopNRequest,
    TrendRequest,
)
from enterprise_business_agent.analysis.engine import AnalysisEngine


def _engine(session):
    return AnalysisEngine(session)


def test_sum_aggregation_by_region_with_known_values(fixed_dataset):
    result = _engine(fixed_dataset).query_metric(
        MetricQueryRequest(
            metric="sales_amount",
            start="2025-01-01",
            end="2025-03-31",
            grain="quarter",
            dimensions=["region"],
        )
    )
    assert result.status == AnalysisStatus.OK
    values = {point.dimensions["region"]: point.value for point in result.points}
    assert values["EC"] == Decimal("3900.000000")
    assert values["NC"] == Decimal("10200.000000")
    assert result.points[0].dimension_labels["region"] == "华东"


def test_ratio_metric_uses_weighted_numerator_and_denominator(fixed_dataset):
    result = _engine(fixed_dataset).query_metric(
        MetricQueryRequest(metric="gross_margin", start="2025-01-01", end="2025-03-31", grain="total")
    )
    assert result.status == AnalysisStatus.OK
    assert result.points[0].value == Decimal("0.255319")
    assert result.points[0].numerator == Decimal("3600.000000")
    assert result.points[0].denominator == Decimal("14100.000000")


def test_turnover_ratio_uses_average_inventory_denominator(fixed_dataset):
    result = _engine(fixed_dataset).query_metric(
        MetricQueryRequest(
            metric="inventory_turnover",
            start="2025-02-01",
            end="2025-02-28",
            filters=[DimensionFilter(dimension="product", operator="eq", value="P001")],
        )
    )
    assert result.status == AnalysisStatus.OK
    assert result.points[0].value == Decimal("20.000000")


def test_mom_yoy_qoq_math(fixed_dataset):
    engine = _engine(fixed_dataset)
    mom = engine.compare_periods(PeriodComparisonRequest(metric="revenue", kind="mom", period="2025-02"))
    yoy = engine.compare_periods(PeriodComparisonRequest(metric="revenue", kind="yoy", period="2025-02"))
    qoq = engine.compare_periods(PeriodComparisonRequest(metric="revenue", kind="qoq", year=2025, quarter=1))
    assert mom.status == AnalysisStatus.OK
    assert mom.items[0].current_value == Decimal("360.000000")
    assert mom.items[0].baseline_value == Decimal("300.000000")
    assert mom.items[0].change_rate == Decimal("0.200000")
    assert yoy.items[0].current_value == Decimal("360.000000")
    assert yoy.items[0].baseline_value == Decimal("200.000000")
    assert yoy.items[0].change_rate == Decimal("0.800000")
    assert qoq.items[0].current_value == Decimal("1060.000000")
    assert qoq.items[0].baseline_value == Decimal("660.000000")
    assert qoq.items[0].change_rate == Decimal("0.606061")


def test_quarter_end_token_covers_full_quarter(fixed_dataset):
    result = _engine(fixed_dataset).query_metric(
        MetricQueryRequest(metric="revenue", start="2025Q1", end="2025Q1", grain="quarter")
    )
    assert result.status == AnalysisStatus.OK
    assert result.start == date(2025, 1, 1)
    assert result.end == date(2025, 3, 1)
    assert [point.period for point in result.points] == ["2025Q1"]
    assert result.points[0].value == Decimal("1060.000000")


def test_missing_baseline_is_insufficient_not_fabricated(fixed_dataset):
    result = _engine(fixed_dataset).compare_periods(
        PeriodComparisonRequest(metric="revenue", kind="yoy", period="2024-01")
    )
    assert result.status == AnalysisStatus.INSUFFICIENT_DATA
    assert "baseline period missing" in (result.reason or "")
    assert result.items == []


def test_zero_denominator_is_not_calculable(fixed_dataset):
    result = _engine(fixed_dataset).query_metric(
        MetricQueryRequest(
            metric="stockout_rate",
            start="2025-01-01",
            end="2025-01-31",
            filters=[DimensionFilter(dimension="product", operator="eq", value="P001")],
        )
    )
    assert result.status == AnalysisStatus.NOT_CALCULABLE
    assert result.points[0].value is None
    assert result.points[0].status == AnalysisStatus.NOT_CALCULABLE


def test_zero_value_is_not_the_same_as_missing_data(fixed_dataset):
    engine = _engine(fixed_dataset)
    zero = engine.query_metric(
        MetricQueryRequest(
            metric="sales_amount",
            start="2025-05-01",
            end="2025-05-31",
            filters=[
                DimensionFilter(dimension="region", operator="eq", value="EC"),
                DimensionFilter(dimension="product", operator="eq", value="P001"),
            ],
        )
    )
    missing = engine.query_metric(
        MetricQueryRequest(
            metric="sales_amount",
            start="2025-05-01",
            end="2025-05-31",
            filters=[DimensionFilter(dimension="product", operator="eq", value="P002")],
        )
    )
    assert zero.status == AnalysisStatus.OK
    assert zero.points[0].value == Decimal("0.000000")
    assert missing.status == AnalysisStatus.INSUFFICIENT_DATA
    assert missing.points == []


def test_incomplete_time_range_is_rejected(fixed_dataset):
    result = _engine(fixed_dataset).query_metric(
        MetricQueryRequest(metric="revenue", start="2025-01-01", end="2025-07-31")
    )
    assert result.status == AnalysisStatus.INSUFFICIENT_DATA
    assert result.missing_periods == ["2025-05-01", "2025-06-01", "2025-07-01"]


def test_top_n_ranking_and_share(fixed_dataset):
    result = _engine(fixed_dataset).get_top_n(
        TopNRequest(metric="sales_amount", dimension="region", start="2025-01-01", end="2025-03-31", n=2)
    )
    assert result.status == AnalysisStatus.OK
    assert [item.dimension_value for item in result.items] == ["NC", "EC"]
    assert result.items[0].value == Decimal("10200.000000")
    assert result.items[0].share == Decimal("0.723404")


def test_contribution_math_and_conservation(fixed_dataset):
    result = _engine(fixed_dataset).analyze_contribution(
        ContributionRequest(
            metric="sales_amount",
            dimension="region",
            current_start="2025-01-01",
            current_end="2025-03-31",
            baseline_start="2024-01-01",
            baseline_end="2024-03-31",
        )
    )
    assert result.status == AnalysisStatus.OK
    assert result.total_current == Decimal("14100.000000")
    assert result.total_baseline == Decimal("12600.000000")
    assert result.total_change == Decimal("1500.000000")
    assert result.denominator == Decimal("1500.000000")
    assert result.allocatable is True
    values = {item.dimension_value: item for item in result.items}
    assert values["EC"].contribution == Decimal("600.000000")
    assert values["EC"].contribution_rate == Decimal("0.400000")
    assert values["NC"].contribution == Decimal("900.000000")
    assert result.sum_contributions == Decimal("1500.000000")
    assert result.residual == Decimal("0.000000")


def test_contribution_with_zero_total_change_is_not_calculable(fixed_dataset):
    result = _engine(fixed_dataset).analyze_contribution(
        ContributionRequest(
            metric="sales_amount",
            dimension="region",
            current_start="2025-01-01",
            current_end="2025-03-31",
            baseline_start="2025-01-01",
            baseline_end="2025-03-31",
        )
    )
    assert result.status == AnalysisStatus.NOT_CALCULABLE
    assert result.denominator == Decimal("0.000000")
    assert result.allocatable is False
    assert all(item.contribution == Decimal("0.000000") for item in result.items)
    assert all(item.contribution_rate is None for item in result.items)


def test_contribution_rejects_non_additive_metric(fixed_dataset):
    result = _engine(fixed_dataset).analyze_contribution(
        ContributionRequest(
            metric="gross_margin",
            dimension="region",
            current_start="2025-01-01",
            current_end="2025-03-31",
            baseline_start="2024-01-01",
            baseline_end="2024-03-31",
        )
    )
    assert result.status == AnalysisStatus.NOT_APPLICABLE
    assert result.allocatable is False


def test_trend_identifies_growth(fixed_dataset):
    result = _engine(fixed_dataset).analyze_trend(
        TrendRequest(metric="revenue", start="2024-01-01", end="2024-04-30")
    )
    assert result.status == AnalysisStatus.OK
    assert result.direction == "growth"
    assert len(result.points) == 4
    assert result.stats["total_change_rate"] == Decimal("1.000000")


def test_mom_anomaly_returns_rule_and_baseline(fixed_dataset):
    result = _engine(fixed_dataset).detect_anomalies(
        AnomalyRequest(
            metric="revenue",
            start="2024-01-01",
            end="2024-06-30",
            method="mom_threshold",
            threshold=0.35,
        )
    )
    assert result.status == AnalysisStatus.OK
    assert [point.period for point in result.points] == ["2024-04", "2024-05"]
    assert result.points[0].baseline_value == Decimal("260.000000")
    assert result.points[0].change_rate == Decimal("0.538462")
    assert "threshold" in result.rule


def test_zscore_anomaly(fixed_dataset):
    result = _engine(fixed_dataset).detect_anomalies(
        AnomalyRequest(
            metric="revenue",
            start="2024-01-01",
            end="2024-04-30",
            method="zscore",
            z_threshold=2.0,
            min_history=3,
        )
    )
    assert result.status == AnalysisStatus.OK
    assert len(result.points) == 1
    assert result.points[0].period == "2024-04"
    assert result.points[0].zscore is not None


def test_ambiguous_and_unsupported_metrics(fixed_dataset):
    engine = _engine(fixed_dataset)
    ambiguous = engine.query_metric(MetricQueryRequest(metric="利润率", start="2025-01", end="2025-03"))
    unsupported = engine.query_metric(MetricQueryRequest(metric="不存在的指标", start="2025-01", end="2025-03"))
    assert ambiguous.status == AnalysisStatus.AMBIGUOUS
    assert "歧义" in (ambiguous.reason or "")
    assert unsupported.status == AnalysisStatus.UNSUPPORTED


def test_invalid_dimension_is_invalid_request(fixed_dataset):
    result = _engine(fixed_dataset).query_metric(
        MetricQueryRequest(
            metric="sales_amount",
            start="2025-01",
            end="2025-03",
            dimensions=["department"],
        )
    )
    assert result.status == AnalysisStatus.INVALID_REQUEST
    assert "does not support dimension" in (result.reason or "")


def test_semi_additive_uses_last_month(fixed_dataset):
    result = _engine(fixed_dataset).query_metric(
        MetricQueryRequest(metric="headcount", start="2025-01", end="2025-03", grain="total")
    )
    assert result.status == AnalysisStatus.OK
    # 10 + 20 employees, period-end value rather than a 3-month sum
    assert result.points[0].value == Decimal("30.000000")
    assert result.aggregation == "last"
