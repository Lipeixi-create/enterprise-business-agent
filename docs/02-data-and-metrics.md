# 02 — 数据模型、模拟数据与指标语义层

> 本文档以当前 [models.py](../src/enterprise_business_agent/models.py) 和 [metrics.json](../semantic_layer/metrics.json) 为基线，说明已实现的数据结构、模拟事件的真实逻辑、与目标需求的差距，以及指标语义层 v2 的设计。

---

## 1. 当前数据基础

### 1.1 覆盖范围

- 时间：`2024-01-01` 至 `2025-12-01`，24 个业务月；2024 年为基线，2025 年植入经营事件；
- 区域：华东、华北、华南、西部；
- 渠道：直营、电商、经销商、零售；
- 商品：8 个；
- 营销活动：13 个（2024 / 2025 各一套，含常青投放）；
- 部门：5 个；
- 供应商：4 个；
- 事实表：销售、营销、财务月度、区域—商品—月份库存快照、采购、人力月度。

### 1.2 生成方式

```bash
python scripts/init_db.py --database-url sqlite:///./enterprise_business_agent.db
python scripts/generate_data.py --database-url sqlite:///./enterprise_business_agent.db --reset
python scripts/quality_check.py --database-url sqlite:///./enterprise_business_agent.db
```

生成器使用固定随机种子 `20251027`，数据可重复。

---

## 2. 数据模型

### 2.1 表关系

```text
regions ─┬─ sales_fact ── products ── suppliers
         ├─ marketing_fact ── campaigns
         ├─ finance_monthly
         ├─ inventory_snapshot
         └─ purchase_fact

channels ─┘
departments ── hr_monthly
```

### 2.2 维度表

| 表 | 粒度 | 主键 | 关键字段 | 说明 |
| --- | --- | --- | --- | --- |
| `regions` | 区域 | `id` | `code`, `name` | 4 个区域 |
| `channels` | 渠道 | `id` | `code`, `name` | 4 个渠道 |
| `suppliers` | 供应商 | `id` | `code`, `name`, `reliability_score` | 可靠性影响采购成本 |
| `products` | 商品 | `id` | `code`, `name`, `category`, `base_price`, `base_cost`, `supplier_id` | 8 个商品 |
| `campaigns` | 活动 | `id` | `code`, `name`, `campaign_type`, `target_roi`, `active_from`, `active_to` | 13 个活动 |
| `departments` | 部门 | `id` | `code`, `name`, `domain` | 5 个部门 |

### 2.3 事实表

| 表 | 粒度（唯一键） | 关键度量 | 当前行数（约） |
| --- | --- | --- | --- |
| `sales_fact` | `period × region × channel × product` | `units`, `sales_amount`, `discount_amount`, `cogs_amount` | 24 × 4 × 4 × 8 = 3072 |
| `marketing_fact` | `period × campaign × region × channel` | `spend`, `impressions`, `clicks`, `conversions`, `attributed_revenue` | 376（活动期 × 电商/零售渠道，常青活动保证每月有数据） |
| `finance_monthly` | `period × region` | `revenue`, `cogs_amount`, `operating_expense`, `marketing_spend`, `labor_cost`, `budget_amount`, `actual_amount` | 24 × 4 = 96 |
| `purchase_fact` | `period × supplier × product × region` | `purchase_qty`, `unit_cost`, `purchase_amount` | 24 × 4 × 8 = 768 |
| `inventory_snapshot` | `period × region × product` | `inventory_qty`, `demand_units`, `units_sold`, `inventory_days`, `stockout_units`, `stockout_rate`, `fulfillment_rate` | 24 × 4 × 8 = 768 |
| `hr_monthly` | `period × department` | `headcount`, `hires`, `turnover_count`, `turnover_rate`, `labor_cost` | 24 × 5 = 120 |

### 2.4 与需求文档表名的映射

需求文档中的逻辑表名与当前物理表名不同。**以当前物理表为准**，避免重命名破坏现有代码：

