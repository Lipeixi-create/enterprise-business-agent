from __future__ import annotations

import random
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    Campaign,
    Channel,
    Department,
    FinanceMonthly,
    HRMonthly,
    InventorySnapshot,
    MarketingFact,
    Product,
    PurchaseFact,
    Region,
    SalesFact,
    Supplier,
)

EAST_CORE_PRODUCTS = frozenset({"P001", "P002", "P003"})

# 2025 年下半年部分商品需求预测偏高：目标库存天数上调、采购过量，形成库存积压。
OVERSTOCK_PRODUCTS = frozenset({"P004", "P005"})
OVERSTOCK_START = date(2025, 7, 1)
OVERSTOCK_TARGET_DAYS = {"P004": 72.0, "P005": 66.0}
OVERSTOCK_SUPPLY_CAP = 1.75


# 2025 年下半年华东核心商品供应收缩：采购上限逐月下降，库存先下降，随后出现缺货。
EAST_EVENT_SUPPLY_CAP = {
    date(2025, 7, 1): 0.95,
    date(2025, 8, 1): 0.78,
    date(2025, 9, 1): 0.60,
    date(2025, 10, 1): 0.40,
    date(2025, 11, 1): 0.30,
    date(2025, 12, 1): 0.28,
}

DEFAULT_SUPPLY_CAP = 1.05
BASE_SUPPLY_CAP = {"P006": 0.91, "P008": 0.86}
TARGET_INVENTORY_DAYS = {"P006": 14.0, "P008": 9.0}
DEFAULT_TARGET_INVENTORY_DAYS = 30.0

