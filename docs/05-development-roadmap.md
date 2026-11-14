# 05 — 开发路线图

> 本文档把目标拆成可执行阶段。每个阶段都有明确目标、任务、交付物、验收标准和演示问题。**按阶段推进，不跨阶段堆功能。**

---

## 0. 使用方式

- 每个任务有 ID（如 `P2-03`），可直接转成 issue；
- 每个阶段结束必须通过“验收标准”和“演示问题”，否则不进入下一阶段；
- 任务完成后勾选，并同步更新 [docs/README.md](README.md) 的状态表；
- 代码变更必须伴随测试；契约变更必须伴随文档。

### 0.1 阶段总览

| 阶段 | 目标 | 预计工作量（1 人） | 依赖 |
| --- | --- | --- | --- |
| Phase 1.5 | 补齐数据与指标口径 | 1–1.5 周 | Phase 1 |
| Phase 2 | 语义查询与分析引擎 | 2–3 周 | Phase 1.5 |
| Phase 3 | Text-to-SQL + SQL Guard | 2 周 | Phase 2 |
| Phase 4 | Agent Runtime + Tool Registry | 3–4 周 | Phase 3 |
| Phase 5 | Evidence Chain + 报告 | 2–3 周 | Phase 4 | ✅ 已完成（2026-10-31） |
| Phase 6 | 企业级控制 | 2 周 | Phase 4 |
| Phase 7 | 评测与可观测性 | 2–3 周 | Phase 4/5 |
| Phase 8 | 可选扩展 | 按需 | Phase 7 |
| Phase 9.1 | 真实 LLM 稳定性验收 | 1 周 | Phase 7 | 🟡 核心完成（Final，2026-11-05；全量 69/90 as-run，离线后验 76/90；26 条定向复测已全部完成，剩余 8 条由 Phase 9.2.1 补跑；9 个契约偏差保留） |
| Phase 9.2 | 真实 PostgreSQL 验收 | 3–5 天 | Phase 6/9.1 | 🟡 核心完成（2026-11-04；30/30 真实 PG 专项通过） |
| Phase 9.2.1 | 模型调用可靠性与任务预算治理 | 3–5 天 | Phase 9.1/9.2 | 🟡 核心完成（2026-11-05；764 离线通过；8 条复测 4 PASS；24×3 seeds 多种子完成；Docker/OTLP/浏览器由 Phase 9.3 完成） |
| Phase 9.3 | Docker 构建部署、外部 OTLP 与浏览器 E2E | 3–5 天 | Phase 8.3/9.2 | 🟡 核心完成（2026-11-06；Compose + 真实 PG 部署通过；外部 Collector 257 spans；浏览器 E2E 主机与容器均 `ok=true`；canonical 最终镜像构建与容器链路稳定性未验证） |

> 工作量是粗略估计，取决于模型调用调试、评测集编写和前端/部署需求。**不要为了赶进度跳过 Phase 2 的确定性能力。**

### 0.2 依赖关系

```text
Phase 1.5
   ↓
Phase 2（语义查询 + 分析）
   ↓
Phase 3（Text-to-SQL + Guard）
   ↓
Phase 4（Agent Runtime + Tools）
   ├──→ Phase 5（Evidence + 报告）
   ├──→ Phase 6（权限 + 审计 + 审批）
   └──→ Phase 7（评测 + 可观测性）
              ↓
         Phase 8（MCP / 前端 / 部署）
              ↓
         Phase 9（真实模型稳定性与生产回归）
```

### 0.3 Definition of Done（每阶段通用）

- [ ] 代码合并，测试通过；
- [ ] 新增/修改的契约有 Pydantic 模型和版本；
- [ ] 有至少 1 个可重复运行的演示脚本或测试；
- [ ] 文档状态标记更新；
- [ ] 没有已知的高危安全问题；
- [ ] 性能/成本在预算内；
- [ ] 失败路径有明确错误码和日志。

---

## Phase 1.5 — 补齐数据与指标口径

### 目标

让数据真的能支持目标问答，尤其是 YoY 和“华东销售下降”的跨域因果链；把指标语义层从 v1 升到 v2。

### 任务

#### P1.5-01 扩展数据到 24 个月 ✅ 已完成

- 文件：`src/enterprise_business_agent/config.py`、`data_generator.py`、`tests/`、`.env.example`
- 内容：
  - 支持 `DATA_START=2024-01-01`、`DATA_MONTHS=24`；
  - 2024 年为正常基线，2025 年植入事件；
  - 趋势与事件按 `calendar_index(period)` 计算，不依赖生成起点；
  - 测试行数断言已更新为 24 个月；
  - 保留只生成 12 个月的能力，YoY 在缺基线时返回 `INSUFFICIENT_DATA`。
- 验收：
  - `2024-01` 至 `2025-12` 连续无缺月（质量检查逐表校验）；
  - QoQ / YoY 都能计算，`tests/test_comparison.py` 覆盖已知样例与缺基线场景；
  - 现有质量检查通过。

#### P1.5-02 修复华东因果链 ✅ 已完成

- 文件：`data_generator.py`、`models.py`、`sql/schema.sql`、`quality.py`、`tests/test_event_chain.py`
- 内容：
  - 库存快照按“区域 × 商品 × 月份”记录 `demand_units`、`units_sold`、`inventory_days`、`stockout_units`、`stockout_rate`、`fulfillment_rate`；
  - 华东核心商品（P001/P002/P003）自 2025-07 起采购入库上限逐月下降（0.95 → 0.28），期末库存先下降；
  - 销量由库存流决定：`售出 = min(需求, 期初 + 入库)`，缺货 = 需求 − 售出，不使用区域销量折减系数；
  - 其他区域同款商品使用正常补货上限，保留对照基线；
  - 增加 8 条事件断言与因果时间顺序检查。
- 验收：
  - 华东 2025Q4 缺货率显著高于其他区域；
  - 华东核心商品库存天数低于自身 2024Q4 基线；
  - 华东销量与销售额由缺货约束自然下降；
  - 时间顺序满足：库存下降 ≤ 缺货率上升 ≤ 销量下降。

#### P1.5-03 校准其他事件 ✅ 已完成

- 文件：`data_generator.py`、`quality.py`、`tests/test_event_chain.py`、`tests/test_data_quality.py`
- 已实现：
  - C006 ROI 在 Q4 动态下降（投入上升 + 转化率下降）；
  - P006/P008 为长期缺货风险；P004/P005 自 2025-07 起进入库存积压情景，2025Q4 库存天数显著高于 2024Q4；
  - 2024Q4 预算结余在 2025Q4 转为超预算；
  - 净利润 2025Q4 同比下降约 28%；
  - 市场部 2025Q4 人力成本显著增长。
- 验收：
  - 事件断言从 8 条扩充到 13 条，全部通过；
  - 事件之间不互相掩盖；
  - 华东异常保留其他区域同款商品作为反例。

#### P1.5-04 指标语义层 v2 ✅ 已完成

- 文件：`semantic_layer/metrics.json`、`semantic_layer/dimensions.json`、`src/enterprise_business_agent/schemas.py`、`semantic.py`、`semantic_validation.py`
- 已实现：
  - `MetricDefinition` 扩展 `synonyms`、`metric_type`、`additivity`、`numerator` / `denominator`、`allowed_aggregations`、`time_grains`、`nullable_when`、`zero_denominator`、`owner`、`version`；
  - 指标从 18 个扩展到 34 个；
  - 新增 7 个维度的维度注册表；
  - 保留 v1 字段，新增 `load_metrics` / `load_dimensions` / `normalize_name`；
  - 新增 `validate_semantic_against_engine` 和 `scripts/validate_semantic.py`。