| 需求文档表名 | 当前物理表 | 是否完整 | 说明 |
| --- | --- | --- | --- |
| `sales` | `sales_fact` | 🟡 | 月度 × 区域 × 渠道 × 商品聚合，不是订单行 |
| `products` | `products` | ✅ | — |
| `regions` | `regions` | ✅ | — |
| `channels` | `channels` | ✅ | — |
| `marketing_campaigns` | `campaigns` | ✅ | — |
| `marketing_performance` | `marketing_fact` | ✅ | — |
| `finance_transactions` | `finance_monthly` | 🟡 | 当前是月度聚合，不是交易级 |
| `finance_budget` | `finance_monthly.budget_amount` / `actual_amount` | 🟡 | 预算与实际合并在月度表 |
| `inventory` | `inventory_snapshot` | ✅ | 月度快照 |
| `suppliers` | `suppliers` | ✅ | — |
| `procurement` | `purchase_fact` | ✅ | — |
| `employees` | ❌ 未实现 | ⬜ | 当前只有部门月度聚合，没有员工明细 |
| `departments` | `departments` | ✅ | — |
| `hr_changes` | ❌ 未实现 | ⬜ | 当前只有 `hires` / `turnover_count` 月度计数 |

**设计决定：** Phase 1 采用月度聚合事实表，而不是交易级明细。理由是数据量小、生成可解释、指标计算稳定、便于跨部门关联。员工明细、招聘/离职事件、交易级财务留到有明确需求时再加，避免为了“看起来企业级”而增加无用复杂度。

---

## 3. 模拟数据的真实业务逻辑

> 这一节非常关键：**文档必须描述代码实际生成了什么，而不是我们希望的因果链。**

### 3.1 已编码的机制

| 机制 | 代码位置 | 效果 |
| --- | --- | --- |
| 区域因子 | `region_factor` | 华东 1.22、华北 1.00、华南 0.96、西部 0.82 |
| 渠道因子 | `channel_factor` | 电商 1.28、直营 0.82、经销商 0.93、零售 0.76 |
| 季节性 | `seasonality[period.month - 1]` | 6 月、11 月高峰；12 月回落；2024 / 2025 使用同一自然月系数 |
| 增长趋势 | `trend = 1 + 0.012 * calendar_index` | 以 2024-01 为 0，2024 是基线，2025 正常增长 |
| 华东供应收缩 | `EAST_EVENT_SUPPLY_CAP` | 2025-07 至 2025-12，华东 P001/P002/P003 采购入库上限 0.95 / 0.78 / 0.60 / 0.40 / 0.30 / 0.28 |
| 库存流转 | `beginning + receipts - units_sold` | 期初库存 + 采购入库 - 已售 = 期末库存 |
| 缺货约束 | `units_sold = min(demand, available)` | 销售量由可售量决定，不再使用外生的区域下滑系数 |
| 库存目标 | `TARGET_INVENTORY_DAYS` | P008 9 天、P006 14 天、其他 30 天 |
| 长期缺货风险 | `BASE_SUPPLY_CAP` | P008 采购上限 0.86、P006 0.91，形成持续缺货风险 |
| 成本上涨 | `1 + max(0, calendar_index - 17) * 0.027` | 2025-06 起每月 +2.7% |
| 高风险商品加价 | `P006` / `P008` 额外 +2.5% | 成本压力更大 |
| 供应商可靠性 | `(1 - reliability_score) * 0.04` | 可靠性越低，采购成本越高 |
| 营销低效活动 | `C006` 的 ROI 0.72 → 0.58 → 0.45，点击率 2.4% → 2.0%，转化率 7.5% → 5.0% | Q4 ROI 持续恶化 |
| 营销大促 | `C003` / `C005` 高投入 | 6 月、11 月费用高 |
| 2024 / 2025 活动 | 13 个活动，`C106` / `C007` 常青投放覆盖全年 | 每月都有营销事实，便于 MoM / YoY |
| 市场部扩张 | `MARKETING` 在 2025 年 10–12 月 headcount +12% 起、每月 +3% | 人力成本增长最快 |
| 人力成本上涨 | 全部门每月 +1.2%；市场部 Q4 额外 ×1.10 | Q4 成本压力 |

### 3.2 五个目标事件与当前实现程度

| 事件 | 目标因果链 | 当前实现 | 结论 |
| --- | --- | --- | --- |
| 事件 1：华东销售下降 | 华东核心商品库存下降 → 缺货率上升 → 销量下降 → 销售额下降 | 采购入库上限逐月下降 → 期末库存下降 → 缺货率上升 → 销售量受可售量约束 | ✅ **因果链已编码** |
| 事件 2：利润下降 | 收入下降 + 营销成本上升 + 人力成本上升 → 毛利/净利下降 | 收入受华东供应收缩影响；成本上涨；2025 Q4 营销与人力上升 | ✅ 可观察；`net_profit` 指标仍待补 |
| 事件 3：营销 ROI 下降 | 某活动成本增加、转化率下降 → ROI 下降 | C006 在 2025 Q4 投入上升、转化率下降，ROI 从约 0.72 降至 0.45 | ✅ 已实现动态恶化 |
| 事件 4：库存风险 | 部分商品库存高、销售慢 → 库存天数高 | P006/P008 库存天数低、缺货率高（风险方向是“缺货”，不是“积压”） | 🟡 缺货风险已实现；积压风险未实现 |
| 事件 5：部门成本增长 | 某部门 headcount 增加 → labor cost 增加 | 市场部 2025 Q4 headcount 与成本上升 | ✅ 已实现 |