CAMPAIGN_SCALE = {
    "C101": 1.0,
    "C102": 1.8,
    "C103": 1.0,
    "C104": 2.2,
    "C105": 1.25,
    "C106": 0.7,
    "C001": 1.0,
    "C002": 0.9,
    "C003": 1.8,
    "C004": 1.0,
    "C005": 2.2,
    "C006": 1.25,
    "C007": 0.7,
}


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def ratio(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def month_add(start: date, offset: int) -> date:
    year = start.year + (start.month - 1 + offset) // 12
    month = (start.month - 1 + offset) % 12 + 1
    return date(year, month, 1)


def calendar_index(period: date) -> int:
    """Months since 2024-01; keeps 2024 as the baseline no matter where generation starts."""
    return (period.year - 2024) * 12 + period.month - 1


def supply_cap(region_code: str, product_code: str, period: date) -> float:
    if region_code == "EC" and product_code in EAST_CORE_PRODUCTS:
        return EAST_EVENT_SUPPLY_CAP.get(period, DEFAULT_SUPPLY_CAP)
    if product_code in OVERSTOCK_TARGET_DAYS and period >= OVERSTOCK_START:
        return OVERSTOCK_SUPPLY_CAP
    return BASE_SUPPLY_CAP.get(product_code, DEFAULT_SUPPLY_CAP)


def target_inventory_days(product_code: str, period: date) -> float:
    if product_code in OVERSTOCK_TARGET_DAYS and period >= OVERSTOCK_START:
        return OVERSTOCK_TARGET_DAYS[product_code]
    return TARGET_INVENTORY_DAYS.get(product_code, DEFAULT_TARGET_INVENTORY_DAYS)


def allocate_units(total: int, weights: dict[str, int]) -> dict[str, int]:
    if total <= 0 or sum(weights.values()) <= 0:
        return {key: 0 for key in weights}
    weight_sum = sum(weights.values())
    raw = {key: total * weight / weight_sum for key, weight in weights.items()}
    allocated = {key: int(value) for key, value in raw.items()}
    remainder = total - sum(allocated.values())
    ranked = sorted(weights, key=lambda key: (-(raw[key] - allocated[key]), key))
    for key in ranked[:remainder]:
        allocated[key] += 1
    return allocated


def reset_database(session: Session) -> None:
    for model in (
        InventorySnapshot,
        PurchaseFact,
        FinanceMonthly,
        MarketingFact,
        SalesFact,
        HRMonthly,
        Product,
        Campaign,
        Department,
        Channel,
        Region,
        Supplier,
    ):
        session.execute(delete(model))
    session.commit()


def seed_dimensions(session: Session) -> dict[str, dict[str, int]]:
    regions = [
        Region(code="EC", name="华东"),
        Region(code="NC", name="华北"),
        Region(code="SC", name="华南"),
        Region(code="WC", name="西部"),
    ]
    channels = [
        Channel(code="DIRECT", name="直营"),
        Channel(code="ECOM", name="电商"),
        Channel(code="DIST", name="经销商"),
        Channel(code="RETAIL", name="零售"),
    ]
    suppliers = [
        Supplier(code="SUP-A", name="华信制造", reliability_score=Decimal("0.96")),
        Supplier(code="SUP-B", name="远东供应链", reliability_score=Decimal("0.91")),
        Supplier(code="SUP-C", name="南方精工", reliability_score=Decimal("0.94")),
        Supplier(code="SUP-D", name="新锐零部件", reliability_score=Decimal("0.82")),
    ]
    session.add_all(regions + channels + suppliers)
    session.flush()
    supplier_by_code = {x.code: x.id for x in suppliers}
    products = [
        Product(
            code="P001",
            name="智能办公套装",
            category="办公设备",
            base_price=money(1299),
            base_cost=money(720),
            supplier_id=supplier_by_code["SUP-A"],
        ),
        Product(
            code="P002",
            name="商务显示器",
            category="办公设备",
            base_price=money(1899),
            base_cost=money(1120),
            supplier_id=supplier_by_code["SUP-B"],
        ),
        Product(
            code="P003",
            name="无线键鼠套装",
            category="电脑周边",
            base_price=money(299),
            base_cost=money(145),
            supplier_id=supplier_by_code["SUP-C"],
        ),
        Product(
            code="P004",
            name="移动硬盘",
            category="存储设备",
            base_price=money(699),
            base_cost=money(410),
            supplier_id=supplier_by_code["SUP-A"],
        ),
        Product(
            code="P005",
            name="会议摄像头",
            category="会议设备",
            base_price=money(1599),
            base_cost=money(930),
            supplier_id=supplier_by_code["SUP-B"],
        ),
        Product(
            code="P006",
            name="高端扩展坞",
            category="电脑周边",
            base_price=money(899),
            base_cost=money(560),
            supplier_id=supplier_by_code["SUP-D"],
        ),
        Product(
            code="P007",
            name="企业路由器",
            category="网络设备",
            base_price=money(1199),
            base_cost=money(690),
            supplier_id=supplier_by_code["SUP-C"],
        ),
        Product(
            code="P008",
            name="专业麦克风",
            category="会议设备",
            base_price=money(499),
            base_cost=money(320),
            supplier_id=supplier_by_code["SUP-D"],
        ),
    ]
    campaigns = [
        Campaign(
            code="C101",
            name="春季增长 2024",
            campaign_type="内容营销",
            target_roi=Decimal("2.40"),
            active_from=date(2024, 1, 1),
            active_to=date(2024, 3, 31),
        ),
        Campaign(
            code="C102",
            name="618 大促 2024",
            campaign_type="大促活动",
            target_roi=Decimal("4.20"),
            active_from=date(2024, 6, 1),
            active_to=date(2024, 6, 30),
        ),
        Campaign(
            code="C103",
            name="开学季 2024",
            campaign_type="社交广告",
            target_roi=Decimal("2.10"),
            active_from=date(2024, 8, 1),
            active_to=date(2024, 9, 30),
        ),
        Campaign(
            code="C104",
            name="双十一 2024",
            campaign_type="大促活动",
            target_roi=Decimal("4.80"),
            active_from=date(2024, 11, 1),
            active_to=date(2024, 11, 30),
        ),
        Campaign(
            code="C105",
            name="品牌曝光 Q4 2024",
            campaign_type="品牌广告",
            target_roi=Decimal("1.10"),
            active_from=date(2024, 10, 1),
            active_to=date(2024, 12, 31),
        ),
        Campaign(
            code="C106",
            name="常青投放 2024",
            campaign_type="内容营销",
            target_roi=Decimal("1.80"),
            active_from=date(2024, 1, 1),
            active_to=date(2024, 12, 31),
        ),
        Campaign(
            code="C001",
            name="春季增长",
            campaign_type="内容营销",
            target_roi=Decimal("2.50"),
            active_from=date(2025, 1, 1),
            active_to=date(2025, 3, 31),
        ),
        Campaign(
            code="C002",
            name="新客转化",
            campaign_type="搜索广告",
            target_roi=Decimal("2.80"),
            active_from=date(2025, 2, 1),
            active_to=date(2025, 4, 30),
        ),
        Campaign(
            code="C003",
            name="618 大促",
            campaign_type="大促活动",
            target_roi=Decimal("4.50"),
            active_from=date(2025, 6, 1),
            active_to=date(2025, 6, 30),
        ),
        Campaign(
            code="C004",
            name="开学季",
            campaign_type="社交广告",
            target_roi=Decimal("2.20"),
            active_from=date(2025, 8, 1),
            active_to=date(2025, 9, 30),
        ),
        Campaign(
            code="C005",
            name="双十一",
            campaign_type="大促活动",
            target_roi=Decimal("5.00"),
            active_from=date(2025, 11, 1),
            active_to=date(2025, 11, 30),
        ),
        Campaign(
            code="C006",
            name="品牌曝光 Q4",
            campaign_type="品牌广告",
            target_roi=Decimal("0.65"),
            active_from=date(2025, 10, 1),
            active_to=date(2025, 12, 31),
        ),
        Campaign(
            code="C007",
            name="常青投放 2025",
            campaign_type="内容营销",
            target_roi=Decimal("1.75"),
            active_from=date(2025, 1, 1),
            active_to=date(2025, 12, 31),
        ),
    ]
    departments = [
        Department(code="SALES", name="销售部", domain="Sales"),
        Department(code="MARKETING", name="市场部", domain="Marketing"),
        Department(code="FINANCE", name="财务部", domain="Finance"),
        Department(code="SUPPLY_CHAIN", name="供应链部", domain="Supply Chain"),
        Department(code="HR", name="人力资源部", domain="HR"),
    ]
    session.add_all(products + campaigns + departments)
    session.flush()
    return {
        "regions": {x.code: x.id for x in regions},
        "channels": {x.code: x.id for x in channels},
        "suppliers": supplier_by_code,
        "products": {x.code: x.id for x in products},
        "campaigns": {x.code: x.id for x in campaigns},
        "departments": {x.code: x.id for x in departments},
    }


def generate_data(
    session: Session, start: date = date(2024, 1, 1), months: int = 24, reset: bool = False
) -> dict[str, int]:
    if reset:
        reset_database(session)
    ids = seed_dimensions(session)
    regions = ["EC", "NC", "SC", "WC"]
    channels = ["DIRECT", "ECOM", "DIST", "RETAIL"]
    product_specs = {
        "P001": (1299, 720, 1.00),
        "P002": (1899, 1120, 0.75),
        "P003": (299, 145, 1.80),
        "P004": (699, 410, 1.10),
        "P005": (1599, 930, 0.65),
        "P006": (899, 560, 0.80),
        "P007": (1199, 690, 0.72),
        "P008": (499, 320, 1.35),
    }
    region_factor = {"EC": 1.22, "NC": 1.00, "SC": 0.96, "WC": 0.82}
    channel_factor = {"DIRECT": 0.82, "ECOM": 1.28, "DIST": 0.93, "RETAIL": 0.76}
    seasonality = [0.90, 0.96, 1.03, 0.94, 0.98, 1.23, 1.00, 1.04, 0.91, 1.00, 1.46, 0.88]
    rng = random.Random(20251027)
    sales_by_region: dict[tuple[int, date], list[float]] = defaultdict(lambda: [0.0, 0.0])
    inventory_state: dict[tuple[str, str], int] = {}
    counts = defaultdict(int)

    campaign_list = sorted(session.scalars(select(Campaign)).all(), key=lambda item: item.code)
    product_objects = {x.id: x for x in session.scalars(select(Product)).all()}
    supplier_objects = {x.id: x for x in session.scalars(select(Supplier)).all()}

    for mi in range(months):
        period = month_add(start, mi)
        cal_index = calendar_index(period)
        trend = 1.0 + 0.012 * cal_index
        season = seasonality[period.month - 1]
        active_campaigns = [x for x in campaign_list if x.active_from <= period <= x.active_to]

        for region_code in regions:
            for product_code, (base_price, base_cost, demand_weight) in product_specs.items():
                channel_demand: dict[str, int] = {}
                for channel_code in channels:
                    noise = rng.uniform(0.96, 1.04)
                    channel_demand[channel_code] = max(
                        1,
                        int(
                            760
                            * demand_weight
                            * region_factor[region_code]
                            * channel_factor[channel_code]
                            * season
                            * trend
                            * noise
                        ),
                    )
                total_demand = sum(channel_demand.values())
                target_days = target_inventory_days(product_code, period)
                target_inventory = int(total_demand * target_days / 30)
                state_key = (region_code, product_code)
                beginning = inventory_state.get(state_key)
                if beginning is None:
                    beginning = target_inventory

                desired_receipts = max(0, target_inventory - beginning + total_demand)
                receipts = int(min(desired_receipts, total_demand * supply_cap(region_code, product_code, period)))
                available = beginning + receipts
                units_sold = min(total_demand, available)
                stockout_units = total_demand - units_sold
                ending_inventory = max(0, available - units_sold)
                inventory_state[state_key] = ending_inventory

                allocated = allocate_units(units_sold, channel_demand)
                discount_rate = 0.035 + (0.055 if period.month in (6, 11) else 0.0)
                product_inflation = 1.0 + max(0, cal_index - 17) * 0.027
                if product_code in ("P006", "P008"):
                    product_inflation += 0.025

                for channel_code in channels:
                    units = allocated[channel_code]
                    channel_discount = discount_rate + (0.015 if channel_code == "ECOM" else 0.0)
                    sales_amount = units * base_price * (1 - channel_discount)
                    cogs_amount = units * base_cost * product_inflation
                    campaign_id = None
                    if active_campaigns and channel_code in ("ECOM", "RETAIL"):
                        index = (cal_index + len(product_code) + len(channel_code)) % len(active_campaigns)
                        campaign_id = active_campaigns[index].id
                    session.add(
                        SalesFact(
                            period=period,
                            region_id=ids["regions"][region_code],
                            channel_id=ids["channels"][channel_code],
                            product_id=ids["products"][product_code],
                            campaign_id=campaign_id,
                            units=units,
                            sales_amount=money(sales_amount),
                            discount_amount=money(units * base_price - sales_amount),
                            cogs_amount=money(cogs_amount),
                        )
                    )
                    key = (ids["regions"][region_code], period)
                    sales_by_region[key][0] += sales_amount
                    sales_by_region[key][1] += cogs_amount
                    counts["sales_fact"] += 1

                inventory_days = ending_inventory / (units_sold / 30) if units_sold > 0 else 0.0
                session.add(
                    InventorySnapshot(
                        period=period,
                        region_id=ids["regions"][region_code],
                        product_id=ids["products"][product_code],
                        supplier_id=product_objects[ids["products"][product_code]].supplier_id,
                        inventory_qty=ending_inventory,
                        demand_units=total_demand,
                        units_sold=units_sold,
                        inventory_days=money(inventory_days),
                        stockout_units=stockout_units,
                        stockout_rate=ratio(stockout_units / total_demand),
                        fulfillment_rate=ratio(units_sold / total_demand),
                    )
                )
                counts["inventory_snapshot"] += 1

                product = product_objects[ids["products"][product_code]]
                supplier = supplier_objects[product.supplier_id]
                unit_cost = (
                    float(base_cost) * product_inflation * (1.0 + (1 - float(supplier.reliability_score)) * 0.04)
                )
                session.add(
                    PurchaseFact(
                        period=period,
                        supplier_id=product.supplier_id,
                        product_id=ids["products"][product_code],
                        region_id=ids["regions"][region_code],
                        purchase_qty=receipts,
                        unit_cost=money(unit_cost),
                        purchase_amount=money(receipts * unit_cost),
                    )
                )
                counts["purchase_fact"] += 1

        marketing_by_region: dict[int, float] = defaultdict(float)
        for campaign in active_campaigns:
            for region_code in regions:
                for channel_code in ("ECOM", "RETAIL"):
                    spend = (
                        42000
                        * CAMPAIGN_SCALE[campaign.code]
                        * region_factor[region_code]
                        * (1.10 if channel_code == "ECOM" else 0.78)
                        * (1.0 + rng.uniform(-0.05, 0.05))
                    )
                    if campaign.code == "C006":
                        offset = period.month - 10
                        spend *= 1.0 + 0.15 * offset
                        effective_roi = [0.72, 0.58, 0.45][offset]
                        click_rate = [0.024, 0.022, 0.020][offset]
                        conversion_rate = [0.075, 0.060, 0.050][offset]
                    else:
                        effective_roi = float(campaign.target_roi)
                        click_rate = 0.042
                        conversion_rate = 0.13
                    attributed_revenue = spend * effective_roi * (1.0 + rng.uniform(-0.04, 0.04))
                    impressions = int(spend * 11.0)
                    clicks = int(impressions * click_rate)
                    conversions = max(1, int(clicks * conversion_rate))
                    session.add(
                        MarketingFact(
                            period=period,
                            campaign_id=campaign.id,
                            region_id=ids["regions"][region_code],
                            channel_id=ids["channels"][channel_code],
                            spend=money(spend),
                            impressions=impressions,
                            clicks=clicks,
                            conversions=conversions,
                            attributed_revenue=money(attributed_revenue),
                        )
                    )
                    marketing_by_region[ids["regions"][region_code]] += spend
                    counts["marketing_fact"] += 1

        total_hr_cost = 0.0
        base_heads = {"SALES": 92, "MARKETING": 38, "FINANCE": 24, "SUPPLY_CHAIN": 46, "HR": 12}
        salaries = {"SALES": 17000, "MARKETING": 20500, "FINANCE": 19000, "SUPPLY_CHAIN": 18000, "HR": 21000}
        for dept_code, base_head in base_heads.items():
            q4_marketing_boost = 1.0
            if dept_code == "MARKETING" and period.year == 2025 and period.month >= 10:
                q4_marketing_boost = 1.0 + 0.12 + 0.03 * (period.month - 10)
            headcount = max(1, int(base_head * (1 + 0.008 * cal_index) * q4_marketing_boost))
            hires = max(1, int(headcount * (0.025 if dept_code != "MARKETING" else 0.05)))
            turnover = max(0, int(headcount * (0.012 if dept_code != "SALES" else 0.025)))
            rate = turnover / headcount
            labor = (
                headcount * salaries[dept_code] * (1 + 0.012 * cal_index) * (1.10 if q4_marketing_boost > 1.0 else 1.0)
            )
            session.add(
                HRMonthly(
                    period=period,
                    department_id=ids["departments"][dept_code],
                    headcount=headcount,
                    hires=hires,
                    turnover_count=turnover,
                    turnover_rate=ratio(rate),
                    labor_cost=money(labor),
                )
            )
            total_hr_cost += labor
            counts["hr_monthly"] += 1

        for region_code in regions:
            region_id = ids["regions"][region_code]
            revenue, cogs = sales_by_region[(region_id, period)]
            labor = total_hr_cost * region_factor[region_code] / sum(region_factor.values())
            marketing_spend = marketing_by_region[region_id]
            fixed_expense = 165000 * region_factor[region_code] * (1 + 0.01 * cal_index)
            operating_expense = labor + marketing_spend + fixed_expense
            actual = cogs + operating_expense
            if period.year < 2025:
                budget_factor = 1.02
            else:
                budget_factor = 0.94 if period.month >= 10 else 0.98
            budget = actual * budget_factor
            session.add(
                FinanceMonthly(
                    period=period,
                    region_id=region_id,
                    revenue=money(revenue),
                    cogs_amount=money(cogs),
                    operating_expense=money(operating_expense),
                    marketing_spend=money(marketing_spend),
                    labor_cost=money(labor),
                    budget_amount=money(budget),
                    actual_amount=money(actual),
                )
            )
            counts["finance_monthly"] += 1

    session.commit()
    counts.update(
        {
            "regions": session.query(Region).count(),
            "channels": session.query(Channel).count(),
            "products": session.query(Product).count(),
            "campaigns": session.query(Campaign).count(),
            "departments": session.query(Department).count(),
            "suppliers": session.query(Supplier).count(),
        }
    )
    return dict(counts)
