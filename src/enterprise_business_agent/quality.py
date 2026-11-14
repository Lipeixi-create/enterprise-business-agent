from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from .models import (
    Base, Campaign, Channel, Department, FinanceMonthly, HRMonthly, InventorySnapshot, MarketingFact,
    Product, PurchaseFact, Region, SalesFact, Supplier,
)
from .schemas import DataQualityReport, EventAssertion

FACT_MODELS = (SalesFact, MarketingFact, FinanceMonthly, PurchaseFact, InventorySnapshot, HRMonthly)
DIM_MODELS = (Region, Channel, Supplier, Product, Campaign, Department)
NUMERIC_FIELDS = {
    SalesFact: ("units", "sales_amount", "discount_amount", "cogs_amount"),
    MarketingFact: ("spend", "impressions", "clicks", "conversions", "attributed_revenue"),
    FinanceMonthly: ("revenue", "cogs_amount", "operating_expense", "marketing_spend", "labor_cost", "budget_amount", "actual_amount"),
    PurchaseFact: ("purchase_qty", "unit_cost", "purchase_amount"),
    InventorySnapshot: ("inventory_qty", "demand_units", "units_sold", "inventory_days", "stockout_units", "stockout_rate", "fulfillment_rate"),
    HRMonthly: ("headcount", "hires", "turnover_count", "turnover_rate", "labor_cost"),
}
EAST = "EC"
CORE_PRODUCTS = {"P001", "P002", "P003"}
OVERSTOCK_PRODUCTS = {"P004", "P005"}
ALL_REGIONS = {"EC", "NC", "SC", "WC"}


def _as_float(value):
    return float(value) if isinstance(value, Decimal) else value