### 3.3 华东因果链的实现

生成器不再对华东销量直接乘以下滑系数，而是按区域—商品—月份维护库存状态：

```python
beginning = inventory_state[(region_code, product_code)]
target_inventory = demand * target_days / 30
receipts = min(desired_receipts, demand * supply_cap(region_code, product_code, period))
available = beginning + receipts
units_sold = min(demand, available)
stockout_units = demand - units_sold
ending_inventory = available - units_sold
```

对应关系：

- `EAST_EVENT_SUPPLY_CAP` 让华东核心商品在 2025-07 至 2025-12 的采购入库逐月减少；
- 期初库存先吸收冲击，因此库存天数先下降；
- 库存耗尽后出现缺货，`stockout_rate` 上升，`fulfillment_rate` 下降；
- 销售量等于可售量，所以销量和销售额随缺货下降；
- `inventory_snapshot` 新增 `demand_units` 和 `fulfillment_rate`，可直接查询“需求、已售、缺货、满足率”；
- 其他区域同款商品使用正常采购上限，形成区域对照。

自动化断言（`quality.py` 与 `tests/test_event_chain.py`）覆盖：

- 华东核心商品 2025Q4 销量/销售额相对 2024Q4 显著下降；
- 2025Q4 库存天数显著低于 2024Q4；
- 2025Q4 缺货率显著上升、满足率显著下降；
- 华东缺货率显著高于其他区域，其他区域销售额没有同幅度异常；
- 库存下降月份 ≤ 缺货率上升月份 ≤ 销量下降月份；
- 库存、销售、采购可按区域—商品—月份追溯，数量关系一致。

### 3.4 同比、环比与季度环比

数据已扩展到 2024-01 至 2025-12 连续 24 个月：

- 2024 年是正常基线；
- 2025 年植入供应收缩、成本上涨、营销 ROI 下降和部门扩张；
- 因此 YoY、MoM、QoQ 都有可比基准。

比较实现位于 `analysis/comparison.py`：

- `compare_mom` 只取上一个自然月；
- `compare_yoy` 只取去年同一月份，不回退到相邻月份；
- `compare_qoq` / `compare_yoy_quarter` 按完整自然季度聚合，支持 `sum` 和 `last`；
- 基准期间缺失、季度不完整或分母为 0 时返回 `insufficient_data` / `invalid_denominator`，`change_rate` 为 `None`。

如果用户只生成 12 个月（`DATA_MONTHS=12`），YoY 会因为没有 2024 年基线而显式返回“数据不足”，不会编造增长率。

### 3.5 库存风险的语义

当前 P006/P008 是“库存天数低 + 缺货率高”，属于**缺货风险**。两类风险不能混为一个 `inventory_risk` 指标。建议定义：

- `stockout_risk`：库存天数低于阈值或缺货率高；
- `overstock_risk`：库存天数高于阈值且周转率低；
- `inventory_risk_level`：由规则组合，输出 `stockout / overstock / healthy`，并附证据。

当前版本只实现了 `stockout_risk` 的数据基础（低库存、高缺货），`overstock_risk` 尚未编码。

---

## 4. 指标语义层 v2（当前实现）

### 4.1 注册表结构

`semantic_layer/metrics.json` 为 `2.0.0`，包含 34 个 canonical 指标。每个指标都包含：

```json
{
  "name": "gross_margin",
  "label": "毛利率",
  "synonyms": ["毛利率", "销售毛利率", "gross margin"],
  "domain": "Finance",
  "description": "毛利除以销售额，按分子分母分别汇总后计算",
  "metric_type": "ratio",
  "additivity": "non_additive",
  "source_table": "sales_fact",
  "source_fields": ["sales_amount", "cogs_amount"],
  "expression": "SUM(sales_amount - cogs_amount) / NULLIF(SUM(sales_amount), 0)",
  "numerator": "SUM(sales_amount - cogs_amount)",
  "denominator": "SUM(sales_amount)",
  "aggregation": "ratio",
  "allowed_aggregations": ["sum", "ratio"],
  "dimensions": ["region", "channel", "product"],
  "time_grains": ["month", "quarter", "year", "total"],
  "unit": "ratio",
  "nullable_when": "sales_amount = 0",
  "zero_denominator": "not_calculable"
}
```