- 验收：
  - 指标 JSON 通过 schema 校验；当前 `valid: true`；
  - name 唯一，来源表和字段存在；
  - 所有 ratio 指标有明确分子/分母；
  - 同义词覆盖常见中文说法；
  - 原有语义层测试已更新并通过。

#### P1.5-05 数据质量 v2

- 文件：`scripts/validate_data.py`、`src/enterprise_business_agent/quality.py`、`tests/`
- 内容：
  - 增加对账检查（finance vs sales / marketing / hr）；
  - 增加派生指标校验；
  - 增加事件断言；
  - 输出 JSON 报告到 `data/quality_report.json`；
  - 保留 `quality_check.py` 作为别名或弃用提示。
- 验收：
  - 报告包含 `passed` / `errors` / `warnings` / `event_assertions`；
  - 对账容差明确；
  - CI 可运行。

#### P1.5-06 只读数据库账号

- 文件：`sql/roles.sql`、`docker-compose.yml`、`README.md`
- 内容：
  - 创建 `eba_readonly` 角色；
  - 只授予 SELECT；
  - 设置 `default_transaction_read_only`、`statement_timeout`、`lock_timeout`；
  - Docker 初始化脚本自动执行；
  - 文档说明应用连接使用只读账号。
- 验收：
  - 只读账号执行写操作失败；
  - 应用查询正常。

#### P1.5-07 数据字典与数据快照

- 文件：`scripts/export_data_dictionary.py`、`docs/data-dictionary.md`、`data/.gitkeep`、`data/golden_metrics.json`
- 内容：
  - 从模型和指标生成数据字典；
  - 导出关键指标的 golden 值（用于回归测试）；
  - 数据快照目录约定。
- 验收：
  - 字典可重复生成；
  - CI 检查无漂移；
  - golden 值与数据库一致。

#### P1.5-08 文档与测试更新

- 文件：`README.md`、`docs/`、`tests/`
- 内容：
  - 更新数据范围、指标数量、事件说明；
  - 更新测试和演示命令；
  - 标注 Phase 1.5 完成状态。
- 验收：
  - 文档与代码一致；
  - 新测试全部通过。

### 演示问题

- “2025 年 11 月销售额同比变化是多少？”（验证 YoY）
- “为什么华东 2025 年 Q3 销售下降？”（验证库存/缺货证据可被发现）
- “哪个营销活动 ROI 最低，Q4 是否恶化？”（验证 C006）
- “哪些商品存在缺货风险，哪些存在积压风险？”（验证风险分类）

---

## Phase 2 — 语义查询与分析引擎

### 目标

在**不接 LLM** 的前提下，把“指标 + 维度 + 时间 + 对比 + 贡献 + 排名 + 趋势”做成确定性的查询与分析能力。这是整个项目的底座。

### 任务

> 实现说明：Phase 2 按仓库实际结构落地为 `metric_resolver.py`、`query_compiler.py`、`semantic_validation.py`、`analysis/contracts.py`、`analysis/time_ranges.py`、`analysis/engine.py` 和 `scripts/analyze.py`。以下保留原始任务设计，完成状态以标题标记为准；未实现的项在阶段验收中明确列出。

#### P2-01 Semantic 模块骨架 ✅ 已完成

- 文件：`src/enterprise_business_agent/semantic/models.py`、`loader.py`、`registry.py`
- 内容：
  - MetricDefinition v2、DimensionDefinition、Relationship；
  - 加载、校验、版本检查；
  - 指标/维度注册表；
  - 同义词索引。
- 验收：
  - 加载失败给出明确错误；
  - 指标引用完整性校验；
  - 单元测试覆盖。

#### P2-02 Metric Resolver ✅ 已完成

- 文件：`semantic/resolver.py`、`tests/unit/test_resolver.py`
- 内容：
  - 归一化（大小写、全半角、空格）；
  - 精确匹配 name/display_name/synonyms；
  - 模糊匹配（`difflib` 起步，必要时 rapidfuzz）；
  - 歧义检测与澄清问题；
  - 输出 `MetricResolution`。
- 验收：
  - “利润率”“毛利率”“GM”解析到 `gross_margin`；
  - “利润”触发澄清（毛利 vs 净利）；
  - “CAC”在数据不足时明确说明；
  - 20 条解析用例通过。

#### P2-03 Query Compiler ✅ 已完成

- 文件：`semantic/compiler.py`、`query/result.py`
- 内容：
  - 结构化公式 → SQLAlchemy Core 表达式；
  - 支持 dimensions、filters、time_range、grain；
  - ratio 指标分子/分母分别聚合；
  - 半加性指标跨时间取期末；
  - 复杂指标允许 `custom_sql`，但必须走 Guard（Phase 3 前先做只读执行 + 白名单）。
- 验收：
  - 不拼接用户输入；
  - 生成 SQL 可读、有参数绑定；
  - 对每个指标至少 1 个 golden 查询；
  - 维度/过滤/时间组合正确。

#### P2-04 只读执行器 🟡 基础执行已实现

- 文件：`query/executor.py`
- 内容：
  - 连接只读账号；
  - 设置 `statement_timeout`、行数上限；
  - 返回结构化结果 + 元数据；
  - 错误标准化。
- 验收：
  - 超时/行数上限生效；
  - 写操作失败；
  - 结果类型正确（Decimal/日期/None）。

#### P2-05 对比分析 ✅ 已完成

- 文件：`analysis/comparison.py`
- 内容：
  - MoM / QoQ / YoY；
  - 当前期/基期解析；
  - 缺失基线返回 `INSUFFICIENT_DATA`；
  - 输出绝对值、变化值、变化率、方向。
- 验收：
  - 跨年、跨季度、跨月正确；
  - 除零返回 None；
  - 12 个月数据下 YoY 明确失败。

#### P2-06 贡献分析 ✅ 已完成

- 文件：`analysis/contribution.py`
- 内容：
  - 加性指标贡献；
  - 贡献合计校验（与总变化一致）；
  - NULL 维度单列“未分类”；
  - 输出 `ContributionItem`。
- 验收：
  - 合计误差 < 0.1%；
  - 负贡献/正贡献正确；
  - 不支持 ratio 直接贡献，明确报错或走分解。

#### P2-07 排名与趋势 ✅ 已完成

- 文件：`analysis/ranking.py`、`analysis/trend.py`
- 内容：
  - Top N / Bottom N；
  - 占比、累计占比；
  - 趋势序列、环比序列、变点（简单规则）。
- 验收：
  - Top N 与 SQL 结果一致；
  - 并列排名稳定；
  - 空结果处理。

#### P2-08 异常检测 ✅ 已完成（规则基线）

- 文件：`analysis/anomaly.py`
- 内容：
  - 稳健 z-score（median/MAD）；
  - 阈值配置；
  - 输出异常点、分数、方法。
- 验收：
  - 植入的异常能被检出；
  - 正常波动不误报；
  - 方法可解释。

#### P2-09 API 与 CLI 🟡 CLI 已完成，API 未实现

- 文件：`api/app.py`、`api/routes/metrics.py`、`scripts/query_metric.py`
- 内容：
  - `GET /metrics`、`POST /metrics/query`；
  - CLI 查询示例；
  - 请求/响应 Pydantic 模型；
  - 错误码。
- 验收：
  - curl 可查询指标；
  - OpenAPI 文档可访问；
  - 响应包含 evidence draft。

#### P2-10 测试与 golden 数据 ✅ 已完成

- 文件：`tests/unit/`、`tests/integration/`、`tests/fixtures/`
- 内容：
  - Resolver / Compiler / Comparison / Contribution / Ranking / Anomaly 单测；
  - 基于生成数据的集成测试；
  - golden 指标值回归。