def _expected_periods(start: date, months: int) -> set[date]:
    return {date(start.year + (start.month - 1 + index) // 12, (start.month - 1 + index) % 12 + 1, 1) for index in range(months)}


def _close(left: float, right: float, absolute: float = 5.0, relative: float = 0.001) -> bool:
    return abs(left - right) <= max(absolute, abs(left) * relative, abs(right) * relative)


def _pct(current: float, baseline: float) -> float | None:
    return (current - baseline) / baseline if baseline else None


def _event_assertions(session: Session) -> list[EventAssertion]:
    region_codes = {row.id: row.code for row in session.scalars(select(Region)).all()}
    product_codes = {row.id: row.code for row in session.scalars(select(Product)).all()}

    sales: dict[tuple[str, str, date], list] = defaultdict(lambda: [0, Decimal("0")])
    for period, region_id, product_id, units, amount in session.execute(
        select(SalesFact.period, SalesFact.region_id, SalesFact.product_id, SalesFact.units, SalesFact.sales_amount)
    ):
        key = (region_codes[region_id], product_codes[product_id], period)
        sales[key][0] += units
        sales[key][1] += amount

    inventory: dict[tuple[str, str, date], InventorySnapshot] = {}
    for row in session.scalars(select(InventorySnapshot)).all():
        inventory[(region_codes[row.region_id], product_codes[row.product_id], row.period)] = row

    purchase_keys = {
        (region_codes[region_id], product_codes[product_id], period)
        for period, region_id, product_id in session.execute(
            select(PurchaseFact.period, PurchaseFact.region_id, PurchaseFact.product_id)
        )
    }

    def totals(region_set: set[str], product_set: set[str], start: date, end: date) -> dict[str, float]:
        units = 0
        amount = Decimal("0")
        demand = 0
        sold = 0
        stockout = 0
        inventory_days_weighted = 0.0
        inventory_days_weight = 0
        for (region_code, product_code, period), (row_units, row_amount) in sales.items():
            if region_code in region_set and product_code in product_set and start <= period <= end:
                units += row_units
                amount += row_amount
        for (region_code, product_code, period), row in inventory.items():
            if region_code in region_set and product_code in product_set and start <= period <= end:
                demand += row.demand_units
                sold += row.units_sold
                stockout += row.stockout_units
                if row.units_sold:
                    inventory_days_weighted += float(row.inventory_days) * row.units_sold
                    inventory_days_weight += row.units_sold
        return {
            "units": units,
            "amount": float(amount),
            "demand": demand,
            "sold": sold,
            "stockout": stockout,
            "inventory_days": inventory_days_weighted / inventory_days_weight if inventory_days_weight else 0.0,
            "stockout_rate": stockout / demand if demand else 0.0,
            "fulfillment_rate": sold / demand if demand else 0.0,
        }

    def assertion(name: str, passed: bool, detail: str, observed: dict[str, float | int | str | None]) -> EventAssertion:
        return EventAssertion(name=name, passed=passed, detail=detail, observed=observed)

    q4_2024 = (date(2024, 10, 1), date(2024, 12, 31))
    q4_2025 = (date(2025, 10, 1), date(2025, 12, 31))
    east_2024 = totals({EAST}, CORE_PRODUCTS, *q4_2024)
    east_2025 = totals({EAST}, CORE_PRODUCTS, *q4_2025)
    other_2024 = totals(ALL_REGIONS - {EAST}, CORE_PRODUCTS, *q4_2024)
    other_2025 = totals(ALL_REGIONS - {EAST}, CORE_PRODUCTS, *q4_2025)

    east_sales_change = _pct(east_2025["amount"], east_2024["amount"])
    east_units_change = _pct(east_2025["units"], east_2024["units"])
    east_inventory_change = _pct(east_2025["inventory_days"], east_2024["inventory_days"])
    other_sales_change = _pct(other_2025["amount"], other_2024["amount"])

    results = [
        assertion(
            "east_core_sales_decline",
            east_sales_change is not None and east_sales_change <= -0.10,
            "2025Q4 华东核心商品销售额相对 2024Q4 显著下降",
            {"sales_2024q4": round(east_2024["amount"], 2), "sales_2025q4": round(east_2025["amount"], 2), "change_rate": None if east_sales_change is None else round(east_sales_change, 6)},
        ),
        assertion(
            "east_core_units_decline",
            east_units_change is not None and east_units_change <= -0.10,
            "2025Q4 华东核心商品销量相对 2024Q4 显著下降",
            {"units_2024q4": east_2024["units"], "units_2025q4": east_2025["units"], "change_rate": None if east_units_change is None else round(east_units_change, 6)},
        ),
        assertion(
            "east_core_inventory_days_drop",
            east_inventory_change is not None and east_inventory_change <= -0.40 and east_2025["inventory_days"] < 15,
            "2025Q4 华东核心商品库存天数相对 2024Q4 显著下降",
            {"inventory_days_2024q4": round(east_2024["inventory_days"], 3), "inventory_days_2025q4": round(east_2025["inventory_days"], 3), "change_rate": None if east_inventory_change is None else round(east_inventory_change, 6)},
        ),
        assertion(
            "east_core_stockout_rate_rise",
            east_2025["stockout_rate"] >= 0.20 and east_2025["stockout_rate"] >= east_2024["stockout_rate"] + 0.15,
            "2025Q4 华东核心商品缺货率显著上升",
            {"stockout_rate_2024q4": round(east_2024["stockout_rate"], 6), "stockout_rate_2025q4": round(east_2025["stockout_rate"], 6)},
        ),
        assertion(
            "east_core_fulfillment_rate_drop",
            east_2025["fulfillment_rate"] <= 0.80 and east_2025["fulfillment_rate"] <= east_2024["fulfillment_rate"] - 0.10,
            "2025Q4 华东核心商品需求满足率显著下降",
            {"fulfillment_rate_2024q4": round(east_2024["fulfillment_rate"], 6), "fulfillment_rate_2025q4": round(east_2025["fulfillment_rate"], 6)},
        ),
        assertion(
            "regional_contrast",
            east_2025["stockout_rate"] >= other_2025["stockout_rate"] + 0.15
            and east_sales_change is not None
            and other_sales_change is not None
            and east_sales_change <= other_sales_change - 0.10,
            "华东异常显著强于其他区域，形成区域对照",
            {
                "east_stockout_rate": round(east_2025["stockout_rate"], 6),
                "other_stockout_rate": round(other_2025["stockout_rate"], 6),
                "east_sales_change": None if east_sales_change is None else round(east_sales_change, 6),
                "other_sales_change": None if other_sales_change is None else round(other_sales_change, 6),
            },
        ),
    ]

    def monthly(region_set: set[str], product_set: set[str], year: int, month: int) -> dict[str, float]:
        period = date(year, month, 1)
        return totals(region_set, product_set, period, period)

    def first_month(metric: str) -> date | None:
        for month in range(7, 13):
            current = monthly({EAST}, CORE_PRODUCTS, 2025, month)
            baseline = monthly({EAST}, CORE_PRODUCTS, 2024, month)
            if metric == "inventory":
                if baseline["inventory_days"] > 0 and current["inventory_days"] <= 0.75 * baseline["inventory_days"]:
                    return date(2025, month, 1)
            elif metric == "stockout":
                if current["stockout_rate"] >= 0.10:
                    return date(2025, month, 1)
            elif metric == "sales":
                if baseline["units"] > 0 and current["units"] <= 0.85 * baseline["units"]:
                    return date(2025, month, 1)
        return None

    inventory_month = first_month("inventory")
    stockout_month = first_month("stockout")
    sales_month = first_month("sales")
    results.append(
        assertion(
            "causal_time_ordering",
            inventory_month is not None
            and stockout_month is not None
            and sales_month is not None
            and inventory_month <= stockout_month <= sales_month,
            "库存下降不晚于缺货率上升，缺货率上升不晚于销量下降",
            {
                "inventory_drop_month": inventory_month.isoformat() if inventory_month else None,
                "stockout_rise_month": stockout_month.isoformat() if stockout_month else None,
                "sales_drop_month": sales_month.isoformat() if sales_month else None,
            },
        )
    )

    overstock_2024 = totals(ALL_REGIONS, OVERSTOCK_PRODUCTS, *q4_2024)
    overstock_2025 = totals(ALL_REGIONS, OVERSTOCK_PRODUCTS, *q4_2025)
    overstock_change = _pct(overstock_2025["inventory_days"], overstock_2024["inventory_days"])
    results.append(
        assertion(
            "overstock_risk_products",
            overstock_change is not None
            and overstock_change >= 0.50
            and overstock_2025["inventory_days"] >= 50,
            "P004/P005 在 2025Q4 出现库存积压，库存天数显著高于 2024Q4",
            {
                "inventory_days_2024q4": round(overstock_2024["inventory_days"], 3),
                "inventory_days_2025q4": round(overstock_2025["inventory_days"], 3),
                "change_rate": None if overstock_change is None else round(overstock_change, 6),
            },
        )
    )

    campaign_roi: dict[date, float] = {}
    for period, spend, revenue in session.execute(
        select(
            MarketingFact.period,
            func.sum(MarketingFact.spend),
            func.sum(MarketingFact.attributed_revenue),
        )
        .join(Campaign, MarketingFact.campaign_id == Campaign.id)
        .where(Campaign.code == "C006")
        .group_by(MarketingFact.period)
    ):
        if spend:
            campaign_roi[period] = float(revenue) / float(spend)
    roi_oct = campaign_roi.get(date(2025, 10, 1))
    roi_dec = campaign_roi.get(date(2025, 12, 1))
    roi_values = [campaign_roi[key] for key in sorted(campaign_roi)]
    roi_average = sum(roi_values) / len(roi_values) if roi_values else None
    results.append(
        assertion(
            "marketing_roi_decline_q4",
            roi_oct is not None
            and roi_dec is not None
            and roi_dec <= roi_oct * 0.75
            and roi_average is not None
            and roi_average <= 0.65,
            "C006 在 2025Q4 投入上升且 ROI 逐月下降",
            {
                "roi_2025_10": None if roi_oct is None else round(roi_oct, 6),
                "roi_2025_12": None if roi_dec is None else round(roi_dec, 6),
                "roi_q4_average": None if roi_average is None else round(roi_average, 6),
            },
        )
    )

    def finance_totals(start: date, end: date) -> dict[str, float]:
        budget, actual, revenue = session.execute(
            select(
                func.sum(FinanceMonthly.budget_amount),
                func.sum(FinanceMonthly.actual_amount),
                func.sum(FinanceMonthly.revenue),
            ).where(FinanceMonthly.period.between(start, end))
        ).one()
        return {
            "budget": float(budget or 0),
            "actual": float(actual or 0),
            "revenue": float(revenue or 0),
            "profit": float(revenue or 0) - float(actual or 0),
        }

    finance_2024 = finance_totals(*q4_2024)
    finance_2025 = finance_totals(*q4_2025)
    profit_change = _pct(finance_2025["profit"], finance_2024["profit"])
    results.append(
        assertion(
            "budget_overrun_q4",
            finance_2024["actual"] <= finance_2024["budget"]
            and finance_2025["actual"] > finance_2025["budget"],
            "2024Q4 实际费用低于预算，2025Q4 转为超预算",
            {
                "variance_2024q4": round(finance_2024["actual"] - finance_2024["budget"], 2),
                "variance_2025q4": round(finance_2025["actual"] - finance_2025["budget"], 2),
            },
        )
    )
    results.append(
        assertion(
            "net_profit_decline_q4",
            profit_change is not None and profit_change <= -0.10,
            "2025Q4 净利润相对 2024Q4 显著下降",
            {
                "profit_2024q4": round(finance_2024["profit"], 2),
                "profit_2025q4": round(finance_2025["profit"], 2),
                "change_rate": None if profit_change is None else round(profit_change, 6),
            },
        )
    )

    marketing_labor = {}
    for year in (2024, 2025):
        marketing_labor[year] = float(
            session.scalar(
                select(func.sum(HRMonthly.labor_cost))
                .join(Department, HRMonthly.department_id == Department.id)
                .where(
                    Department.code == "MARKETING",
                    HRMonthly.period.between(date(year, 10, 1), date(year, 12, 31)),
                )
            )
            or 0
        )
    labor_change = _pct(marketing_labor[2025], marketing_labor[2024])
    results.append(
        assertion(
            "marketing_labor_cost_growth_q4",
            labor_change is not None and labor_change >= 0.10,
            "市场部 2025Q4 人力成本相对 2024Q4 明显增长",
            {
                "labor_2024q4": round(marketing_labor[2024], 2),
                "labor_2025q4": round(marketing_labor[2025], 2),
                "change_rate": None if labor_change is None else round(labor_change, 6),
            },
        )
    )

    missing_sales = [key for key in inventory if key not in sales]
    missing_purchase = [key for key in inventory if key not in purchase_keys]
    unit_mismatch = sum(1 for key, row in inventory.items() if sales.get(key, [0])[0] != row.units_sold)
    arithmetic_mismatch = sum(
        1 for row in inventory.values()
        if row.demand_units != row.units_sold + row.stockout_units
    )
    results.append(
        assertion(
            "traceability_and_consistency",
            not missing_sales and not missing_purchase and unit_mismatch == 0 and arithmetic_mismatch == 0,
            "库存、销售、采购可按区域-商品-月份追溯，且数量关系一致",
            {
                "inventory_rows": len(inventory),
                "missing_sales_rows": len(missing_sales),
                "missing_purchase_rows": len(missing_purchase),
                "unit_mismatch_rows": unit_mismatch,
                "arithmetic_mismatch_rows": arithmetic_mismatch,
            },
        )
    )
    return results


def run_quality_checks(session: Session, expected_start: date = date(2024, 1, 1), expected_months: int = 24) -> DataQualityReport:
    checks: list[str] = []
    errors: list[str] = []
    row_counts: dict[str, int] = {}
    expected_periods = _expected_periods(expected_start, expected_months)
    all_tables = DIM_MODELS + FACT_MODELS
    for model in all_tables:
        rows = session.scalars(select(model)).all()
        table_name = model.__tablename__
        row_counts[table_name] = len(rows)
        if not rows:
            errors.append(f"{table_name}: no rows")
        ids = [row.id for row in rows]
        if len(ids) != len(set(ids)):
            errors.append(f"{table_name}: duplicate primary key")
        mapper = inspect(model)
        for row in rows:
            for column in mapper.columns:
                if not column.nullable and getattr(row, column.name) is None and column.name != "created_at":
                    errors.append(f"{table_name}.{column.name}: unexpected null")
                    break
            if model in NUMERIC_FIELDS:
                for field in NUMERIC_FIELDS[model]:
                    value = getattr(row, field)
                    if value is None or value < 0:
                        errors.append(f"{table_name}.{field}: negative or null")
        if model in FACT_MODELS:
            periods = {row.period for row in rows}
            if periods != expected_periods:
                errors.append(f"{table_name}.period: expected {expected_months} continuous months, got {len(periods)}")
    checks.append("primary keys and required values")
    checks.append("numeric fields are non-negative")
    checks.append(f"all monthly fact periods cover the continuous {expected_months}-month range")

    fk_checks = [
        (SalesFact, "region_id", Region), (SalesFact, "channel_id", Channel), (SalesFact, "product_id", Product),
        (MarketingFact, "campaign_id", Campaign), (MarketingFact, "region_id", Region), (MarketingFact, "channel_id", Channel),
        (FinanceMonthly, "region_id", Region), (PurchaseFact, "supplier_id", Supplier), (PurchaseFact, "product_id", Product), (PurchaseFact, "region_id", Region),
        (InventorySnapshot, "region_id", Region), (InventorySnapshot, "product_id", Product), (InventorySnapshot, "supplier_id", Supplier),
        (HRMonthly, "department_id", Department),
    ]
    for child, fk, parent in fk_checks:
        parent_ids = set(session.scalars(select(parent.id)).all())
        orphan_count = sum(getattr(row, fk) not in parent_ids for row in session.scalars(select(child)).all())
        if orphan_count:
            errors.append(f"{child.__tablename__}.{fk}: {orphan_count} orphan rows")
    checks.append("foreign-key references are complete")

    inventory_rows = session.scalars(select(InventorySnapshot)).all()
    inventory_errors = 0
    for row in inventory_rows:
        if row.demand_units != row.units_sold + row.stockout_units:
            inventory_errors += 1
            continue
        expected_fulfillment = row.units_sold / row.demand_units if row.demand_units else 0.0
        expected_stockout = row.stockout_units / row.demand_units if row.demand_units else 0.0
        if abs(float(row.fulfillment_rate) - expected_fulfillment) > 1e-6 or abs(float(row.stockout_rate) - expected_stockout) > 1e-6:
            inventory_errors += 1
    if inventory_errors:
        errors.append(f"inventory_snapshot: {inventory_errors} rows violate demand/stockout/fulfillment identities")

    sales_units: dict[tuple[date, int, int], int] = defaultdict(int)
    for period, region_id, product_id, units in session.execute(
        select(SalesFact.period, SalesFact.region_id, SalesFact.product_id, SalesFact.units)
    ):
        sales_units[(period, region_id, product_id)] += units
    unit_mismatch = sum(
        1 for row in inventory_rows
        if sales_units[(row.period, row.region_id, row.product_id)] != row.units_sold
    )
    if unit_mismatch:
        errors.append(f"inventory_snapshot: {unit_mismatch} rows do not match sales_fact units")
    checks.append("inventory demand, stockout, fulfillment and sold quantities are mutually consistent")
    checks.append("inventory units_sold reconciles with sales_fact units at region-product-month grain")

    sales_totals: dict[tuple[date, int], list[float]] = defaultdict(lambda: [0.0, 0.0])
    for period, region_id, amount, cogs in session.execute(
        select(SalesFact.period, SalesFact.region_id, SalesFact.sales_amount, SalesFact.cogs_amount)
    ):
        sales_totals[(period, region_id)][0] += float(amount)
        sales_totals[(period, region_id)][1] += float(cogs)
    marketing_totals: dict[tuple[date, int], float] = defaultdict(float)
    for period, region_id, spend in session.execute(
        select(MarketingFact.period, MarketingFact.region_id, MarketingFact.spend)
    ):
        marketing_totals[(period, region_id)] += float(spend)
    hr_labor: dict[date, float] = defaultdict(float)
    for period, labor in session.execute(select(HRMonthly.period, HRMonthly.labor_cost)):
        hr_labor[period] += float(labor)
    finance_labor: dict[date, float] = defaultdict(float)
    reconciliation_errors = 0
    for row in session.scalars(select(FinanceMonthly)).all():
        revenue, cogs = sales_totals[(row.period, row.region_id)]
        if not _close(float(row.revenue), revenue) or not _close(float(row.cogs_amount), cogs):
            reconciliation_errors += 1
        if not _close(float(row.marketing_spend), marketing_totals[(row.period, row.region_id)]):
            reconciliation_errors += 1
        finance_labor[row.period] += float(row.labor_cost)
    for period in expected_periods:
        if not _close(finance_labor[period], hr_labor[period]):
            reconciliation_errors += 1
    if reconciliation_errors:
        errors.append(f"finance_monthly: {reconciliation_errors} rows do not reconcile with sales, marketing or HR facts")
    checks.append("finance revenue/cogs/marketing/labor reconcile with source facts")

    sales_total = session.scalar(select(text("COALESCE(SUM(sales_amount), 0)")).select_from(SalesFact)) or 0
    cogs_total = session.scalar(select(text("COALESCE(SUM(cogs_amount), 0)")).select_from(SalesFact)) or 0
    marketing_spend = session.scalar(select(text("COALESCE(SUM(spend), 0)")).select_from(MarketingFact)) or 0
    campaign_revenue = session.scalar(select(text("COALESCE(SUM(attributed_revenue), 0)")).select_from(MarketingFact)) or 0
    if sales_total <= 0 or cogs_total <= 0:
        errors.append("gross_profit: sales or cogs is not positive")
    if marketing_spend <= 0 or campaign_revenue <= 0:
        errors.append("ROI: marketing spend or campaign revenue is not positive")
    if sales_total <= cogs_total:
        errors.append("gross_margin: generated costs exceed sales")
    checks.append("gross profit, gross margin, ROI, stockout rate and budget variance are computable")

    event_assertions = _event_assertions(session)
    for item in event_assertions:
        if not item.passed:
            errors.append(f"event assertion failed: {item.name}")

    demand_total = sum(row.demand_units for row in inventory_rows)
    sold_total = sum(row.units_sold for row in inventory_rows)
    stockout_total = sum(row.stockout_units for row in inventory_rows)
    return DataQualityReport(
        passed=not errors and all(item.passed for item in event_assertions),
        checks=checks,
        errors=errors,
        row_counts=row_counts,
        period_start=min(expected_periods),
        period_end=max(expected_periods),
        sample_metric_values={
            "sales_amount": _as_float(sales_total),
            "gross_profit": _as_float(sales_total - cogs_total),
            "ROI": _as_float(campaign_revenue / marketing_spend),
            "fulfillment_rate": sold_total / demand_total if demand_total else 0.0,
            "stockout_rate": stockout_total / demand_total if demand_total else 0.0,
        },
        event_assertions=event_assertions,
    )