### 4.2 指标目录

| 域 | 指标 |
| --- | --- |
| Sales | `sales_amount`、`sales_volume`、`gross_profit`、`gross_margin`、`sales_unit_price` |
| Marketing | `marketing_spend`、`campaign_revenue`、`ROI`、`CAC`、`conversion_rate`、`impressions`、`clicks`、`conversions`、`CTR` |
| Finance | `revenue`、`net_profit`、`net_margin`、`budget`、`actual`、`budget_vs_actual`、`budget_variance_rate` |
| Supply Chain | `inventory`、`demand_units`、`stockout_units`、`inventory_days`、`inventory_turnover`、`stockout_rate`、`fulfillment_rate`、`purchase_amount`、`unit_purchase_cost` |
| HR | `headcount`、`turnover_rate`、`labor_cost`、`labor_cost_per_head` |

### 4.3 维度注册表

`semantic_layer/dimensions.json` 定义 7 个维度：

| 维度 | 关联表 | 事实表外键 | 编码列 | 名称列 |
| --- | --- | --- | --- | --- |
| `period` | 时间 | `period` | — | — |
| `region` | `regions` | `region_id` | `code` | `name` |
| `channel` | `channels` | `channel_id` | `code` | `name` |
| `product` | `products` | `product_id` | `code` | `name` |
| `campaign` | `campaigns` | `campaign_id` | `code` | `name` |
| `department` | `departments` | `department_id` | `code` | `name` |
| `supplier` | `suppliers` | `supplier_id` | `code` | `name` |

维度解析支持中文名称和同义词。编译器只会为指标声明的维度建立关联。

### 4.4 配置校验

`python scripts/validate_semantic.py` 对照真实数据库检查：

- 指标名不重复；
- 来源表、来源字段、维度关联列存在；
- 表达式只使用允许的函数和标识符；
- 比率指标同时声明分子和分母；
- `allowed_aggregations`、`time_grains`、`unit` 合法；
- 维度事实列和名称列可解析。

当前 SQLite 校验结果为 `{"valid": true, "errors": []}`。

### 4.5 与 v1 的差异

1. 所有指标增加 `synonyms`，支持中文、英文和常见简称；
2. 增加 `metric_type` / `additivity`，明确加性、半加性和非加性；
3. 比率指标使用结构化的 `numerator` / `denominator`，不再依赖单条 SQL 字符串；
4. 增加 `allowed_aggregations`、`time_grains`、`nullable_when`、`zero_denominator`；
5. 增加独立维度注册表，维度引用使用稳定名称而非物理列名；
6. 补齐 CAC、conversion_rate、inventory_turnover、budget、actual、net_profit 等指标；
7. 通过 schema 校验消除“指标引用了不存在的表/列”的风险。

---

## 5. 指标语义层 v2（实现细节）

### 5.1 目标

- 一个指标只有一个 canonical 定义；
- 同义词、别名可解析；
- 明确可加性，禁止错误聚合；
- 公式结构化，能安全编译；
- 明确来源表、列、默认过滤、时间粒度；
- 可版本化、可测试、可审计。

### 5.2 MetricDefinition v2

```json
{
  "name": "gross_margin",
  "display_name": "毛利率",
  "domain": "Finance",
  "description": "毛利除以营收，衡量主营业务盈利能力",
  "synonyms": ["毛利率", "利润率", "gross margin", "GM", "gross margin ratio"],
  "metric_type": "ratio",
  "additivity": "non_additive",
  "formula": {
    "type": "ratio",
    "numerator": {"metric": "gross_profit"},
    "denominator": {"metric": "revenue"}
  },
  "source": {
    "table": "finance_monthly",
    "columns": ["revenue", "cogs_amount"],
    "time_column": "period",
    "default_filters": []
  },
  "unit": "ratio",
  "dimensions": ["period", "region_id"],
  "allowed_aggregations": ["sum", "avg"],
  "time_grains": ["month", "quarter", "year"],
  "nullable_when": "revenue = 0",
  "owner": "finance",
  "version": "2.0.0"
}
```

字段说明：

