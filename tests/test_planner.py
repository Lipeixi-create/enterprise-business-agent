from __future__ import annotations

from datetime import date

import pytest

from enterprise_business_agent.analysis.contracts import TimeGrain
from enterprise_business_agent.config import Settings
from enterprise_business_agent.llm import LLMTimeout, LLMUnavailable
from enterprise_business_agent.nl import HybridPlanner, RuleBasedPlanner


@pytest.fixture(scope="module")
def planner() -> RuleBasedPlanner:
    return RuleBasedPlanner(Settings(data_start=date(2024, 1, 1), data_months=24))


def test_chinese_quarter_profit_comparison(planner):
    intent = planner.plan("2025年第四季度净利润同比变化是多少？", as_of=date(2025, 12, 1))
    assert intent.operation == "compare"
    assert intent.metric == "net_profit"
    assert intent.comparison_kind == "yoy_quarter"
    assert (intent.year, intent.quarter) == (2025, 4)
    assert intent.start == date(2025, 10, 1)


def test_current_quarter_uses_last_available_month(planner):
    intent = planner.plan("本季度净利润变化", as_of=date(2025, 12, 1))
    assert intent.operation == "compare"
    assert intent.comparison_kind == "qoq"
    assert (intent.year, intent.quarter) == (2025, 4)


def test_ambiguous_profit_keeps_candidates(planner):
    intent = planner.plan("本季度利润变化", as_of=date(2025, 12, 1))
    assert intent.metric_candidates == ["gross_profit", "net_profit"]
    assert intent.operation == "compare"


def test_east_china_sales_mom(planner):
    intent = planner.plan("华东销售额环比变化", as_of=date(2025, 12, 1))
    assert intent.metric == "sales_amount"
    assert intent.comparison_kind == "mom"
    assert intent.period == date(2025, 12, 1)
    assert [(item.dimension, item.operator, item.value) for item in intent.filters] == [("region", "eq", "EC")]


def test_roi_top_n_uses_campaign_dimension(planner):
    intent = planner.plan("2025年营销活动 ROI Top 5")
    assert intent.operation == "top_n"
    assert intent.metric == "ROI"
    assert intent.dimension == "campaign"
    assert intent.n == 5


def test_stockout_risk_top_n_uses_product_dimension(planner):
    intent = planner.plan("商品库存风险排名前5", as_of=date(2025, 12, 1))
    assert intent.metric == "stockout_rate"
    assert intent.operation == "top_n"
    assert intent.dimension == "product"
    assert intent.n == 5


def test_labor_cost_contribution_defaults_to_year_over_year(planner):
    intent = planner.plan("2025年各部门人力成本贡献", as_of=date(2025, 12, 1))
    assert intent.operation == "contribution"
    assert intent.metric == "labor_cost"
    assert intent.dimension == "department"
    assert (intent.start, intent.end) == (date(2025, 1, 1), date(2025, 12, 1))
    assert (intent.baseline_start, intent.baseline_end) == (date(2024, 1, 1), date(2024, 12, 1))


def test_multi_metric_question_returns_candidates(planner):
    intent = planner.plan("按月列出2025年各区域销售额和毛利率")
    assert intent.operation == "query"
    assert intent.grain == TimeGrain.MONTH
    assert intent.metric_candidates == ["gross_margin", "sales_amount"]
    assert "region" in intent.dimensions


def test_annual_year_over_year_uses_year_comparison(planner):
    intent = planner.plan("2025年净利润同比变化")
    assert intent.operation == "compare"
    assert intent.comparison_kind == "yoy_year"
    assert intent.year == 2025


def test_chinese_month_is_parsed(planner):
    intent = planner.plan("2025年三月销售额")
    assert intent.start == date(2025, 3, 1)
    assert intent.end == date(2025, 3, 1)
    assert intent.grain == TimeGrain.MONTH


def test_recent_months_range(planner):
    intent = planner.plan("最近6个月销售额", as_of=date(2025, 12, 1))
    assert intent.start == date(2025, 7, 1)
    assert intent.end == date(2025, 12, 1)


def test_investigation_scope(planner):
    intent = planner.plan("华东销售下降是否与核心商品缺货有关", as_of=date(2025, 12, 1))
    assert intent.operation == "investigation"
    assert intent.metric == "sales_amount"
    assert intent.region == "EC"
    assert intent.products == ["P001", "P002", "P003"]
    assert (intent.year, intent.quarter) == (2025, 4)


def test_unknown_metric_returns_none(planner):
    assert planner.plan("客户满意度是多少") is None


class _FailingClient:
    model = "fake"

    def __init__(self, error):
        self.error = error

    def complete_json(self, *, system, prompt):
        raise self.error


@pytest.mark.parametrize(
    ("error", "warning"),
    [
        (LLMTimeout("timeout"), "llm_timeout_using_rule_planner"),
        (LLMUnavailable("down"), "llm_unavailable_using_rule_planner"),
    ],
)
def test_hybrid_planner_falls_back_to_rules_and_reports_provider(error, warning):
    hybrid = HybridPlanner(Settings(data_months=24), _FailingClient(error))
    result = hybrid.plan("华东销售额环比变化", as_of=date(2025, 12, 1))
    assert result.provider == "rule"
    assert result.intent.metric == "sales_amount"
    assert warning in result.warnings
    assert result.error is error
