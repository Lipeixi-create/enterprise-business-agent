from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from enterprise_business_agent.db import Base
from enterprise_business_agent.models import (
    Campaign,
    Channel,
    Department,
    FinanceMonthly,
    HRMonthly,
    InventorySnapshot,
    MarketingFact,
    Product,
    Region,
    SalesFact,
    Supplier,
)


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


@pytest.fixture(scope="module")
def fixed_dataset():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine)

    regions = [Region(id=1, code="EC", name="华东"), Region(id=2, code="NC", name="华北")]
    channels = [Channel(id=1, code="DIRECT", name="直营"), Channel(id=2, code="ECOM", name="电商")]
    suppliers = [Supplier(id=1, code="SUP", name="供应商", reliability_score=Decimal("0.95"))]
    products = [
        Product(id=1, code="P001", name="商品一", category="测试", base_price=_money(100), base_cost=_money(60), supplier_id=1),
        Product(id=2, code="P002", name="商品二", category="测试", base_price=_money(100), base_cost=_money(80), supplier_id=1),
    ]
    campaigns = [
        Campaign(id=1, code="C1", name="活动一", campaign_type="测试", target_roi=Decimal("2"), active_from=date(2024, 1, 1), active_to=date(2025, 12, 31)),
        Campaign(id=2, code="C2", name="活动二", campaign_type="测试", target_roi=Decimal("1"), active_from=date(2024, 1, 1), active_to=date(2025, 12, 31)),
    ]
    departments = [
        Department(id=1, code="SALES", name="销售部", domain="Sales"),
        Department(id=2, code="MARKETING", name="市场部", domain="Marketing"),
    ]
    session.add_all(regions + channels + suppliers + products + campaigns + departments)
    session.flush()

    sales = [
        # 2024 baseline
        (date(2024, 1, 1), 1, 1, 1, 10, 1000, 600),
        (date(2024, 1, 1), 2, 1, 2, 10, 3000, 2400),
        (date(2024, 2, 1), 1, 1, 1, 10, 1100, 660),
        (date(2024, 2, 1), 2, 1, 2, 10, 3100, 2480),
        (date(2024, 3, 1), 1, 1, 1, 10, 1200, 720),
        (date(2024, 3, 1), 2, 1, 2, 10, 3200, 2560),
        (date(2024, 4, 1), 1, 1, 1, 10, 1000, 600),
        (date(2024, 4, 1), 2, 1, 2, 10, 3000, 2400),
        # 2025 current
        (date(2025, 1, 1), 1, 1, 1, 10, 1200, 720),
        (date(2025, 1, 1), 2, 1, 2, 10, 3300, 2640),
        (date(2025, 2, 1), 1, 1, 1, 10, 1300, 780),
        (date(2025, 2, 1), 2, 1, 2, 10, 3400, 2720),
        (date(2025, 3, 1), 1, 1, 1, 10, 1400, 840),
        (date(2025, 3, 1), 2, 1, 2, 10, 3500, 2800),
        (date(2025, 4, 1), 1, 1, 1, 10, 1000, 600),
        (date(2025, 4, 1), 2, 1, 2, 10, 3000, 2400),
        # genuine zero, used to distinguish zero from missing
        (date(2025, 5, 1), 1, 1, 1, 0, 0, 0),
    ]
    for period, region_id, channel_id, product_id, units, amount, cogs in sales:
        session.add(
            SalesFact(
                period=period,
                region_id=region_id,
                channel_id=channel_id,
                product_id=product_id,
                units=units,
                sales_amount=_money(amount),
                discount_amount=Decimal("0.00"),
                cogs_amount=_money(cogs),
            )
        )

    finance_revenue = {
        1: {1: 100, 2: 100, 3: 130, 4: 200, 5: 100, 6: 100, 10: 100, 11: 110, 12: 120},
        2: {1: 100, 2: 100, 3: 130, 4: 200, 5: 100, 6: 100, 10: 100, 11: 110, 12: 120},
    }
    finance_current = {1: 150, 2: 180, 3: 200, 4: 220}
    for region_id, monthly in finance_revenue.items():
        for month, revenue in monthly.items():
            session.add(
                FinanceMonthly(
                    period=date(2024, month, 1),
                    region_id=region_id,
                    revenue=_money(revenue),
                    cogs_amount=_money(20),
                    operating_expense=_money(40),
                    marketing_spend=_money(10),
                    labor_cost=_money(5),
                    budget_amount=_money(50),
                    actual_amount=_money(60),
                )
            )
        for month, revenue in finance_current.items():
            session.add(
                FinanceMonthly(
                    period=date(2025, month, 1),
                    region_id=region_id,
                    revenue=_money(revenue),
                    cogs_amount=_money(20),
                    operating_expense=_money(40),
                    marketing_spend=_money(10),
                    labor_cost=_money(5),
                    budget_amount=_money(50),
                    actual_amount=_money(60),
                )
            )

    session.add_all(
        [
            MarketingFact(
                period=date(2025, 1, 1), campaign_id=1, region_id=1, channel_id=2,
                spend=_money(100), impressions=1000, clicks=100, conversions=10, attributed_revenue=_money(300),
            ),
            MarketingFact(
                period=date(2025, 2, 1), campaign_id=2, region_id=1, channel_id=2,
                spend=_money(200), impressions=500, clicks=50, conversions=10, attributed_revenue=_money(200),
            ),
        ]
    )

    inventory = [
        # period, demand, sold, inventory_qty, inventory_days
        (date(2024, 1, 1), 100, 80, 40, 15.0),
        (date(2024, 2, 1), 100, 100, 25, 7.5),
        (date(2024, 3, 1), 100, 90, 30, 10.0),
        (date(2025, 1, 1), 0, 0, 50, 0.0),
        (date(2025, 2, 1), 200, 100, 5, 1.5),
        (date(2025, 3, 1), 200, 120, 10, 2.5),
        (date(2025, 4, 1), 200, 100, 8, 2.0),
    ]
    for period, demand, sold, inventory_qty, inventory_days in inventory:
        stockout = demand - sold
        session.add(
            InventorySnapshot(
                period=period,
                region_id=1,
                product_id=1,
                supplier_id=1,
                inventory_qty=inventory_qty,
                demand_units=demand,
                units_sold=sold,
                inventory_days=Decimal(str(inventory_days)),
                stockout_units=stockout,
                stockout_rate=Decimal(str(stockout / demand if demand else 0)),
                fulfillment_rate=Decimal(str(sold / demand if demand else 0)),
            )
        )

    for year, sales_labor, marketing_labor in ((2024, 1000, 2000), (2025, 1200, 2500)):
        for month in (1, 2, 3):
            session.add(
                HRMonthly(
                    period=date(year, month, 1), department_id=1, headcount=10, hires=1,
                    turnover_count=0, turnover_rate=Decimal("0"), labor_cost=_money(sales_labor),
                )
            )
            session.add(
                HRMonthly(
                    period=date(year, month, 1), department_id=2, headcount=20, hires=1,
                    turnover_count=1, turnover_rate=Decimal("0.05"), labor_cost=_money(marketing_labor),
                )
            )

    session.commit()
    yield session
    session.close()