| 字段 | 作用 |
| --- | --- |
| `name` | canonical ID，代码/工具使用 |
| `display_name` | 展示名 |
| `synonyms` | 中文/英文/缩写，供 Resolver 匹配 |
| `metric_type` | `additive` / `semi_additive` / `ratio` / `derived` |
| `additivity` | 聚合规则：`additive` / `semi_additive` / `non_additive` |
| `formula` | 结构化公式；比裸 SQL 更安全、可组合 |
| `source` | 物理表、列、时间列、默认过滤 |
| `dimensions` | 允许的分组维度 |
| `allowed_aggregations` | 允许的聚合方式 |
| `time_grains` | 支持的时间粒度 |
| `nullable_when` | 分母为 0 等空值条件 |
| `owner` / `version` | 口径责任人与版本 |

### 5.3 可加性规则

| 类型 | 含义 | 示例 | 聚合方式 |
| --- | --- | --- | --- |
| `additive` | 可跨所有维度和时间相加 | `sales_amount`, `spend`, `labor_cost` | `SUM` |
| `semi_additive` | 可跨部分维度相加，但不能跨时间相加 | `headcount`, `inventory_qty` | 跨维度 `SUM`；跨时间取期末值 |
| `ratio` | 分子/分母分别聚合后再相除 | `gross_margin`, `ROI`, `stockout_rate` | 不能平均比率 |
| `derived` | 由其他指标计算 | `net_profit`, `budget_variance` | 按公式计算 |

**硬规则：**

- 不允许对 `ratio` 指标做 `AVG` 或直接求和；
- `stockout_rate` 必须按需求量加权，不是简单平均；
- `inventory_days` 必须按销量加权或重算，不是简单平均；
- `gross_margin` 必须按营收加权，不能对各区域毛利率取平均；
- `headcount`、`inventory_qty` 跨月不能求和，只能取期末快照。

### 5.4 口径仲裁：一个指标只有一个权威来源

当前数据中，`finance_monthly.revenue` 由 `sales_fact.sales_amount` 聚合而来，理论上对得上；但两个表都可以算“销售额/营收”。必须明确规定：

| 分析场景 | 权威来源 | 原因 |
| --- | --- | --- |
| 净利润、净利率、预算偏差 | `finance_monthly` | 财务口径，含成本与费用 |
| 商品/渠道/区域销售与毛利 | `sales_fact` | 有商品、渠道维度 |
| 营销效率 | `marketing_fact` | 活动、渠道维度 |
| 库存/缺货 | `inventory_snapshot` | 商品/区域快照 |
| 采购成本 | `purchase_fact` | 供应商/商品维度 |
| 人力成本 | `hr_monthly` | 部门维度 |

具体规则（当前实现）：

- `revenue`（P&L）来自 `finance_monthly`；
- `sales_amount`（经营）来自 `sales_fact`；
- `gross_profit` / `gross_margin` 来自 `sales_fact`，保留商品、渠道、区域维度；
- `net_profit` / `net_margin` 来自 `finance_monthly`；
- 两者对账：按 `period × region` 比较，容差 0.5% 或 1 元；不一致时质量检查报警，不静默选择。

### 5.5 指标目录（已实现 34 个）

> 以下为 `metrics.json` 2.0.0 的实际目录。字段级定义（含 synonyms、分子/分母、允许聚合、维度、时间粒度）以 JSON 为准。

#### Sales

| 指标 | 公式 | 来源 | 类型 |
| --- | --- | --- | --- |
| `sales_amount` | `SUM(sales_amount)` | `sales_fact` | additive |
| `sales_volume` | `SUM(units)` | `sales_fact` | additive |
| `gross_profit` | `SUM(sales_amount - cogs_amount)` | `sales_fact` | additive |
| `gross_margin` | `gross_profit / sales_amount` | `sales_fact` | ratio |
| `sales_unit_price` | `sales_amount / units` | `sales_fact` | ratio |

#### Marketing

| 指标 | 公式 | 来源 | 类型 |
| --- | --- | --- | --- |
| `marketing_spend` | `SUM(spend)` | `marketing_fact` | additive |
| `campaign_revenue` | `SUM(attributed_revenue)` | `marketing_fact` | additive |
| `ROI` | `campaign_revenue / marketing_spend` | `marketing_fact` | ratio |
| `CAC` | `marketing_spend / conversions` | `marketing_fact` | ratio |
| `conversion_rate` | `conversions / clicks` | `marketing_fact` | ratio |
| `impressions` | `SUM(impressions)` | `marketing_fact` | additive |
| `clicks` | `SUM(clicks)` | `marketing_fact` | additive |
| `conversions` | `SUM(conversions)` | `marketing_fact` | additive |
| `CTR` | `clicks / impressions` | `marketing_fact` | ratio |

