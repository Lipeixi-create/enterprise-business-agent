import json
from pathlib import Path

from enterprise_business_agent.metric_resolver import MetricResolver
from enterprise_business_agent.semantic import load_dimensions, load_metrics
from enterprise_business_agent.semantic_validation import validate_semantic_against_engine


def test_required_metrics_are_defined():
    metrics = {
        metric.name: metric for metric in load_metrics(Path(__file__).parents[1] / "semantic_layer" / "metrics.json")
    }
    required = {
        "revenue",
        "sales_amount",
        "sales_volume",
        "gross_profit",
        "gross_margin",
        "marketing_spend",
        "campaign_revenue",
        "ROI",
        "CAC",
        "conversion_rate",
        "inventory",
        "demand_units",
        "stockout_units",
        "inventory_days",
        "inventory_turnover",
        "stockout_rate",
        "fulfillment_rate",
        "headcount",
        "turnover_rate",
        "labor_cost",
        "budget",
        "actual",
        "budget_vs_actual",
    }
    assert required <= metrics.keys()
    assert all(metric.source_table and metric.expression and metric.source_fields for metric in metrics.values())
    assert metrics["stockout_rate"].expression == "SUM(stockout_units) / NULLIF(SUM(demand_units), 0)"
    assert metrics["fulfillment_rate"].expression == "SUM(units_sold) / NULLIF(SUM(demand_units), 0)"
    assert metrics["gross_margin"].metric_type == "ratio"
    assert metrics["gross_margin"].numerator and metrics["gross_margin"].denominator
    assert metrics["inventory"].additivity == "semi_additive"
    assert metrics["headcount"].additivity == "semi_additive"


def test_metric_file_is_valid_json_and_v2():
    path = Path(__file__).parents[1] / "semantic_layer" / "metrics.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == "2.0.0"
    assert len(payload["metrics"]) >= 30


def test_dimension_registry_is_defined():
    dimensions = {item.name: item for item in load_dimensions()}
    assert {"period", "region", "channel", "product", "campaign", "department", "supplier"} <= dimensions.keys()
    assert dimensions["region"].table == "regions"
    assert dimensions["region"].fact_column == "region_id"


def test_metric_config_matches_database_schema():
    from sqlalchemy import create_engine

    from enterprise_business_agent import models  # noqa: F401
    from enterprise_business_agent.db import Base

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    assert validate_semantic_against_engine(engine) == []


def test_chinese_aliases_and_ambiguity():
    resolver = MetricResolver()
    assert resolver.resolve("营收").metric_name == "revenue"
    assert resolver.resolve("收入").metric_name == "revenue"
    assert resolver.resolve("销售收入").metric_name == "sales_amount"
    assert resolver.resolve("毛利率").metric_name == "gross_margin"
    assert resolver.resolve("获客成本").metric_name == "CAC"
    assert resolver.resolve("库存周转率").metric_name == "inventory_turnover"
    ambiguous = resolver.resolve("利润率")
    assert ambiguous.status == "ambiguous"
    assert {item.name for item in ambiguous.candidates} == {"gross_margin", "net_margin"}
    assert resolver.resolve("未知指标").status == "unsupported"