- 验收：
  - 覆盖率 ≥ 80%（核心模块）；
  - CI 通过。

### 演示问题

- “2025 年 11 月销售额环比、同比是多少？”
- “2025 年 Q4 净利下降了多少？按区域拆解贡献。”
- “销售额 Top 3 商品和 Bottom 3 商品。”
- “哪些商品缺货率异常？”

### 阶段验收

- [x] 结构化问题可通过 Python 服务函数与 CLI 得到正确数字；
- [x] 不依赖 LLM；
- [x] ratio 指标聚合正确（先汇总分子/分母，再相除）；
- [x] 贡献合计与总变化一致，残差单独返回；
- [x] 所有查询返回来源表、公式、SQL、参数和状态，可作为证据草稿；
- [x] Phase 2 阶段测试通过（Phase 3 已扩充为全量测试套件）；
- [x] SQLite 集成通过，PostgreSQL 方言编译通过；
- [x] FastAPI 接口与 OpenAPI 文档（Phase 3 已实现，见 [api/](../src/enterprise_business_agent/api/)）；
- [x] 独立只读数据库账号、statement timeout、行数上限（Phase 3 已实现）；
- [ ] 覆盖率 ≥ 80% 尚未测量。

---

## Phase 3 — 受控 Text-to-SQL、安全执行与 API

> 状态：✅ 已完成主体实现；真实外部 LLM 质量评测、独立 golden SQL 评测集延后。

### 目标

形成“自然语言 → 受控查询请求 → 安全执行 → 确定性分析 → 结构化 API 响应”的闭环。语义层优先；只有语义层无法表达且调用方显式 opt-in 时才允许 LLM 生成候选 SQL，候选 SQL 必须经过 Guard、只读执行和结果校验。**Guard 是硬门槛，不是可选。**

### 任务与状态

| 任务 | 状态 | 实际文件 | 说明 |
| --- | --- | --- | --- |
| P3-01 LLM Client | ✅ 已实现 | [llm/client.py](../src/enterprise_business_agent/llm/client.py)、[llm/schemas.py](../src/enterprise_business_agent/llm/schemas.py) | `complete_json()`；超时、有限重试、token 统计、错误标准化；密钥不进入 repr/日志/序列化 |
| P3-02 SQL Guard | ✅ 已实现 | [security/sql_guard.py](../src/enterprise_business_agent/security/sql_guard.py) | SQLGlot AST；单语句只读 SELECT/CTE；表/列/函数白名单；拒绝 DDL/DML/多语句/注释/`SELECT *`/`SELECT INTO`/`FOR UPDATE`；命名绑定参数；LIMIT/OFFSET 上限 |
| P3-03 只读执行器增强 | ✅ 已实现 | [security/executor.py](../src/enterprise_business_agent/security/executor.py)、[db.py](../src/enterprise_business_agent/db.py) | SQLite `mode=ro` + `PRAGMA query_only`；PostgreSQL 只读会话 + `statement_timeout`/`lock_timeout`；行数上限；超时/权限/数据库错误结构化；审计仅记录 SQL 哈希与元数据 |
| P3-04 Text-to-SQL 管线 | ⚠️ 部分实现 | [nl/planner.py](../src/enterprise_business_agent/nl/planner.py)、[nl/sql_fallback.py](../src/enterprise_business_agent/nl/sql_fallback.py)、[nl/service.py](../src/enterprise_business_agent/nl/service.py) | 语义优先；候选 SQL 仅显式 `allow_sql_fallback=true`；最多 2 次尝试并携带前次错误；未实现独立 golden SQL 执行率评测 |
| P3-05 评测集 | ⚠️ 部分实现 | [tests/evals/nl_cases.jsonl](../tests/evals/nl_cases.jsonl)、[run_nl_evals.py](../scripts/run_nl_evals.py) | 45 条 NL 规划 / 分析 / Guard 用例（Phase 4 已扩展到 75 条）；未建立 30 题 golden SQL + golden values 语料 |
| 独立只读账号 | ✅ 已实现 | [create_readonly_role.py](../scripts/create_readonly_role.py) | PostgreSQL 角色/授权/默认只读/超时；密码只从环境变量读取；本地 PG17 验证 |
| FastAPI 服务 | ✅ 已实现 | [api/](../src/enterprise_business_agent/api/) | 健康检查、自然语言分析、结构化指标查询、结果读取；请求 ID、JSON 日志、异常映射、API 超时 |
| 跨指标调查 | ✅ 已实现 | [nl/service.py](../src/enterprise_business_agent/nl/service.py) | 观测与推断分离；模拟数据因果免责声明；零基线缺货率处理 |
| 固定演示 | ✅ 已实现 | [run_phase3_demos.py](../scripts/run_phase3_demos.py) | 五个固定问题 + 跨指标调查；走只读连接 |

### 演示问题（已实际运行）

- “2025年第四季度净利润环比变化”；
- “华东销售额环比变化”；
- “2025年营销活动 ROI Top 5”；
- “2025年第四季度商品库存风险排名前5”；
- “2025年各部门人力成本贡献”；
- “华东销售下降是否与核心商品缺货有关”。

### 阶段验收

- [x] Guard 对抗样例在测试集中全部按预期拒绝（注入、多语句、未知表/列、危险函数、注释、未绑定值、`SELECT *`、OFFSET 超限）；
- [x] 所有执行路径只读；SQLite 与 PostgreSQL 均在数据库层拒绝写入；
- [x] 危险 SQL 0 执行；Guard 拒绝结构化返回错误码；
- [x] 超时、权限拒绝、数据库异常映射为结构化错误；
- [x] SQLite 全链路测试通过；PostgreSQL 17 集成测试与只读角色拒写通过；
- [x] 45 条离线评测通过；
- [x] 五个固定演示与跨指标调查返回 `status=ok`；
- [ ] 30 题 golden SQL 执行成功率 ≥ 90%（未建立该语料，本阶段不声称）；
- [ ] 结果准确率 ≥ 80% 的规模化 golden values 评测（仅固定演示做了人工核对）；
- [ ] 真实外部 LLM 的端到端规划质量与修复循环统计（本阶段使用假客户端测试）。


---

## Phase 4 — Agent Runtime + Tool Registry

> **当前状态（2026-10-29）：最小闭环已完成，完整目标未全部完成。**
>
> - 已完成：P4-01（预算/停止/动作校验，状态在 `agent/contracts.py` 与 `agent/graph.py`）、P4-02（Tool Registry 最小实现）、P4-03（8 个数据/分析工具）、P4-06（`decide → act` LangGraph 图）、P4-07（系统提示与结构化工具调用）、P4-09（6 条控制流标准问题，非 20 题真实模型 smoke test）；
> - 部分完成：P4-05（指标口径可由 `describe_metric` 返回，但没有知识检索/RAG）、P4-08（同步 run 创建/查询，没有 SSE、取消、持久化）；
> - 未完成：P4-04 领域组合工具；20 题以上真实模型评测；checkpointer；完整 Evidence 链。
>
> 不需要也不应该把固定工具序列包装成“自主 Agent”；当前测试明确区分脚本化假规划器与真实 LLM。

### Phase 4.1 — 真实 LLM 验收与关键安全补强（2026-10-30）

> **状态：安全补强已在 Phase 4.1 完成；真实 LLM 验收在 Phase 4.4 执行（两轮各 26 次真实 GLM 调用，首轮 17/24，修正后 23/24，见 [报告](archive/phase5/phase5-prerequisite-report.md) 第 6 节）。**

已完成：