**CAC 口径说明：** 当前数据没有独立的 `new_customers` 字段，`CAC` 暂以 `conversions` 作为获客数的近似分母。结果会标注来源字段；如果用于严格的新客获取成本分析，需要后续新增字段。

#### Finance

| 指标 | 公式 | 来源 | 类型 |
| --- | --- | --- | --- |
| `revenue` | `SUM(revenue)` | `finance_monthly` | additive |
| `net_profit` | `SUM(revenue - actual_amount)` | `finance_monthly` | additive |
| `net_margin` | `net_profit / revenue` | `finance_monthly` | ratio |
| `budget` | `SUM(budget_amount)` | `finance_monthly` | additive |
| `actual` | `SUM(actual_amount)` | `finance_monthly` | additive |
| `budget_vs_actual` | `SUM(actual_amount - budget_amount)` | `finance_monthly` | additive |
| `budget_variance_rate` | `(actual - budget) / budget` | `finance_monthly` | ratio |

#### Supply Chain

| 指标 | 公式 | 来源 | 类型 |
| --- | --- | --- | --- |
| `inventory` | `SUM(inventory_qty)`，跨时间取期末值 | `inventory_snapshot` | semi_additive |
| `demand_units` | `SUM(demand_units)` | `inventory_snapshot` | additive |
| `stockout_units` | `SUM(stockout_units)` | `inventory_snapshot` | additive |
| `inventory_days` | `SUM(inventory_days * units_sold) / SUM(units_sold)` | `inventory_snapshot` | ratio |
| `inventory_turnover` | `SUM(units_sold) / AVG(inventory_qty)` | `inventory_snapshot` | ratio |
| `stockout_rate` | `SUM(stockout_units) / SUM(demand_units)` | `inventory_snapshot` | ratio |
| `fulfillment_rate` | `SUM(units_sold) / SUM(demand_units)` | `inventory_snapshot` | ratio |
| `purchase_amount` | `SUM(purchase_amount)` | `purchase_fact` | additive |
| `unit_purchase_cost` | `purchase_amount / purchase_qty` | `purchase_fact` | ratio |

#### HR

| 指标 | 公式 | 来源 | 类型 |
| --- | --- | --- | --- |
| `headcount` | `SUM(headcount)`（期末） | `hr_monthly` | semi_additive |
| `turnover_rate` | `turnover_count / headcount` | `hr_monthly` | ratio |
| `labor_cost` | `SUM(labor_cost)` | `hr_monthly` | additive |
| `labor_cost_per_head` | `labor_cost / headcount` | `hr_monthly` | ratio |

### 5.6 比率指标的聚合规则

这是最容易出错的地方。举例：

```text
错误：华东毛利率 20%，华北毛利率 30%，全国毛利率 = (20% + 30%) / 2 = 25%
正确：全国毛利率 = (华东毛利 + 华北毛利) / (华东营收 + 华北营收)
```

所有 ratio 指标必须遵守：

1. 先按维度聚合分子和分母；
2. 再做除法；
3. 需要“平均”时，使用加权平均，权重为分母；
4. 分母为 0 返回 `NULL`，并在结果中标注 `nullable`。

### 5.7 维度与关系定义

Phase 1.5 增加：

- `semantic_layer/dimensions.json`：维度名、来源表、列、类型、层级（region → city）、显示名、同义词；
- `semantic_layer/relationships.json`：表关系、join key、基数（1:N）、是否允许跨域 join。

示例：

```json
{
  "dimensions": [
    {"name": "region_id", "display_name": "区域", "table": "regions", "key": "id", "label_column": "name", "synonyms": ["区域", "大区", "region"]},
    {"name": "period", "display_name": "月份", "type": "time", "grains": ["month", "quarter", "year"]},
    {"name": "product_id", "display_name": "商品", "table": "products", "key": "id", "label_column": "name", "synonyms": ["商品", "产品", "SKU"]}
  ]
}
```

---

## 6. Metric Resolver 与查询编译

### 6.1 目标

用户说：

- “利润率” → `gross_margin` 还是 `net_margin`？
- “ROI” → `ROI`；
- “缺货率” → `stockout_rate`；
- “人员成本” → `labor_cost`；
- “销售额” → `sales_amount`；
- “营收” → `revenue`。

Resolver 必须给出 canonical metric + 置信度，低置信度时澄清，不能静默猜。

### 6.2 解析流程（已实现）

