from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from enterprise_business_agent.analysis.contracts import AnalysisStatus
from enterprise_business_agent.config import Settings
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base
from enterprise_business_agent.nl import NaturalLanguageAnalyzer, NaturalLanguageRequest, ResultType


@pytest.fixture(scope="module")
def demo_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    generate_data(session, date(2024, 1, 1), 24, reset=False)
    yield session
    session.close()


@pytest.fixture(scope="module")
def analyzer(demo_session):
    return NaturalLanguageAnalyzer(demo_session, Settings())


def _ask(analyzer, question):
    return analyzer.analyze(NaturalLanguageRequest(question=question))


def test_demo_quarterly_profit_change(analyzer):
    response = _ask(analyzer, "2025年第四季度净利润环比变化")
    assert response.status == AnalysisStatus.OK
    assert response.result_type == ResultType.COMPARISON
    item = response.result.items[0]
    assert item.current_value == Decimal("64875265.76")
    assert item.baseline_value == Decimal("83681849.06")
    assert item.absolute_change == Decimal("-18806583.30")
    assert round(item.change_rate, 4) == Decimal("-0.2247")


def test_demo_east_china_sales_mom(analyzer):
    response = _ask(analyzer, "华东销售额环比变化")
    assert response.status == AnalysisStatus.OK
    item = response.result.items[0]
    assert item.current_value == Decimal("18129454.19")
    assert item.baseline_value == Decimal("28548840.50")
    assert round(item.change_rate, 4) == Decimal("-0.3650")


def test_demo_marketing_roi_ranking(analyzer):
    response = _ask(analyzer, "2025年营销活动 ROI Top 5")
    assert response.status == AnalysisStatus.OK
    assert response.result_type == ResultType.TOP_N
    values = [(item.dimension_value, round(item.value, 6)) for item in response.result.items]
    assert values == [
        ("C005", Decimal("4.971426")),
        ("C003", Decimal("4.511720")),
        ("C002", Decimal("2.773576")),
        ("C001", Decimal("2.492591")),
        ("C004", Decimal("2.195895")),
    ]


def test_demo_inventory_risk_ranking(analyzer):
    response = _ask(analyzer, "2025年第四季度商品库存风险排名前5")
    assert response.status == AnalysisStatus.OK
    values = [(item.dimension_value, round(item.value, 6)) for item in response.result.items]
    assert values == [
        ("P003", Decimal("0.178584")),
        ("P002", Decimal("0.172700")),
        ("P001", Decimal("0.171035")),
        ("P008", Decimal("0.140135")),
        ("P006", Decimal("0.090156")),
    ]


def test_demo_department_labor_cost_contribution(analyzer):
    response = _ask(analyzer, "2025年各部门人力成本贡献")
    assert response.status == AnalysisStatus.OK
    result = response.result
    assert result.total_change == Decimal("13202974.00")
    assert result.sum_contributions == Decimal("13202974.00")
    values = [
        (item.dimension_value, item.contribution, round(item.contribution_rate, 4))
        for item in result.items
    ]
    assert values == [
        ("SALES", Decimal("4967400.00"), Decimal("0.3762")),
        ("MARKETING", Decimal("3373890.00"), Decimal("0.2555")),
        ("SUPPLY_CHAIN", Decimal("2613240.00"), Decimal("0.1979")),
        ("FINANCE", Decimal("1452208.00"), Decimal("0.1100")),
        ("HR", Decimal("796236.00"), Decimal("0.0603")),
    ]


def test_cross_metric_investigation_separates_observation_from_inference(analyzer):
    response = _ask(analyzer, "华东销售下降是否与核心商品缺货有关")
    assert response.status == AnalysisStatus.OK
    assert response.result_type == ResultType.INVESTIGATION
    result = response.result
    assert result.inference_type == "observational"

    observations = {item.name: item for item in result.observations}
    east_sales = observations["east_sales"]
    east_stockout = observations["east_core_stockout_rate"]
    east_inventory = observations["east_core_inventory_days"]
    control_stockout = observations["other_regions_core_stockout_rate"]

    assert east_sales.change_rate < 0
    assert east_stockout.value > Decimal("0.20")
    assert east_stockout.baseline_value == Decimal("0.00")
    assert east_inventory.change_rate == Decimal("-1.0000") or east_inventory.value == 0
    assert control_stockout.value is not None

    assert "不能证明" in result.inference
    assert result.disclaimers
    assert any("不代表真实商业世界的因果关系" in item for item in result.disclaimers)
    assert result.limitations