- [x] 结构化查询边界：6 个 Agent 数据工具不再把 `QueryCompiler` SQL 文本直接交给 SQL Guard，而是走 `CompiledQuery → SemanticQueryGuard → TrustedSelect → SafeQueryExecutor.execute_select`；篡改指标、非 SELECT、越权表/列、原始 `TextClause`、非标量绑定参数均有拒绝测试（[semantic_guard.py](../src/enterprise_business_agent/security/semantic_guard.py)、[test_semantic_query_boundary.py](../tests/test_semantic_query_boundary.py)）；
- [x] 区域受限角色强制区域过滤，不能通过省略过滤或仅按区域分组读取未授权数据（[auth.py](../src/enterprise_business_agent/security/auth.py)）；
- [x] Prompt Injection / 越权测试：绕过权限、未授权区域、不存在工具、工具结果恶意指令、未验证数字、线程超时语义（[test_agent_security_boundaries.py](../tests/test_agent_security_boundaries.py)）；
- [x] 真实 LLM 配置诊断：`verify_agent_live.py --check-config` / `verify_llm_live.py --check-config`，不打印 key，检测 provider/model/base URL 与 Anthropic Messages 协议不匹配；
- [x] 26 条人工可核对真实模型用例（24 条计分 + 2 条负向控制）及运行器（[live_agent_cases.jsonl](../tests/evals/live_agent_cases.jsonl)、[run_agent_live_evals.py](../scripts/run_agent_live_evals.py)）；
- [x] PostgreSQL 集成：本轮在本地 Docker PostgreSQL 16 上 7 条通过，包含新增的结构化语义执行测试。

未完成/阻塞：

- [ ] 5 条真实模型 smoke 与 26 条全量真实评测。阻塞原因：`.env` 不会自动加载；进程环境未导出 key；`.env` 中 key 形状不是 Anthropic key；`LLM_PROVIDER=anthropic` 与 `LLM_MODEL=glm-5.3-flash`、`LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/` 不一致；无法访问提供方控制台，未确认 key 已轮换。按安全要求未使用该 key 发起真实请求；
- [ ] 真实模型注入抵抗率、真实延迟/成本统计：没有真实调用，不报告；
- [ ] 完整 RBAC/RLS、持久化审计、人工审批仍属 Phase 6。

真实验收的前置条件：在提供方控制台轮换曾暴露的 key；配置有效的 Anthropic key/model/base URL；确认 `--check-config` 通过；再依次运行 5 条 smoke、修复问题、运行 26 条全量，并保存原始记录。

### Phase 4.2–4.5 与 Phase 5 前置治理（2026-10-30）

> **状态：GLM 提供方接入、真实 Function Calling、运行治理与企业基础控制已实现并通过离线/集成回归；真实 GLM 验收结果见第 6 节与 [Phase 5 前开发完成报告](archive/phase5/phase5-prerequisite-report.md)。**

已完成：

- [x] Phase 4.2 GLM 提供方：独立 `GLMClient`（OpenAI 兼容 Chat Completions）与 Provider Factory，保留 Anthropic 适配器；`LLM_PROVIDER` / `LLM_MODEL` / `LLM_BASE_URL` / key 全部走配置，key 只来自环境变量；401/403 不重试，429/5xx 指数退避，超时与网络错误结构化返回；真实协议探测确认 `finish_reason=tool_calls`、`tool_calls[].id`、JSON 字符串 `arguments`、最终 `finish_reason=stop`（[llm/glm.py](../src/enterprise_business_agent/llm/glm.py)、[llm/factory.py](../src/enterprise_business_agent/llm/client.py)）；
- [x] Phase 4.3 自主循环补强：空响应单次重试、动态工具上下文、token 预算、重复调用检测、连续失败熔断、结构化失败状态、prompt 版本号 `phase4.3-2026-10-30.2`（[agent/graph.py](../src/enterprise_business_agent/agent/graph.py)）；
- [x] Phase 4.5 运行治理：持久化 `agent_runs` / `agent_audit_events`，异步 Run API、SSE 进度、取消、幂等键、重启后 `interrupted` 标记、线程超时与数据库 statement timeout 双重限制（[api/run_manager.py](../src/enterprise_business_agent/api/run_manager.py)、[api/run_store.py](../src/enterprise_business_agent/api/run_store.py)）；
- [x] 企业基础控制：角色级业务域/指标/维度/区域/数据资源白名单，区域受限角色强制过滤；持久化审计关联 principal / run / tool_call；Prompt Injection 与越权拒绝测试；Agent 只有只读工具，不具备修改业务数据或自动执行采购/财务动作的代码路径；
- [x] 可观测性：统一 run trace（决策、模型响应、工具调用、延迟、错误、最终结论）+ 可选 OpenTelemetry span（`EBA_OTEL_ENABLED`，无 SDK 时为 no-op，故障隔离）；
- [x] 评测扩展：确定性 NL/分析用例从 45 条扩到 75 条，覆盖 Sales / Marketing / Finance / Supply Chain / HR 与跨域调查。

仍属 Phase 6（Phase 5 已完成证据账本、finding、归因、报告与数字校验）：

- [ ] 数据库 RLS、列级权限、人工审批流；
- [ ] 多进程任务队列、checkpointer 断点恢复；
- [ ] 真实 OpenTelemetry exporter / Langfuse 接入。


### 目标

让 Agent 根据当前状态自主选择下一步调查动作，而不是固定流程。**先做 5–8 个高质量工具，不追求工具数量。**

### 任务

#### P4-01 Agent State 与 Policies

- 文件：`agent/state.py`、`agent/policies.py`
- 内容：
  - 按 [03-agent-runtime.md](03-agent-runtime.md) 第 2 节实现状态；
  - 预算、最大迭代、停止条件；
  - 动作校验。
- 验收：
  - 状态可序列化/反序列化；
  - 预算生效；
  - 非法动作被拒绝。

#### P4-02 Tool Registry

- 文件：`tools/base.py`、`tools/registry.py`
- 内容：
  - ToolSpec / ToolResult / ToolError；
  - 注册、查找、权限过滤；
  - 输入校验、超时、重试、缓存；
  - 审计。
- 验收：
  - 未注册工具不可调用；
  - 参数错误返回结构化错误；
  - 超时/重试正确；
  - 相同调用命中缓存。

#### P4-03 指标与分析工具

- 文件：`tools/metric_tools.py`、`tools/analysis_tools.py`
- 内容：
  - `list_metrics`、`describe_metric`、`query_metric`；
  - `compare_periods`、`rank_dimension`、`contribution`、`trend`、`anomaly_scan`；
  - 每个工具返回 evidence drafts。
- 验收：
  - 工具输入输出 schema 完整；
  - 与 Phase 2 引擎结果一致；
  - 证据字段完整。

#### P4-04 领域工具

- 文件：`tools/domain_tools.py`
- 内容：
  - `sales_diagnose`、`marketing_efficiency`、`finance_profit_bridge`、`supply_risk`、`hr_cost_diagnose`、`cross_domain_scan`；
  - 组合分析工具，不直接拼 SQL。
- 验收：
  - 每个工具至少 1 个演示问题；
  - 返回结构化发现和证据；
  - 不越权。

#### P4-05 Knowledge 工具

- 文件：`tools/knowledge_tools.py`、`semantic_layer/knowledge/`
- 内容：
  - 指标定义、口径规则、分析案例；
  - 先用 JSON/Markdown 直接检索；
  - 需要时再加 pgvector。
- 验收：
  - “毛利率怎么定义”可回答并引用指标；
  - 不把知识库内容当指令执行。

#### P4-06 LangGraph 图

- 文件：`agent/graph.py`、`agent/nodes/`
- 内容：
  - Understand / Plan / SelectAction / ExecuteTool / Observe / Decide / Verify / Analyze / Synthesize / Validate / Clarify / Finish；
  - 条件边；
  - checkpointer（先用内存/Postgres）。