```text
输入名称
  ↓
NFKC 归一化 + 小写 + 去除首尾空白
  ↓
1. 精确匹配 canonical name
  ↓ 命中 → resolved
2. 精确匹配唯一 label
  ↓ 命中 → resolved
3. 匹配 synonyms 索引
  ↓ 唯一命中 → resolved
  ↓ 多个命中 → ambiguous + candidates
  ↓ 无命中 → unsupported
```

当前实现不做模糊匹配，也不调用 LLM；无法确定时要求调用方澄清。

### 6.3 歧义与未知处理（已实现）

| 输入 | 结果 |
| --- | --- |
| 利润 | `ambiguous`，候选 `gross_profit` / `net_profit` |
| 利润率 | `ambiguous`，候选 `gross_margin` / `net_margin` |
| 收入 / 营收 | `revenue` |
| 销售收入 | `sales_amount` |
| 未知指标 | `unsupported` |

### 6.4 输出契约（已实现）

```python
class MetricResolution(BaseModel):
    query: str
    status: str                 # resolved / ambiguous / unsupported
    metric_name: str | None
    label: str | None
    candidates: list[ResolutionCandidate]
    message: str | None
```

### 6.5 Query Compiler（已实现）

`query_compiler.py` 将结构化请求编译为参数化 SQL：

1. 只从 `metrics.json` 读取指标表达式、来源表和允许聚合；
2. 只从 `dimensions.json` 解析维度，并检查指标是否声明该维度；
3. 仅为请求的维度和过滤器建立 JOIN；
4. `SELECT period, <dimension code/name>, numerator, denominator ... GROUP BY period, dimensions`；
5. 时间与分类过滤值全部通过 `bindparam` 绑定；
6. 分类操作符仅允许 `eq`、`ne`、`in`；
7. 编译结果包含 SQL、params、方言和 `explain` 信息，可分别渲染为 SQLite / PostgreSQL 方言。

任何未知表、列、维度、操作符或不合法的指标配置都会抛出 `QueryCompileError`，不会降级为字符串拼接。

### 6.6 查询与分析流程（已实现）

```text
Pydantic 请求（metric / time / grain / dimensions / filters）
  ↓
MetricResolver 解析指标
  ↓
QueryCompiler 白名单编译 + 绑定参数
  ↓
SQL 按月 × 维度返回分子/分母
  ↓
AnalysisEngine 检查月份完整性
  ↓
按加性 / 半加性 / 比率规则聚合
  ↓
上卷到月 / 季 / 年 / 全期
  ↓
统一响应（status / values / unit / baseline / change / explain）
```

默认 SQL 只按月分组，由上卷层处理季度和年度。这样可以保证比率指标“先汇总分子分母再相除”，半加性指标“跨时间取最后一期”，并且 SQLite 与 PostgreSQL 行为一致。

---

## 7. 数据质量 v2

### 7.1 当前检查

`quality.py` 已覆盖：

- 主键重复；
- 必填字段为空；
- 数值字段非负；
- 六张事实表覆盖 24 个连续月份；
- 外键完整性；
- 库存 `demand_units = units_sold + stockout_units`、缺货率/满足率公式一致；
- 库存 `units_sold` 与销售事实按区域—商品—月份对账；
- 财务收入/成本/营销费用/人力成本与源事实对账；
- 毛利/毛利率/ROI 可计算；
- 13 条事件断言：华东销售/销量/库存/缺货/满足率下降、区域对照、时间顺序、P004/P005 库存积压、C006 ROI 下降、Q4 预算超支、净利润下降、市场部人力成本增长、可追溯性；
- 生成数据总体通过。

### 7.2 目标检查

