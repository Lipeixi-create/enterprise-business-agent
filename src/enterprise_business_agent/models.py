from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

MONEY = Numeric(18, 2)
RATE = Numeric(10, 6)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class Region(TimestampMixin, Base):
    __tablename__ = "regions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)


class Channel(TimestampMixin, Base):
    __tablename__ = "channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)


class Supplier(TimestampMixin, Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    reliability_score: Mapped[Decimal] = mapped_column(RATE, nullable=False)


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    base_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    base_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    supplier: Mapped[Supplier] = relationship()


class Campaign(TimestampMixin, Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    campaign_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_roi: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    active_from: Mapped[date] = mapped_column(Date, nullable=False)
    active_to: Mapped[date] = mapped_column(Date, nullable=False)


class Department(TimestampMixin, Base):
    __tablename__ = "departments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    domain: Mapped[str] = mapped_column(String(50), nullable=False)


class SalesFact(TimestampMixin, Base):
    __tablename__ = "sales_fact"
    __table_args__ = (UniqueConstraint("period", "region_id", "channel_id", "product_id", name="uq_sales_grain"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    units: Mapped[int] = mapped_column(Integer, nullable=False)
    sales_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cogs_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class MarketingFact(TimestampMixin, Base):
    __tablename__ = "marketing_fact"
    __table_args__ = (UniqueConstraint("period", "campaign_id", "region_id", "channel_id", name="uq_marketing_grain"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), nullable=False)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    spend: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    conversions: Mapped[int] = mapped_column(Integer, nullable=False)
    attributed_revenue: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class FinanceMonthly(TimestampMixin, Base):
    __tablename__ = "finance_monthly"
    __table_args__ = (UniqueConstraint("period", "region_id", name="uq_finance_grain"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), nullable=False)
    revenue: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cogs_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    operating_expense: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    marketing_spend: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    labor_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    budget_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    actual_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class PurchaseFact(TimestampMixin, Base):
    __tablename__ = "purchase_fact"
    __table_args__ = (UniqueConstraint("period", "supplier_id", "product_id", "region_id", name="uq_purchase_grain"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), nullable=False)
    purchase_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    purchase_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)


class InventorySnapshot(TimestampMixin, Base):
    __tablename__ = "inventory_snapshot"
    __table_args__ = (UniqueConstraint("period", "region_id", "product_id", name="uq_inventory_grain"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    region_id: Mapped[int] = mapped_column(ForeignKey("regions.id"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    inventory_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    demand_units: Mapped[int] = mapped_column(Integer, nullable=False)
    units_sold: Mapped[int] = mapped_column(Integer, nullable=False)
    inventory_days: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    stockout_units: Mapped[int] = mapped_column(Integer, nullable=False)
    stockout_rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    fulfillment_rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)


class HRMonthly(TimestampMixin, Base):
    __tablename__ = "hr_monthly"
    __table_args__ = (UniqueConstraint("period", "department_id", name="uq_hr_grain"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    headcount: Mapped[int] = mapped_column(Integer, nullable=False)
    hires: Mapped[int] = mapped_column(Integer, nullable=False)
    turnover_count: Mapped[int] = mapped_column(Integer, nullable=False)
    turnover_rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    labor_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