- 验收：
  - 不同问题走不同工具序列；
  - 能插入计划外调查；
  - 预算/停止条件生效；
  - 节点可单独测试。

#### P4-07 Prompt 与结构化输出

- 文件：`agent/prompts/`、`llm/schemas.py`
- 内容：
  - 系统提示、理解提示、计划提示、动作选择提示、报告提示；
  - JSON schema；
  - 注入防护；
  - 上下文预算。
- 验收：
  - 输出可解析；
  - 非法动作被拒绝并重试；
  - 注入样例不越权。

#### P4-08 Run API 与 SSE

- 文件：`api/routes/runs.py`、`api/app.py`
- 内容：
  - 创建 run、查询状态、SSE、取消；
  - run 持久化；
  - 错误码。
- 验收：
  - curl 可创建并跟踪 run；
  - SSE 事件完整；
  - 取消生效。

#### P4-09 Agent smoke test

- 文件：`tests/integration/test_agent_smoke.py`、`tests/evals/agent_smoke.jsonl`
- 内容：
  - 20 个问题，覆盖 5 个域；
  - 记录工具序列、停止原因、证据数；
  - 不要求答案完美，先要求流程正确。
- 验收：
  - 20 题全部完成；
  - 无越权/无危险 SQL；
  - 工具序列因问题而异。

### 演示问题

- “为什么本季度利润下降？”（Finance → Sales → Supply → Marketing → HR）
- “哪个营销活动 ROI 最低？”（Marketing，不走 Supply）
- “哪些商品库存风险最高？”（Supply，可能查 Sales）
- “哪个部门成本增长最快？”（HR，可能查 Finance）

### 阶段验收

- [x] 单 Agent + 工具，无多 Agent；
- [x] 不同问题 trace 不同（脚本化控制流评测覆盖 6 条问题）；
- [ ] 可插入计划外调查（架构允许，但尚无真实模型验收）；
- [x] 预算、停止条件、权限、重复调用、连续失败生效；
- [ ] 20 题 smoke test 通过（当前 6 条，且为假规划器）；
- [ ] run 可回放（无 checkpointer/持久化）。

---

## Phase 5 — Evidence Chain + 报告

> **状态（2026-10-31）：Phase 5.1–5.6 已实现并通过自动化验收，真实 GLM 五场景已执行。** 实际实现按 5.1 Evidence Ledger、5.2 Collector、5.3 Finding Engine、5.4 归因、5.5 Report Generator、5.6 Numeric Validator 组织；下方 P5-01–P5-07 为最初的任务拆分，部分文件名与范围已被实际实现替代：
>
> - `evidence/models.py` → [evidence/contracts.py](../src/enterprise_business_agent/evidence/contracts.py) + [evidence/schema.py](../src/enterprise_business_agent/evidence/schema.py)；
> - P5-03“源表重算/跨表对账/反例搜索”未作为独立 verifier 实现；当前以证据来源、原始结果、口径版本和报告交叉校验替代，属于已知范围差异；
> - P5-06 仅保留加性贡献与比率分解；毛利率桥（price/cost/mix）未实现；
> - P5-07 人工评分 10 份报告未执行，改为 Phase 5 专项自动化测试 + 5 场景真实模型验收。
>
> 完整结果见 [Phase 5 完成报告](archive/phase5/phase5-completion-report.md)。

### 目标

让每个结论可追溯、每个数字可验证，报告能用于管理沟通，而不是“模型作文”。

### 任务

#### P5-01 Evidence 模型与账本

- 文件：`evidence/models.py`、`evidence/ledger.py`
- 内容：
  - Evidence / Finding / Recommendation / Report；
  - 证据分配 ID、去重、索引；
  - 与 tool_call 关联。
- 验收：
  - 每条证据可追溯到工具调用；
  - 重复证据可合并；
  - 账本可查询。

#### P5-02 证据生成与观察

- 文件：`tools/`、`agent/nodes/observe.py`
- 内容：
  - 所有工具返回 evidence drafts；
  - observe 节点写入账本；
  - 异常/冲突检测。
- 验收：
  - 查询类工具 100% 产生证据；
  - 证据字段完整（metric/value/period/dimensions/source/calculation）。

#### P5-03 验证器

- 文件：`evidence/verifier.py`
- 内容：
  - 源表重算；
  - 跨表对账；
  - 分维度复核；
  - 反例搜索；
  - 冲突记录。
- 验收：
  - 关键结论至少 2 条独立证据；
  - 冲突会降置信度；
  - 验证结果写入报告。

#### P5-04 报告生成

- 文件：`evidence/report.py`、`agent/prompts/report.md`
- 内容：
  - 结构化骨架；
  - LLM 叙述；
  - 建议生成；
  - 置信度/限制。
- 验收：
  - 报告包含 7 个部分；
  - 每条建议绑定 Finding；
  - 高风险建议标记审批。

#### P5-05 数字校验器

- 文件：`evidence/validator.py`
- 内容：
  - 提取数字；
  - 与证据账本匹配；
  - 容差；
  - 失败重写/模板兜底。
- 验收：
  - 报告中无不可追溯数字；
  - 失败时不会把未校验文本返回；
  - 单测覆盖金额/百分比/千分位。

#### P5-06 贡献与比率分解

- 文件：`analysis/decomposition.py`
- 内容：
  - 加性贡献（已完成）；
  - 毛利率桥（price / cost / mix）；
  - 费用率分解。
- 验收：
  - 桥的合计与总变化一致；
  - 方法写入证据；
  - 不支持时明确说明。

#### P5-07 报告质量评测

- 文件：`tests/evals/report_rubric.md`、`scripts/run_report_eval.py`
- 内容：
  - 结论是否有证据；
  - 建议是否可行动；
  - 是否遗漏主要因素；
  - 语言是否夸大因果。
- 验收：
  - 10 份报告人工评分 ≥ 4/5；
  - 主要问题有记录。

### 演示问题

- “为什么本季度利润下降？”→ 完整报告 + 证据链 + 建议
- “为什么华东销售下降？”→ 库存/缺货证据链
- “哪个活动 ROI 最低，应该怎么调整？”→ 营销建议 + 风险

### 阶段验收

- [ ] 每个 primary finding ≥ 2 条证据，至少 1 条跨域（当前不强制，单证据 finding 会在 limitations 标注）；
- [x] 报告数字可追溯到本次 run 的 Evidence（`ValueReference` + 独立 Validator）；
- [x] 冲突/数据不足有明确说明；
- [x] 建议包含依据、预期方向、风险、验证方式，高风险建议标记 `requires_approval`；
- [ ] 报告可按步回放（run/证据/发现/报告已持久化，回放接口未实现）；
- [x] Phase 5.1–5.6 专项测试、全量回归、PostgreSQL 集成、确定性评测与控制流评测通过；
- [x] 5 个业务场景真实 GLM 验收已执行，真实与脚本化结果分开记录。

---

## Phase 6 — 企业级控制

### 目标

让系统在真实组织中使用：权限、审计、异常恢复、人工审批。

### 任务

#### P6-01 RBAC

- 文件：`security/permissions.py`、`tests/unit/test_permissions.py`
- 内容：
  - 角色策略；
  - 域/指标/工具/行列权限；
  - 策略加载与校验。
- 验收：
  - 权限矩阵测试通过；
  - 未授权访问被拒绝；
  - 拒绝有审计。

#### P6-02 行级过滤

- 文件：`semantic/compiler.py`、`sql/guard.py`
- 内容：
  - 按角色注入 region/department 过滤；
  - 视图 + RLS（可选）；
  - 防止聚合泄露。
- 验收：
  - Sales Manager 只能看本区域；
  - 聚合结果不泄露全局；
  - 注入后语义正确。

