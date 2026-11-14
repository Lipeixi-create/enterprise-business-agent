from collections import defaultdict
from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from enterprise_business_agent.analysis.comparison import ComparisonStatus, compare_qoq, compare_yoy
from enterprise_business_agent.data_generator import generate_data
from enterprise_business_agent.db import Base
from enterprise_business_agent.models import FinanceMonthly, InventorySnapshot, MarketingFact, Product, PurchaseFact, Region, SalesFact
from enterprise_business_agent.quality import run_quality_checks

CORE = ("P001", "P002", "P003")


@pytest.fixture(scope="module")
def dataset():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)
    counts = generate_data(session, date(2024, 1, 1), 24, reset=False)
    yield session, counts
    session.close()


def _sales(session, region_code, product_codes, start, end):
    statement = (
        select(func.sum(SalesFact.units), func.sum(SalesFact.sales_amount))
        .join(Region, SalesFact.region_id == Region.id)
        .join(Product, SalesFact.product_id == Product.id)
        .where(Region.code == region_code, Product.code.in_(product_codes), SalesFact.period.between(start, end))
    )
    units, amount = session.execute(statement).one()
    return int(units or 0), float(amount or 0)


def _inventory(session, region_code, product_codes, start, end):
    statement = (
        select(
            func.sum(InventorySnapshot.demand_units),
            func.sum(InventorySnapshot.units_sold),
            func.sum(InventorySnapshot.stockout_units),
            func.sum(InventorySnapshot.inventory_days * InventorySnapshot.units_sold),
        )
        .join(Region, InventorySnapshot.region_id == Region.id)
        .join(Product, InventorySnapshot.product_id == Product.id)
        .where(Region.code == region_code, Product.code.in_(product_codes), InventorySnapshot.period.between(start, end))
    )
    demand, sold, stockout, days_weighted = session.execute(statement).one()
    demand = int(demand or 0)
    sold = int(sold or 0)
    return {
        "demand": demand,
        "sold": sold,
        "stockout": int(stockout or 0),
        "stockout_rate": (stockout or 0) / demand if demand else 0.0,
        "fulfillment_rate": sold / demand if demand else 0.0,
        "inventory_days": float(days_weighted or 0) / sold if sold else 0.0,
    }


def test_all_fact_tables_cover_continuously_from_2024_01_to_2025_12(dataset):
    session, _ = dataset
    for model in (SalesFact, MarketingFact, FinanceMonthly, PurchaseFact, InventorySnapshot):
        periods = sorted({row.period for row in session.scalars(select(model)).all()})
        assert len(periods) == 24
        assert periods[0] == date(2024, 1, 1)
        assert periods[-1] == date(2025, 12, 1)
        for previous, current in zip(periods, periods[1:]):
            assert (current.year * 12 + current.month) - (previous.year * 12 + previous.month) == 1


def test_east_core_sales_fall_against_comparable_baseline(dataset):
    session, _ = dataset
    units_2024, amount_2024 = _sales(session, "EC", CORE, date(2024, 10, 1), date(2024, 12, 31))
    units_2025, amount_2025 = _sales(session, "EC", CORE, date(2025, 10, 1), date(2025, 12, 31))
    assert units_2025 <= units_2024 * 0.90
    assert amount_2025 <= amount_2024 * 0.90


def test_east_core_inventory_falls_and_stockout_rises(dataset):
    session, _ = dataset
    baseline = _inventory(session, "EC", CORE, date(2024, 10, 1), date(2024, 12, 31))
    current = _inventory(session, "EC", CORE, date(2025, 10, 1), date(2025, 12, 31))
    assert current["inventory_days"] <= baseline["inventory_days"] * 0.60
    assert current["inventory_days"] < 15
    assert current["stockout_rate"] >= 0.20
    assert current["stockout_rate"] >= baseline["stockout_rate"] + 0.15
    assert current["fulfillment_rate"] <= 0.80
    assert current["fulfillment_rate"] <= baseline["fulfillment_rate"] - 0.10


def test_other_regions_keep_a_comparable_baseline(dataset):
    session, _ = dataset
    east = _inventory(session, "EC", CORE, date(2025, 10, 1), date(2025, 12, 31))
    other = _inventory(session, "NC", CORE, date(2025, 10, 1), date(2025, 12, 31))
    assert east["stockout_rate"] >= other["stockout_rate"] + 0.15
    assert other["stockout_rate"] < 0.05

    units_east_2024, amount_east_2024 = _sales(session, "EC", CORE, date(2024, 10, 1), date(2024, 12, 31))
    units_east_2025, amount_east_2025 = _sales(session, "EC", CORE, date(2025, 10, 1), date(2025, 12, 31))
    units_other_2024, amount_other_2024 = _sales(session, "NC", CORE, date(2024, 10, 1), date(2024, 12, 31))
    units_other_2025, amount_other_2025 = _sales(session, "NC", CORE, date(2025, 10, 1), date(2025, 12, 31))
    east_change = amount_east_2025 / amount_east_2024 - 1
    other_change = amount_other_2025 / amount_other_2024 - 1
    assert east_change <= other_change - 0.10
    assert other_change > -0.05
    assert units_east_2025 < units_east_2024
    assert units_other_2025 > units_other_2024