| 类别 | 检查项 | 严重级别 | 说明 |
| --- | --- | --- | --- |
| 结构 | 主键唯一 | error | 所有表 |
| 结构 | 外键存在 | error | 所有 FK |
| 结构 | 唯一键唯一 | error | 事实表粒度 |
| 空值 | 必填字段非空 | error | 排除 `created_at` |
| 数值 | 金额/数量/人数非负 | error | — |
| 数值 | 比率在 `[0,1]` | error | 毛利率可为负，需允许负毛利 |
| 数值 | 折扣不超过原价 | error | `discount_amount <= units * base_price` |
| 时间 | 月份连续 | error | 已实现：24 个月无缺月 |
| 时间 | 无未来日期 | error | 相对数据截止月 |
| 时间 | 事实表粒度为月初 | error | `period.day == 1` |
| 对账 | `finance.revenue = SUM(sales.sales_amount)` | error | 已实现，按 period × region |
| 对账 | `finance.cogs = SUM(sales.cogs_amount)` | error | 已实现，同上 |
| 对账 | `finance.marketing_spend = SUM(marketing.spend)` | error | 已实现，按 period × region |
| 对账 | `finance.labor_cost` 合计 = `SUM(hr.labor_cost)` | error | 已实现，按 period |
| 对账 | `inventory.units_sold = SUM(sales.units)` | error | 已实现，按 region × product × period |
| 派生 | `gross_profit = revenue - cogs` | error | 容差 0.01 |
| 派生 | `gross_margin = gross_profit / revenue` | error | 分母为 0 时为空 |
| 派生 | `ROI = campaign_revenue / spend` | error | 分母为 0 时为空 |
| 派生 | `stockout_rate = stockout_units / demand_units` | error | 已实现，容差 1e-6 |
| 派生 | `fulfillment_rate = units_sold / demand_units` | error | 已实现，容差 1e-6 |
| 派生 | `inventory_days ≈ inventory_qty / (units_sold / 30)` | warning | 生成器直接写入，允许近似 |
| 派生 | `budget_variance = actual - budget` | error | — |
| 事件 | 华东 2025Q4 核心商品销售显著下降 | error | 已实现：相对 2024Q4 基线 |
| 事件 | 华东核心商品库存天数下降、缺货率上升、满足率下降 | error | 已实现 |
| 事件 | 华东异常显著强于其他区域 | error | 已实现：区域对照 |
| 事件 | 库存下降 ≤ 缺货上升 ≤ 销量下降 | error | 已实现：时间顺序 |
| 事件 | C006 ROI 最低且 Q4 恶化 | error | 已实现 |
| 事件 | P006/P008 缺货风险最高 | error | 已实现 |
| 事件 | 市场部 Q4 人力成本增长最快 | error | 已实现 |
| 分布 | 无数值列全常量 | warning | 防止生成器退化 |
| 分布 | 无极端异常值 | warning | 按业务阈值 |
| 新鲜度 | 最新月份符合预期 | warning | 演示环境 |

### 7.3 实现方式

- 脚本：`scripts/quality_check.py`（`validate_data.py` 作为后续别名）；
- 输出：JSON 报告 + 退出码；
- 报告字段：`passed`, `checks`, `errors`, `row_counts`, `period_start`, `period_end`, `sample_metric_values`, `event_assertions`；
- CI：`pytest` + `quality_check.py`；
- 数据快照：`data/quality_report.json`、`data/golden_metrics.json`（待 Phase 1.5 后续任务）。

### 7.4 对账为什么重要

如果 `finance_monthly.revenue` 与 `sales_fact.sales_amount` 对不上，Agent 在跨部门调查时会出现“财务说收入下降、销售说销售额没降”的冲突。对账检查能把这类问题在数据层暴露，而不是留给 Agent 解释。

---

## 8. 数据字典

Phase 1.5 增加 `scripts/export_data_dictionary.py`：

- 从 SQLAlchemy 模型读取表、列、类型、可空、注释；
- 从 `metrics.json` 读取指标；
- 生成 `docs/data-dictionary.md`；
- CI 检查生成文件是否与代码一致（防止文档漂移）。

数据字典至少包含：

- 表名、业务含义、粒度、主键、外键；
- 字段名、类型、单位、是否可空、业务说明；
- 指标名、公式、来源、维度、单位、可加性；
- 维度值域（区域、渠道、部门、商品分类）。

---

## 9. 验收标准

Phase 1.5 / Phase 2 完成时：

- [x] 数据覆盖 24 个月（2024-01 至 2025-12），YoY 可计算；缺基线返回 `insufficient_data`；
- [x] 华东库存 → 缺货 → 销量 → 销售额的因果链在数据中可检测；
- [x] 五个目标事件都有自动化断言，并补充区域对照、时间顺序、可追溯性断言；
- [x] 指标语义层 v2 包含 34 个指标，含同义词、可加性、结构化公式；
- [x] 所有 ratio 指标由查询编译器和分析引擎强制按分子/分母聚合；
- [x] `finance_monthly` 与 `sales_fact` / `marketing_fact` / `hr_monthly` 对账通过；
- [x] `quality_check.py` 输出 JSON 报告并通过测试；
- [ ] 数据字典自动生成且无漂移（当前由 `semantic_layer/*.json` 与手工文档维护）；
- [x] 现有测试全部通过，新增事件链、比较计算、SQL 编译和分析引擎测试；
- [x] 中文别名、歧义、未知指标有明确状态；
- [x] 五个 CLI 演示可直接运行。