#### P6-03 审计

- 文件：`security/audit.py`、`models.py`（运行表）
- 内容：
  - 审计事件模型；
  - 记录 run/tool/sql/guard/permission/report/approval；
  - 查询接口。
- 验收：
  - 所有关键动作有记录；
  - 不可篡改；
  - 可按 run/user 检索。

#### P6-04 异常恢复

- 文件：`agent/nodes/`、`tools/registry.py`
- 内容：
  - LLM 非法 JSON 重试；
  - 工具超时重试/降级；
  - SQL 修复循环；
  - 冲突处理；
  - 数据不足降级。
- 验收：
  - 注入故障后 run 仍能完成或明确失败；
  - 部分结果保留；
  - 恢复过程有日志。

#### P6-05 人工审批

- 文件：`api/routes/approvals.py`、`security/approvals.py`
- 内容：
  - 高风险建议进入审批；
  - 状态机；
  - 审批记录。
- 验收：
  - 未审批建议不能标记为可执行；
  - 审批/拒绝有审计；
  - 不自动执行。

### 阶段验收

- [ ] RBAC 矩阵测试通过；
- [ ] 行级权限双层生效；
- [ ] 审计完整；
- [ ] 故障注入测试通过；
- [ ] 审批流可用；
- [ ] 安全清单（[04](04-security-and-governance.md) 第 9 节）全部通过。

---

## Phase 7 — 评测与可观测性

### 目标

用数据证明 Agent 好不好，能定位失败原因，能控制成本。

### 任务

#### P7-01 评测集

- 文件：`tests/evals/questions.jsonl`
- 内容：
  - 50–100 题，覆盖 9 类（见 [06-evaluation-and-observability.md](06-evaluation-and-observability.md)）；
  - 每题含期望指标、工具、golden 值、证据要求、容差。
- 验收：
  - 题目覆盖五大域和跨域；
  - 有歧义/权限/对抗/数据不足题；
  - golden 值由 Phase 2 引擎生成并人工复核。

#### P7-02 评测 Harness

- 文件：`scripts/run_eval.py`、`tests/evals/`
- 内容：
  - 批量运行；
  - 计算指标；
  - 输出 JSON/HTML 报告；
  - 与基线对比。
- 验收：
  - 可重复；
  - 失败样例可定位；
  - CI 可跑子集。

#### P7-03 可观测性

- 文件：`observability/tracing.py`、`observability/logging.py`
- 内容：
  - OTel span：run/step/tool/sql/llm；
  - 结构化日志；
  - Langfuse / Phoenix 接入（可选）；
  - 成本/延迟统计。
- 验收：
  - 一次 run 可在 trace 中完整查看；
  - 能回答“哪一步最慢、哪次 SQL 失败、花了多少 token”；
  - 日志脱敏。

#### P7-04 CI 分层

- 文件：`.github/workflows/` 或本地脚本
- 内容：
  - PR：单测 + 小评测集（10–20 题）+ 安全测试；
  - Nightly：全量评测 + 数据质量 + 成本报告；
  - 回归阈值。
- 验收：
  - PR 不通过不能合并；
  - Nightly 有报告；
  - 阈值可配置。

#### P7-05 失败分类

- 文件：`docs/failure-taxonomy.md`
- 内容：
  - 指标解析错、工具选错、SQL 错、数据不足、证据不足、报告错、权限错、超时；
  - 每类有样例和修复建议。
- 验收：
  - 评测失败都能归类；
  - 每类有 owner/优先级。

### 阶段验收

- [ ] 50–100 题评测集可跑；
- [ ] 核心指标达到阈值；
- [ ] trace 完整；
- [ ] 成本/延迟可监控；
- [ ] CI 回归生效；
- [ ] 失败分类清晰。

---

## Phase 8 — 可选扩展

### P8-01 MCP 适配

- 前提：Tool Registry 稳定；
- 内容：把领域工具包装成 MCP server；
- 验收：外部客户端可调用；权限/审计不绕过。

### P8-02 前端

- 内容：对话、进度、报告、证据链、审批；
- 验收：核心流程可用；不替代后端权限。

### P8-03 生产部署

- 内容：Docker、只读副本、备份、限流、密钥管理；
- 验收：压测/故障演练通过。

---

## Phase 9 — 真实 LLM 稳定性与生产回归

### 目标

在 Phase 7 的 92 条语料和 Phase 8 的生产链路上，用真实 GLM 模型执行可复现的稳定性验收；复现历史超时、结论越界等问题，做最小修复，并把真实结果、失败根因、离线结果和未完成项分开归档。

### P9.1-01 真实配置核验与三个历史问题复现 ✅ 已完成

- 文件：`.env`（只读取，不回显密钥）、`config.py`、`agent/relations.py`、`agent/evaluation.py`、`agent/graph.py`、`tests/`；
- 内容：
  - 核验 `glm` / `glm-5.3-flash` / `open.bigmodel.cn`、Key 加载、120s 超时、1 次重试、4000 输出上限、300s/120k Token Run 预算；
  - 复现 `live-004`、`live-005` 超时：20s 单次超时低于长上下文最终生成耗时，重试耗尽后记 `llm_timeout`；
  - 复现 `live-023` 结论越界：评测器把“EC 与最高的 NC”误读为 EC 是最高；
  - 做最小修复：运行配置、比较语境识别、多月期间选择、标题数字语境、元数据回答状态；
  - 验证修复不改变 Ground Truth、`required_calls`、`allowed_statuses` 或通过阈值。
- 验收：
  - 真实模型复测 `live-004` PASS（6 次调用、250.5s、51,540/4,940 Token）；
  - `live-005` 修正后 53.98s 安全返回 `no_evidence`，未编造数字；
  - `live-023` 定向复测 PASS（4 次调用、77.5s、无关系校验问题）。

### P9.1-02 真实全量回归与失败分类（Final）✅ 已完成

- 文件：`scripts/run_phase7_evals.py`、`data/exports/phase9_1_final.full.json`、`docs/archive/phase9/PHASE9_1_FINAL_FULL_RUN.md`；
- 内容：
  - 92 条语料全部真实执行，90 条计分；
  - 记录工具选择、参数、查询、任务完成、数字准确、Evidence 完整性、结论越界、调用次数、延迟、Token；
  - 第一次运行在 83/92 处因连续 `llm_unavailable` 停止，使用 `--resume` 完成剩余 9 条；
  - 21 个失败拆分为 5 个 provider 不可用、7 个旧评测器误报、9 个保留契约/语义偏差；
  - 7 个误报用例在离线后验（消息对象重建后）关系问题全部归零，后验 76/90；该值是离线复算，不是新的真实运行；
  - 未配置模型单价时成本保持 null，不估算。
- 验收：
  - 全量 as-run：**69/90**，真实调用 **210 次**，1,445,546 / 75,928 Token，P50/P95 32.5s / 102.1s，P50/P95 受 provider 阻塞影响明显升高；
  - 安全拦截 7/10（3 个未拦截均为 provider 失败）；数字准确率 0.9851；
  - 离线 `pytest -q`：**746 passed, 30 skipped**；
  - 失败用例、历史归档和 Ground Truth 保留。

### P9.1-03 评测器最小修复与回归测试 ✅ 已完成

- 文件：`src/enterprise_business_agent/agent/relations.py`、`tests/test_phase7_6_evaluator.py`；
- 内容：
  - 修复 10 类解析/归属错误：变化字段提示、括号遮蔽、比较短语、排名区间、排名对照、排名列示主语、排序线索门禁、指标边距距离、子集作用域、全负贡献方向；
  - 新增 8 个负向回归测试和 3 个正向对照，评测器测试文件 **89 passed**；正向对照确认守卫没有屏蔽真实错误；
  - 未修改 Ground Truth、`required_calls`、`allowed_statuses`、阈值或安全规则。
