-- PostgreSQL DDL for Enterprise Business Agent Phase 1.
-- Amounts are CNY; ratios are stored as decimals (0.15 = 15%).

CREATE TABLE regions (
  id SERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL UNIQUE,
  name VARCHAR(50) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE channels (
  id SERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL UNIQUE,
  name VARCHAR(50) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE suppliers (
  id SERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  reliability_score NUMERIC(10,6) NOT NULL CHECK (reliability_score BETWEEN 0 AND 1),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  category VARCHAR(50) NOT NULL,
  base_price NUMERIC(18,2) NOT NULL CHECK (base_price >= 0),
  base_cost NUMERIC(18,2) NOT NULL CHECK (base_cost >= 0),
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE campaigns (
  id SERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  campaign_type VARCHAR(50) NOT NULL,
  target_roi NUMERIC(10,6) NOT NULL CHECK (target_roi >= 0),
  active_from DATE NOT NULL,
  active_to DATE NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK (active_to >= active_from)
);
CREATE TABLE departments (
  id SERIAL PRIMARY KEY,
  code VARCHAR(20) NOT NULL UNIQUE,
  name VARCHAR(50) NOT NULL,
  domain VARCHAR(50) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sales_fact (
  id SERIAL PRIMARY KEY,
  period DATE NOT NULL,
  region_id INTEGER NOT NULL REFERENCES regions(id),
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  campaign_id INTEGER REFERENCES campaigns(id),
  units INTEGER NOT NULL CHECK (units >= 0),
  sales_amount NUMERIC(18,2) NOT NULL CHECK (sales_amount >= 0),
  discount_amount NUMERIC(18,2) NOT NULL CHECK (discount_amount >= 0),
  cogs_amount NUMERIC(18,2) NOT NULL CHECK (cogs_amount >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (period, region_id, channel_id, product_id)
);
CREATE INDEX ix_sales_fact_period ON sales_fact(period);

CREATE TABLE marketing_fact (
  id SERIAL PRIMARY KEY,
  period DATE NOT NULL,
  campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
  region_id INTEGER NOT NULL REFERENCES regions(id),
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  spend NUMERIC(18,2) NOT NULL CHECK (spend >= 0),
  impressions INTEGER NOT NULL CHECK (impressions >= 0),
  clicks INTEGER NOT NULL CHECK (clicks >= 0),
  conversions INTEGER NOT NULL CHECK (conversions >= 0),
  attributed_revenue NUMERIC(18,2) NOT NULL CHECK (attributed_revenue >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (period, campaign_id, region_id, channel_id)
);
CREATE INDEX ix_marketing_fact_period ON marketing_fact(period);

CREATE TABLE finance_monthly (
  id SERIAL PRIMARY KEY,
  period DATE NOT NULL,
  region_id INTEGER NOT NULL REFERENCES regions(id),
  revenue NUMERIC(18,2) NOT NULL CHECK (revenue >= 0),
  cogs_amount NUMERIC(18,2) NOT NULL CHECK (cogs_amount >= 0),
  operating_expense NUMERIC(18,2) NOT NULL CHECK (operating_expense >= 0),
  marketing_spend NUMERIC(18,2) NOT NULL CHECK (marketing_spend >= 0),
  labor_cost NUMERIC(18,2) NOT NULL CHECK (labor_cost >= 0),
  budget_amount NUMERIC(18,2) NOT NULL CHECK (budget_amount >= 0),
  actual_amount NUMERIC(18,2) NOT NULL CHECK (actual_amount >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (period, region_id)
);
CREATE INDEX ix_finance_monthly_period ON finance_monthly(period);

CREATE TABLE purchase_fact (
  id SERIAL PRIMARY KEY,
  period DATE NOT NULL,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  region_id INTEGER NOT NULL REFERENCES regions(id),
  purchase_qty INTEGER NOT NULL CHECK (purchase_qty >= 0),
  unit_cost NUMERIC(18,2) NOT NULL CHECK (unit_cost >= 0),
  purchase_amount NUMERIC(18,2) NOT NULL CHECK (purchase_amount >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (period, supplier_id, product_id, region_id)
);
CREATE INDEX ix_purchase_fact_period ON purchase_fact(period);

CREATE TABLE inventory_snapshot (
  id SERIAL PRIMARY KEY,
  period DATE NOT NULL,
  region_id INTEGER NOT NULL REFERENCES regions(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  inventory_qty INTEGER NOT NULL CHECK (inventory_qty >= 0),
  demand_units INTEGER NOT NULL CHECK (demand_units >= 0),
  units_sold INTEGER NOT NULL CHECK (units_sold >= 0),
  inventory_days NUMERIC(10,6) NOT NULL CHECK (inventory_days >= 0),
  stockout_units INTEGER NOT NULL CHECK (stockout_units >= 0),
  stockout_rate NUMERIC(10,6) NOT NULL CHECK (stockout_rate BETWEEN 0 AND 1),
  fulfillment_rate NUMERIC(10,6) NOT NULL CHECK (fulfillment_rate BETWEEN 0 AND 1),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (period, region_id, product_id)
);
CREATE INDEX ix_inventory_snapshot_period ON inventory_snapshot(period);

CREATE TABLE hr_monthly (
  id SERIAL PRIMARY KEY,
  period DATE NOT NULL,
  department_id INTEGER NOT NULL REFERENCES departments(id),
  headcount INTEGER NOT NULL CHECK (headcount >= 0),
  hires INTEGER NOT NULL CHECK (hires >= 0),
  turnover_count INTEGER NOT NULL CHECK (turnover_count >= 0),
  turnover_rate NUMERIC(10,6) NOT NULL CHECK (turnover_rate BETWEEN 0 AND 1),
  labor_cost NUMERIC(18,2) NOT NULL CHECK (labor_cost >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (period, department_id)
);
CREATE INDEX ix_hr_monthly_period ON hr_monthly(period);