def test_inventory_anomaly_precedes_stockout_and_sales_decline(dataset):
    session, _ = dataset
    first = {}
    for month in range(7, 13):
        current_inventory = _inventory(session, "EC", CORE, date(2025, month, 1), date(2025, month, 1))
        baseline_inventory = _inventory(session, "EC", CORE, date(2024, month, 1), date(2024, month, 1))
        current_units, _ = _sales(session, "EC", CORE, date(2025, month, 1), date(2025, month, 1))
        baseline_units, _ = _sales(session, "EC", CORE, date(2024, month, 1), date(2024, month, 1))
        if "inventory" not in first and baseline_inventory["inventory_days"] > 0 and current_inventory["inventory_days"] <= 0.75 * baseline_inventory["inventory_days"]:
            first["inventory"] = date(2025, month, 1)
        if "stockout" not in first and current_inventory["stockout_rate"] >= 0.10:
            first["stockout"] = date(2025, month, 1)
        if "sales" not in first and baseline_units > 0 and current_units <= 0.85 * baseline_units:
            first["sales"] = date(2025, month, 1)
    assert set(first) == {"inventory", "stockout", "sales"}
    assert first["inventory"] <= first["stockout"] <= first["sales"]


def test_inventory_sales_and_purchase_are_traceable(dataset):
    session, _ = dataset
    sales_units = defaultdict(int)
    for period, region_id, product_id, units in session.execute(
        select(SalesFact.period, SalesFact.region_id, SalesFact.product_id, SalesFact.units)
    ):
        sales_units[(period, region_id, product_id)] += units
    purchase_keys = {
        (period, region_id, product_id)
        for period, region_id, product_id in session.execute(
            select(PurchaseFact.period, PurchaseFact.region_id, PurchaseFact.product_id)
        )
    }
    rows = session.scalars(select(InventorySnapshot)).all()
    assert rows
    for row in rows:
        key = (row.period, row.region_id, row.product_id)
        assert key in purchase_keys
        assert sales_units[key] == row.units_sold
        assert row.demand_units == row.units_sold + row.stockout_units
        expected_fulfillment = row.units_sold / row.demand_units if row.demand_units else 0
        expected_stockout = row.stockout_units / row.demand_units if row.demand_units else 0
        assert abs(float(row.fulfillment_rate) - expected_fulfillment) < 1e-6
        assert abs(float(row.stockout_rate) - expected_stockout) < 1e-6


def test_quality_report_passes_with_all_event_assertions(dataset):
    session, _ = dataset
    report = run_quality_checks(session, date(2024, 1, 1), 24)
    assert report.passed, report.errors
    assert len(report.event_assertions) == 13
    assert all(item.passed for item in report.event_assertions)


def test_generated_revenue_supports_qoq_and_yoy(dataset):
    session, _ = dataset
    revenue = {
        period: amount
        for period, amount in session.execute(
            select(FinanceMonthly.period, func.sum(FinanceMonthly.revenue))
            .join(Region, FinanceMonthly.region_id == Region.id)
            .where(Region.code == "EC")
            .group_by(FinanceMonthly.period)
        )
    }
    qoq = compare_qoq("revenue", revenue, 2025, 4)
    yoy = compare_yoy("revenue", revenue, date(2025, 11, 1))
    assert qoq.status == ComparisonStatus.OK
    assert yoy.status == ComparisonStatus.OK
    assert qoq.change_rate is not None and qoq.change_rate < 0
    assert yoy.change_rate is not None and yoy.change_rate < 0


def test_missing_baseline_returns_insufficient_instead_of_fabricated_rate(dataset):
    session, _ = dataset
    revenue = {
        period: amount
        for period, amount in session.execute(
            select(FinanceMonthly.period, func.sum(FinanceMonthly.revenue)).group_by(FinanceMonthly.period)
        )
    }
    result = compare_yoy("revenue", revenue, date(2024, 6, 1))
    assert result.status == ComparisonStatus.INSUFFICIENT_DATA
    assert result.change_rate is None
    assert result.absolute_change is None