- 验收：
  - 7 个误报用例在当前离线校验器下不再报 `conclusion_overreach`；
  - `live-005`、`phase7-051` 的真实截断越界仍被保留。

### P9.1-04 真实定向复测与多种子稳定性（Final）🟡 部分完成/受 provider 限制

- 内容：选取历史修复项、误报项和保留偏差用例执行真实模型定向复测；对代表性用例设置 `LLM_TEMPERATURE=0.8`、`LLM_SEED=42/43/44` 做多种子重复。
- 定向复测结果：26 条中 **18 条完成，13 PASS / 5 FAIL**；5 个失败全部是保留契约偏差（`phase7-035/041/042/044/051`）。5 个旧评测器误报用例（`live-010`、`live-023`、`phase7-003/021/030`）真实通过，`live-004` 从 provider 失败恢复。剩余 8 条因 provider 卡住未执行，未推算。
- 多种子结果：seed 42 跑 3 条，**2 PASS / 1 FAIL**（`phase7-058` 为稳定契约偏差）；seed 43 被卡停，seed 44 未启动，provider 是否遵守 seed 未验证。
- 结果详见 `docs/archive/phase9/PHASE9_1_FINAL_REPORT.md` 第 6、7 节；provider 在本轮多次出现十余分钟无响应，且单例 300 s 预算未能在卡住的读取中生效。

### 阶段验收

- [x] 真实模型配置和 preflight 通过；
- [x] 三个历史问题已复现并给出根因；
- [x] 最小修复有定向测试覆盖；
- [x] 92 条原有语料执行真实全量回归并单独归档；
- [x] 真实结果与离线结果分开记录，未用假模型制造通过；
- [x] 修复后完整 92 条二次真实全量回归（as-run 69/90，provider 中断并恢复）；
- [x] 评测器 10 类误报已修复，新增 8 个负向回归测试和 3 个正向对照；
- [x] 全部 26 条定向复测用例完成（18 条在 Phase 9.1-Final，剩余 8 条由 Phase 9.2.1 补跑：4 PASS / 4 FAIL）；
- [ ] CI 自动门禁（多轮/多种子统计已由 Phase 9.2.1 完成 24 条 × 3 seeds，尚未接入 CI）；
- [ ] 模型单价配置后的成本核算；
- [ ] 密钥轮换技术验证（用户已确认，本工具未独立验证）。

---

## Phase 9.2 — 真实 PostgreSQL 验收

### 目标

在真实 PostgreSQL 上验证运行时最小权限、持久化、Checkpoint 恢复、并发控制、Lease/Fencing、幂等、服务重启、用户/区域隔离和数据库异常处理；不允许用 SQLite 结果代替。

### P9.2-01 真实 PG 权限与功能验收 ✅ 已完成

- 文件：`tests/test_phase9_2_postgres_acceptance.py`、`docs/archive/phase9/PHASE9_2_REPORT.md`；
- 环境：PostgreSQL 17.11（`mark-postgres`，`pgvector/pgvector:pg17`），验收库 `eba_phase9_2_test`；
- 内容：
  - `eba_runtime` 仅能对 `agent_*` 表 DML，不能读业务表、不能 DDL；
  - `eba_readonly` 可读业务表，不能写、不能 DDL；
  - 真实 PG 上的并发抢占、Lease 过期接管、fencing、并发幂等、重启标记 `interrupted`；
  - 区域/部门越权（省略、替换、`in` 放宽）返回 403；跨用户 Run 读取 403/404；
  - 撤销 `CONNECT`、表缺失、连接不可达时快速失败，不回退 SQLite；清理失败整体回滚。
- 验收：
  - 真实 PG 专项 **30 passed / 0 failed**（4.12s）；
  - 未设置 PG URL 的完整离线回归 **746 passed, 30 skipped**；
  - LLM 使用脚本化替身，本阶段不产生真实模型结论。

### 阶段验收

- [x] 真实 PostgreSQL 上完成权限、持久化、恢复、并发、幂等、隔离和异常验收；
- [x] SQLite 结果未计入 PostgreSQL 通过数；
- [ ] 外部托管 PostgreSQL、多节点、TLS、备份/PITR、容量压测；
- [x] 真实模型 + PostgreSQL 全链路（Phase 9.3 容器内浏览器 E2E 通过）；
- [x] Docker 镜像构建与 Compose 部署（Phase 9.3；生产环境验收仍未完成）。

---

## Phase 9.2.1 — 模型调用可靠性与任务预算治理

### 目标

让单例 300 秒任务预算在模型读取阶段真正生效：超时/取消能中断已发出的 HTTP 请求、停止后续重试、释放线程并正确更新任务状态；区分 provider 故障与任务预算耗尽；避免 provider 不可用时长时间卡死或重复消耗 Token。

### P9.2.1-01 硬边界传输层 ✅ 已完成

- 文件：`src/enterprise_business_agent/llm/transport.py`；
- 连接/读取/写入/连接池超时与单请求总墙钟分离；
- 请求在独立 daemon 线程执行，调用线程按 50ms 轮询取消事件和总超时；
- 取消/超时时 `shutdown(SHUT_RDWR)` 唤醒阻塞读取，再关闭客户端；
- `active_request_threads()` 供测试验证线程退出。

### P9.2.1-02 取消与硬截止传播 ✅ 已完成

- 文件：`llm/resilience.py`、`agent/graph.py`；
- `ContextVar` 绑定 Run 取消事件与截止时间，覆盖决策、报告、续写、纠错；
- `LLMCancelled` → `cancelled` / `cancelled_by_user`；
- `task_deadline` 超时 → `timeout` / `time_budget_exhausted` / `deadline_exceeded`；
- 取消在持久化前到达时，完整回答不能覆盖 `cancelled`。

### P9.2.1-03 重试、故障分类与熔断 ✅ 已完成

- 401/403 不重试；429/5xx/超时/连接错误重试；
- `Retry-After` 解析与截断；指数退避上限；
- 剩余预算不足以完成「退避 + 5s 最小窗口」时停止重试；
- 连续临时故障熔断，冷却后半开探测；认证/协议错误不计入；
- 修正早期 provider 超时被误判为 Run 硬截止的分类缺陷。

### P9.2.1-04 测试与真实复测 ✅ 已完成

- 新增 18 个测试：传输 11 个、Run/Worker/Lease 7 个；
- 完整离线回归 **764 passed, 30 skipped**；
- 8 条遗留定向复测全部执行：**4 PASS / 4 FAIL**，真实调用 25 次；失败均为保留安全契约偏差；
- 第一次复测暴露分类缺陷，修复后完整重跑，两次归档都保留。

### P9.2.1-05 多种子真实评测 ✅ 已完成（有限样本）

- 24 条代表用例 × seeds 42/7/123，温度 0.8，按 seed 独立统计；
- 通过率 **54.55% / 63.64% / 72.73%**，任务完成率 **72.73% / 90.91% / 100%**；
- 11 条工具序列存在跨 seed 差异，其中 7 条涉及某个 seed 的 provider/预算失败；
- 结果不与 92 条全量回归混算；provider 是否遵守 `seed` 未验证。

### 阶段验收

- [x] 超时后真正中断请求、停止重试、释放线程；
- [x] 取消、Worker 中断与租约接管不产生旧写入或重复 `run_finished`；
- [x] provider 故障与任务预算耗尽分类正确；
- [x] 8 条遗留定向复测全部完成；
- [x] 离线完整回归通过；
- [x] 多种子按 seed 统计通过率、完成率、工具差异、耗时与 Token；
- [ ] 更大样本、更多 seed 的稳定性统计；
- [ ] provider 对 `seed` 参数的遵守情况；
- [ ] 多进程 + 真实 PostgreSQL 的「阻塞请求 + 租约接管」组合；
- [x] 外部 OTLP、Docker 构建部署、浏览器 E2E（Phase 9.3 完成）。

---

## Phase 9.3 — Docker 构建部署、外部 OTLP 与浏览器 E2E

报告：[docs/archive/phase9/PHASE9_3_REPORT.md](archive/phase9/PHASE9_3_REPORT.md)

### 目标

把 Phase 8.3/8.4 中“代码存在但未实际验证”的部署与可观测能力跑通一次真实链路，并明确所有未验证边界。

### P9.3-01 Docker 镜像构建 ✅ 已完成（存在偏差）

- 基础镜像 `python:3.12-slim`（digest 固定），`pip install ".[otel]"`，镜像 410 MB；
- 首次拉取连续 6 次 TLS 握手超时，网络恢复后成功；备选“本地 postgres + apt Python”路径两次 OOM，未使用；
- 仓库 Dockerfile 两次完整构建成功；第三次全量重建在 `psycopg-binary` 下载处 PyPI 读超时失败；
- **部署镜像为派生层**（已构建镜像 + `COPY src`），不能表述为“最终源码通过单次 canonical 构建”。

### P9.3-02 Compose + 真实 PostgreSQL 部署 ✅ 已完成

- 独立项目 `eba-phase93`，PostgreSQL 16 映射 `55432`（宿主 5432 被既有容器占用，未停止）；
- init 容器执行 Alembic、初始化、只读/运行时角色、运行时 schema、Phase 5 迁移与种子数据，退出码 0；
- `public` schema 23 张表；`/health` 返回 `database=ok`、`llm_configured=true`；两个角色鉴权通过。

### P9.3-03 外部 OTLP Collector 链路 ✅ 已完成（自建）

- 官方 `otelcol-contrib 0.164.0`，SHA-256 校验通过，OTLP/HTTP `4318` → file + debug；
- 应用以 OTLP/HTTP 导出 `agent.*`、`llm.complete`、`tool.call`、`db.query`、`evidence.collect`、`finding.build`、`report.generate`、`approval.ensure` 等 span；
- Collector 收到 **257 个 span**，`service.name` 均为 `enterprise-business-agent`；容器内应用向宿主 Collector 导出成功；
- 边界：Collector 在宿主运行；托管后端、采样、长期存储、指标/日志链路未验证。

### P9.3-04 浏览器端到端验收 ✅ 已完成

- `scripts/browser_e2e.py` 通过 CDP 驱动真实 Chrome，覆盖提问、调查、报告、Evidence、Findings、审批、历史/刷新、取消恢复；
- 主机后端：`ok=true`，答案 952 字、4 步时间线、3 次工具调用、报告 3146 字、3 条 Evidence、3 条 Finding、审批 `pending → approved`、历史 24 条；
- 容器后端：`ok=true`，答案 1014 字、报告 2879 字、审批通过、历史 3 条、取消 `running → cancelling → cancelled`。

### P9.3-05 真实缺陷修复 ✅ 已完成（1 项未在真实停顿中复验）

- `api/__init__.py` 急切导入导致 init 容器无法创建运行时 schema → 改为惰性导出；
- 取消后工作台停留在 `cancelling` → 取消后轮询终态（最多 30 s）；
- 超时清理调用 `client.close()` 阻塞调用线程（32 分 12 秒停顿时段）→ 关闭移至守护线程，新增回归测试；
- 第三项只证明“清理不再阻塞调用线程”，未在真实网络停顿中再次复现。

### 阶段验收

- [x] 真实 `docker build` 与镜像内 OTLP 依赖验证；
- [x] `docker compose up`、init/migrations/seeding、健康检查与鉴权；
- [x] 外部 OTLP Collector 接收应用 span；
- [x] 浏览器 E2E 覆盖提问、调查、报告、Evidence、历史、审批、异常恢复（主机与容器后端）；
- [x] 修复部署阻塞、取消 UI 与超时清理三处真实缺陷；
- [ ] 包含最终源码的单一 canonical 镜像构建（PyPI 超时阻塞，使用派生层）；
- [ ] 容器内真实模型链路的重复稳定性（出现 32 分钟停顿，单次 5.5 分钟通过）；
- [ ] Compose 内运行的 Collector、托管 OTLP 后端、采样与长期存储；
- [ ] 托管 PostgreSQL、多节点、TLS、备份/PITR、容量压测（沿用 Phase 9.2 边界）；
- [ ] 密钥轮换的技术验证（用户声明“确认安全”不构成验证）。

---

## 4. 里程碑与演示脚本

| 里程碑 | 演示 | 证明什么 |
| --- | --- | --- |
| M1（Phase 1.5） | `validate_data.py` + 事件断言 | 数据可信、事件可发现 |
| M2（Phase 2） | CLI/API 查询指标、贡献拆解 | 确定性分析能力 |
| M3（Phase 3） | Text-to-SQL + Guard 拦截 | 安全兜底 |
| M4（Phase 4） | 不同问题不同工具序列 | Agent 自主性 |
| M5（Phase 5） | 完整报告 + 证据链 | 可追溯、可验证 |
| M6（Phase 6） | 权限/审计/审批演示 | 企业可用 |
| M7（Phase 7） | 评测报告 + trace | 可度量、可优化 |
| M8（Phase 9.1） | 真实 GLM 全量回归 + 定向复测 + 失败分类 | 真实可用、失败可解释 |
| M9（Phase 9.3） | Compose + 真实 PG + 外部 OTLP + 浏览器全流程 | 部署可运行、链路可见、UI 全流程可验收 |

---

## 5. 风险登记

| 风险 | 阶段 | 影响 | 缓解 |
| --- | --- | --- | --- |
| 模拟事件不可发现 | 1.5 | Agent 无法归因 | 事件断言 + 校准 |
| 指标口径错误 | 2 | 全链路错误 | v2 schema + golden 测试 |
| ratio 聚合错误 | 2 | 数字错误 | 分子/分母规则 + 测试 |
| SQL Guard 绕过 | 3 | 安全事件 | AST + 只读账号 + 对抗测试 |
| Agent 过度调查 | 4 | 成本/延迟 | 预算 + 停止条件 |
| 工具过多难维护 | 4 | 质量下降 | 先 5–8 个高质量工具 |
| LLM 编造数字 | 5 | 报告不可信 | 证据账本 + 数字校验 |
| 权限绕过 | 6 | 数据泄露 | 四层检查 + RLS |
| 评测过拟合 | 7 | 虚假指标 | 隐藏集 + 人工复核 |
| provider 延迟波动 | 9 | 真实调用超时/不稳定 | 合理超时、重试、预算和 `llm_timeout` 明示；多轮统计 |
| 评测器误报 | 9 | 正确回答被判失败 | 关系/数字解析回归测试；真实复测校验 |
| 密钥轮换未确认 | 9 | 凭据风险 | 提供方控制台轮换后再继续真实调用/部署 |
| 范围蔓延 | 全程 | 延期 | 阶段门禁 + 非目标清单 |

---

## 6. 建议的开发顺序（第一周）

1. 跑通现有测试与数据生成；
2. 完成 P1.5-01 / P1.5-02（24 个月 + 华东因果链）；
3. 写事件断言，确认 Agent 将来能“查出来”；
4. 开始 P1.5-04 指标 v2；
5. 每周保持一个可演示的端到端切片。

> 不要先写 Agent Prompt。先把数据和指标做对，否则 Agent 再聪明也只能在错误的数据上编故事。
