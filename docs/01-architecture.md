# 01 — 总体架构设计

> 本文档定义目标架构、模块边界、技术选型和核心契约。当前实现已覆盖数据层、语义查询、受控 SQL、单 Agent、证据链、运行时持久化、可观测性与 Docker 部署；文中标注 ⬜ 的是当时尚未建设、现已由后续阶段补齐或仍未实现的条目。最新实现状态以 [PROJECT_DEVELOPMENT.md](PROJECT_DEVELOPMENT.md) 与根目录 [README.md](../README.md) 为准。

---

## 1. 设计目标与非目标

### 1.1 目标

系统要回答的不是“执行一条 SQL”，而是：

> **一个经营问题 → 自主确定调查范围 → 调用跨部门工具 → 形成证据链 → 定位主要原因 → 给出可验证的经营建议。**

必须具备：

- 统一的指标口径，避免“利润”“毛利”“ROI”各说各话；
- 确定性的数据分析能力（环比、同比、贡献、排名、趋势、异常）；
- Agent 根据当前观察结果自主决定下一步调查什么；
- 每个结论都能追溯到数据证据；
- SQL 与权限边界可控；
- 评测与可观测性，能解释一次分析为什么成功或失败。

### 1.2 非目标

当前阶段不做：

- 五个部门各一个 Agent，或 Supervisor + 多 Agent 编排；
- 自动执行经营决策（下单、调价、裁员、投放）；
- 直接连接生产 ERP / CRM / 广告平台；
- 复杂微服务、Kafka、K8s；
- 用 RAG 替代业务数据库；
- 让 LLM 直接持有数据库写权限；
- 为了简历而强行引入 MCP。

---

## 2. 架构原则

| 原则 | 含义 | 落地方式 |
| --- | --- | --- |
| Semantic-first | 先用指标口径解决，再考虑自由 SQL | Metric Resolver + Query Compiler |
| Deterministic numbers | 数字来自数据库或确定性计算 | 工具返回结构化值；报告只引用证据 |
| Evidence-first | 结论必须有证据 | Evidence Ledger + 数字校验 |
| Tool-first | Agent 只通过工具访问能力 | Tool Registry + Pydantic 契约 |
| Least privilege | LLM / Agent 不持有写权限 | 只读账号 + Guard + RBAC |
| Single agent | 一个统一 Agent 动态选工具 | LangGraph 单图 + 条件边 |
| Bounded autonomy | 自主但有预算和停止条件 | max_iterations / max_tool_calls / 停止原因 |
| Progressive delivery | 每阶段可独立验收 | Phase gate + 测试 + 演示问题 |

---

## 3. 总体架构

```text
┌────────────────────────────────────────────────────────────────────┐
│                            Client / UI                             │
│                 （当前：CLI / HTTP；后续：看板 / 对话）              │
└───────────────────────────────┬────────────────────────────────────┘
                                │ HTTPS / SSE
┌───────────────────────────────▼────────────────────────────────────┐
│                         FastAPI API Layer                          │
│  auth · run lifecycle · SSE progress · report / evidence query     │
└───────────────────────────────┬────────────────────────────────────┘
                                │
┌───────────────────────────────▼────────────────────────────────────┐
│                        Agent Runtime (LangGraph)                   │
│                                                                    │
│  Understand → Plan → Select Action → Execute Tool → Observe        │
│       ↑                                      │                     │
│       └────────── Decide ←───────────────────┘                     │
│                     │                                              │
│         ┌───────────┼────────────┬──────────────┐                  │
│         ▼           ▼            ▼              ▼                  │
│      Verify      Analyze     Synthesize      Clarify / Finish      │
│                                                                    │
│  State: hypotheses · observations · evidence · budget · confidence │
└───────────────────────────────┬────────────────────────────────────┘
                                │ Tool Registry（权限 / 契约 / 超时）
┌───────────────────────────────▼────────────────────────────────────┐
│                        Capability Layer                            │
│                                                                    │
│  Metric Resolver   Query Compiler   Analysis Engine                │
│  Domain Tools      Knowledge/RAG    SQL Guard + Read-only Executor │
│  Evidence Builder  Report Generator  Number Validator              │
└───────────────────────────────┬────────────────────────────────────┘
                                │
┌───────────────────────────────▼────────────────────────────────────┐
│                           Data Layer                               │
│  PostgreSQL（业务数据 + run/evidence/audit）                        │
│  Semantic Layer（metrics / dimensions / relationships）             │
│  Knowledge Base（指标定义、业务规则、历史案例；可选 pgvector）        │
└───────────────────────────────┬────────────────────────────────────┘
                                │
┌───────────────────────────────▼────────────────────────────────────┐
│                     Observability & Evaluation                     │
│  OpenTelemetry / Langfuse · structured logs · eval harness · CI     │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 关键边界

- **API 层不包含业务推理。** 只负责鉴权、创建 run、读取状态、推送事件。
- **Agent Runtime 不直接访问数据库。** 只能通过 Tool Registry。
- **Tool 不拼接自然语言 SQL。** 优先走 Metric Resolver / Query Compiler；只有 `run_sql` 工具允许候选 SQL，且必须经过 Guard。
- **Evidence Ledger 是唯一数字来源。** 报告生成器不能自己算数，也不能让 LLM 自由生成数字。
- **Guard 是执行前的最后一道关。** 不是 Prompt 里的“请不要写 DELETE”。

---

## 4. 分层职责

### 4.1 API Layer

| 职责 | 说明 |
| --- | --- |
| 鉴权与用户上下文 | 解析 user_id、role、允许的业务域 |
| Run 生命周期 | 创建、查询、取消、重试 |
| 进度推送 | SSE 推送 step / tool / evidence 事件 |
| 结果读取 | 报告、证据链、执行轨迹 |
| 参数校验 | Pydantic 请求模型、预算上限 |

不做：指标解析、SQL 生成、业务归因。

### 4.2 Agent Runtime

| 组件 | 职责 |
| --- | --- |
| State | 保存问题、目标、假设、观察、证据、预算、置信度、停止原因 |
| Graph | 定义节点与条件边；单 Agent，不是多 Agent |
| Nodes | understand / plan / select_action / execute_tool / observe / decide / verify / analyze / synthesize / validate / clarify / finish |
| Policies | 最大迭代、工具调用上限、token 预算、必须验证的规则 |
| Prompts | 角色、可用工具、状态摘要、输出 JSON schema、注入防护 |
| Checkpointer | 持久化 run 状态，支持恢复与审计 |

Agent 不直接“思考出数字”。它决定**下一步做什么**，数字由工具返回。

### 4.3 Capability Layer

| 组件 | 职责 | 是否用 LLM |
| --- | --- | --- |
| Metric Resolver | “利润率”→“gross_margin”；同义词、歧义澄清 | Phase 2 规则；Phase 3+ 可加 LLM 兜底 |
| Query Compiler | metric + dimensions + filters → 安全 SQL | 否 |
| Analysis Engine | MoM / QoQ / YoY / Top N / Contribution / Trend / Anomaly | 否 |
| Domain Tools | 销售诊断、营销效率、利润桥、库存风险、人力成本 | 否（内部调用分析引擎） |
| Knowledge / RAG | 指标定义、业务规则、历史案例 | 检索可不用 LLM；总结可用 |
| SQL Guard | 解析、白名单、只读、超时、LIMIT | 否 |
| Evidence Builder | 把 ToolResult 转为 Evidence，维护账本 | 否 |
| Report Generator | 结构化报告 + 叙述 | 是，但数字只能引用证据 |
| Number Validator | 校验报告中每个数字能否追溯 | 否（正则 + 账本匹配） |

### 4.4 Data Layer

| 存储 | 内容 |
| --- | --- |
| PostgreSQL 业务库 | 维度表、事实表、只读视图 |
| PostgreSQL 运行库 | `agent_run`、`agent_step`、`tool_call`、`evidence`、`report`、`audit_log`（可与业务库同实例不同 schema） |
| Semantic Layer | `metrics.json`、`dimensions.json`、`relationships.json` |
| Knowledge Base | 指标说明、口径规则、历史分析案例；Phase 5+ 可选 pgvector |

### 4.5 Observability & Evaluation

- 结构化日志：每次 run、step、tool call、SQL、LLM 调用都有 ID；
- OpenTelemetry trace：run → step → tool → sql / llm；
- 指标：成功率、p95 延迟、工具错误率、Guard 拦截率、平均迭代数、token / 成本；
- 评测：50–100 题业务问题集，离线跑分，CI 回归。

---

## 5. 一次分析请求的生命周期

以“为什么本季度利润下降？”为例：

```text
1. API 接收问题
   → 创建 run_id，记录 user/role/budget
   → 返回 202 + run_id，SSE 推送进度

2. Understand
   → 识别意图：profit_diagnosis
   → 目标指标：net_profit / gross_profit / gross_margin
   → 业务域：Finance 为主，Sales / Marketing / Supply / HR 待查
   → 时间范围：本季度 vs 上季度 / 去年同期
   → 维度：region / product / department
   → 若“利润”有歧义（毛利 vs 净利），最多澄清一次

3. Plan
   → H1: 收入下降导致利润下降
   → H2: 成本上升导致利润下降
   → H3: 营销效率下降
   → H4: 人力成本上升
   → H5: 供应链成本上升
   → 初始调查顺序：Finance → Sales → Marketing → Supply → HR

4. Select Action
   → LLM 从可用工具中选择：finance_profit_bridge
   → 规则校验：工具存在、参数合法、有权限、预算足够

5. Execute Tool
   → Query Compiler 生成安全 SQL
   → 只读执行，返回结构化结果
   → 生成 Evidence E001、E002...

6. Observe
   → 更新假设状态：H1 支持 / H2 部分支持
   → 发现收入下降主要来自华东
   → 决定继续调查 Sales

7. Decide
   → 选择 sales_diagnose，按 region / product / channel 拆解
   → 发现华东销量下降，核心商品库存异常
   → 继续 supply_risk、marketing_efficiency、hr_cost_diagnose

8. Verify
   → 对关键证据做二次计算 / 交叉验证
   → 检查是否存在冲突：销售额 vs 财务收入是否对得上

9. Analyze
   → 计算贡献：收入下降 45%、营销效率 20%、人力成本 15%、其他 20%
   → 所有百分比由确定性算法产生，不由 LLM 编造

10. Synthesize
    → 生成 Finding，每个绑定 Evidence IDs
    → 生成报告骨架，LLM 只写叙述
    → 数字校验：所有数字必须能在 Evidence Ledger 中找到

11. Validate
    → 证据覆盖、数字可追溯、置信度、限制说明
    → 不通过则重试或降级为模板报告

12. Finish
    → 返回报告、证据链、执行轨迹、成本与耗时
```

---

## 6. 技术选型

| 领域 | 选择 | 理由 | 备选 | 引入阶段 |
| --- | --- | --- | --- | --- |
| 语言 | Python 3.11+ | 数据 / LLM 生态成熟；现有代码基线 | — | 已完成 |
| API | FastAPI | Pydantic 原生、异步、SSE 方便 | Flask | Phase 2 |
| 数据模型 | SQLAlchemy 2 + Alembic | 现有实现；迁移可控 | Django ORM | 已完成 |
| 数据库 | PostgreSQL 16 | 窗口函数、CTE、RLS、JSONB | SQLite（本地开发） | 已完成 |
| 校验 | Pydantic v2 | 工具契约、API 契约、LLM 结构化输出 | dataclasses | 已完成 |
| Agent 编排 | LangGraph | 显式状态图、条件边、checkpoint、human-in-the-loop | 自研状态机 / OpenAI Agents SDK | Phase 4 |
| SQL 解析 | sqlglot | AST 级校验、方言支持、可读性好 | sqlparse（太弱） | Phase 3 |
| 分析计算 | SQL + Python | 聚合下推数据库；复杂归因用 Python | pandas 全量加载（不推荐） | Phase 2 |
| LLM 接入 | 统一 `LLMClient` 接口，单供应商起步 | 避免锁死；结构化输出 | LiteLLM（可选） | Phase 3 |
| 知识检索 | 先用语义层 JSON / 规则表；需要时 pgvector | 避免过早引入向量库 | Chroma / Milvus | Phase 5 |
| 可观测性 | 结构化日志 → OTel → Langfuse / Phoenix | 渐进式，不阻塞前期 | 只上 Langfuse | Phase 7 |
| 评测 | pytest + 自研 harness；可选 DeepEval | 业务指标需要自定义 | Ragas（偏 RAG） | Phase 3 起 |
| 前端 | 暂不做；后续 Streamlit / Next.js | 先把后端能力做对 | — | Phase 8 |
| 部署 | 本地 + Docker Compose；后续单机容器 | 当前规模不需要 K8s | — | Phase 8 |

### 6.1 为什么选 LangGraph

- 需要显式状态和条件边：`decide → 继续调查 / 验证 / 分析 / 结束`；
- 需要 checkpoint：中断恢复、审计、human-in-the-loop；
- 需要流式事件：SSE 推送 step / tool；
- 单图即可，不需要多 Agent。

不选多 Agent 框架的原因：五个业务域是**工具维度**，不是**Agent 维度**。多 Agent 会增加路由、上下文同步、冲突合并的成本，而收益不明确。

### 6.2 为什么 Semantic-first

- Text-to-SQL 在“利润率”这类口径上极易出错；
- 指标口径可以测试、版本化、审计；
- 大多数经营问题可以用“指标 + 维度 + 时间 + 对比”表达；
- 自由 SQL 只作为兜底，且必须 Guard。

---

## 7. 目标目录结构

保留当前 `src/` 布局；需求文档中的 `backend/` 在 Python 项目中对应 `src/enterprise_business_agent/`，不额外造一层 `backend/`。

```text
enterprise_business_agent/
├── docs/                              # 本文档集
├── src/enterprise_business_agent/
│   ├── config.py                      # ✅ 现有
│   ├── db.py                          # ✅ 现有
│   ├── models.py                      # ✅ 现有
│   ├── schemas.py                     # ✅ 现有（Phase 1 契约）
│   ├── data_generator.py              # ✅ 现有
│   ├── quality.py                     # ✅ 现有
│   ├── semantic.py                    # ✅ 现有（v1 loader）
│   │
│   ├── semantic/                      # ⬜ Phase 2
│   │   ├── models.py                  # MetricDefinition v2 / Dimension / Relationship
│   │   ├── loader.py                  # 加载与校验 JSON
│   │   ├── resolver.py                # 同义词 / 模糊 / 澄清
│   │   ├── compiler.py                # metric + dims + filters → SQL
│   │   └── registry.py                # 指标 / 维度注册表
│   │
│   ├── query/                         # ⬜ Phase 2
│   │   ├── executor.py                # 只读执行、超时、行数上限
│   │   └── result.py                  # 结构化结果
│   │
│   ├── analysis/                      # ⬜ Phase 2
│   │   ├── comparison.py              # MoM / QoQ / YoY
│   │   ├── contribution.py            # 加性指标贡献
│   │   ├── ranking.py                 # Top N / Bottom N
│   │   ├── trend.py                   # 趋势 / 变点
│   │   ├── anomaly.py                 # 稳健 z-score
│   │   └── decomposition.py           # 比率指标桥（Phase 5）
│   │
│   ├── tools/                         # ⬜ Phase 4
│   │   ├── base.py                    # ToolSpec / ToolResult / 错误模型
│   │   ├── registry.py                # 注册、权限、超时、审计
│   │   ├── metric_tools.py            # list/describe/query metric
│   │   ├── analysis_tools.py          # compare/rank/contribution/trend
│   │   ├── domain_tools.py            # sales/marketing/finance/supply/hr
│   │   ├── knowledge_tools.py         # 指标定义、规则、案例
│   │   └── sql_tool.py                # Guard 后的兜底 SQL
│   │
│   ├── sql/                           # ⬜ Phase 3
│   │   ├── guard.py                   # AST 校验、白名单、LIMIT
│   │   ├── validator.py               # 禁止语句 / 函数 / 系统表
│   │   └── executor.py                # 只读执行、statement_timeout
│   │
│   ├── agent/                         # ⬜ Phase 4
│   │   ├── state.py                   # AnalysisState
│   │   ├── graph.py                   # StateGraph
│   │   ├── policies.py                # 预算、停止条件
│   │   ├── nodes/                     # understand/plan/act/observe/decide/...
│   │   └── prompts/                   # 系统提示、决策提示、报告提示
│   │
│   ├── evidence/                      # ⬜ Phase 5
│   │   ├── models.py                  # Evidence / Finding / Report
│   │   ├── ledger.py                  # 证据账本
│   │   ├── verifier.py                # 二次计算 / 冲突检测
│   │   └── report.py                  # 报告骨架 + 叙述
│   │
│   ├── llm/                           # ⬜ Phase 3
│   │   ├── client.py                  # LLMClient 接口 + 单供应商实现
│   │   └── schemas.py                 # 结构化输出模型
│   │
│   ├── security/                      # ⬜ Phase 6
│   │   ├── permissions.py             # RBAC / 行列权限
│   │   └── audit.py                   # 审计日志
│   │
│   ├── observability/                 # ⬜ Phase 7
│   │   ├── tracing.py                 # OTel spans
│   │   └── logging.py                 # 结构化日志
│   │
│   └── api/                           # ⬜ Phase 2 起
│       ├── app.py
│       ├── deps.py
│       └── routes/
│           ├── runs.py
│           ├── metrics.py
│           └── health.py
│
├── semantic_layer/
│   ├── metrics.json                   # ✅ 现有 v1；Phase 1.5 升 v2
│   ├── dimensions.json                # ⬜ Phase 1.5
│   └── relationships.json             # ⬜ Phase 1.5
│
├── data/                              # ⬜ Phase 1.5：快照 / 评测样本 / 导出
├── alembic/                           # ✅ 现有迁移目录（不重命名为 migrations/）
├── scripts/                           # ✅ 现有 CLI；新增 validate_data.py
├── sql/schema.sql                     # ✅ 现有
├── tests/                             # ✅ 现有
│   ├── unit/
│   ├── integration/
│   ├── evals/
│   └── fixtures/
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
└── README.md
```

> 目录演进策略：新增模块用新包，不重写现有 Phase 1 文件；`models.py` / `quality.py` / `semantic.py` 保持兼容，逐步把职责迁移到 `semantic/` 等子包。

---

## 8. API 设计

### 8.1 Endpoints

| 方法 | 路径 | 用途 | 状态 |
| --- | --- | --- | --- |
| GET | `/health` | 健康检查 | ✅ 已实现 |
| POST | `/api/v1/analysis/natural-language` | 语义优先自然语言分析 | ✅ 已实现 |
| POST | `/api/v1/metrics/query` | 结构化指标查询（不经 Agent） | ✅ 已实现 |
| GET | `/api/v1/analysis/results/{request_id}` | 读取分析结果（按 principal 隔离） | ✅ 已实现 |
| POST | `/api/v1/agent/runs` | 创建一次同步 Agent 分析 | ✅ 已实现（无持久化） |
| GET | `/api/v1/agent/runs/{run_id}` | 查询已完成的 Agent run | ✅ 已实现（内存存储） |
| GET | `/api/v1/agent/runs/{run_id}/events` | SSE 进度流 | ⬜ 未实现 |
| GET | `/api/v1/agent/runs/{run_id}/evidence` | 完整证据账本 | ⬜ Phase 5 |
| POST | `/api/v1/agent/runs/{run_id}/cancel` | 取消 | ⬜ 未实现 |

### 8.2 创建分析

```http
POST /api/v1/runs
Content-Type: application/json

{
  "question": "为什么本季度利润下降？",
  "conversation_id": "conv_2025_001",
  "options": {
    "max_iterations": 8,
    "max_tool_calls": 12,
    "language": "zh-CN"
  }
}
```

```json
{
  "run_id": "run_01J...",
  "status": "queued",
  "created_at": "2026-10-28T10:00:00Z"
}
```

### 8.3 SSE 事件

```text
event: run.started
data: {"run_id":"run_01J...","question":"..."}

event: step.started
data: {"step":1,"node":"understand"}

event: tool.called
data: {"tool_call_id":"tc_001","tool":"finance_profit_bridge","args":{...}}

event: tool.completed
data: {"tool_call_id":"tc_001","ok":true,"rows":8,"latency_ms":42}

event: evidence.added
data: {"evidence_id":"E001","metric":"net_profit","value":-1200000}

event: finding.updated
data: {"finding_id":"F1","statement":"收入下降是利润下降的首要因素","confidence":0.82}

event: report.ready
data: {"run_id":"run_01J...","confidence":0.78}

event: run.failed
data: {"run_id":"run_01J...","error_code":"TOOL_TIMEOUT"}
```

### 8.4 错误模型

```json
{
  "error": {
    "code": "PERMISSION_DENIED",
    "message": "当前用户无权访问 HR.labor_cost",
    "retryable": false,
    "details": {"metric": "labor_cost", "role": "sales_manager"}
  }
}
```

错误码至少包括：

- `INVALID_REQUEST`
- `PERMISSION_DENIED`
- `METRIC_NOT_FOUND`
- `AMBIGUOUS_METRIC`
- `SQL_GUARD_REJECTED`
- `SQL_EXECUTION_FAILED`
- `TOOL_TIMEOUT`
- `BUDGET_EXCEEDED`
- `INSUFFICIENT_DATA`
- `EVIDENCE_VALIDATION_FAILED`
- `LLM_ERROR`
- `RUN_CANCELLED`

---

## 9. 核心数据契约

> 以下为设计契约，代码实现时放在 `schemas.py` / `agent/state.py` / `evidence/models.py`，并加版本号。

### 9.1 AnalysisRequest

```python
class AnalysisOptions(BaseModel):
    max_iterations: int = Field(default=8, ge=1, le=20)
    max_tool_calls: int = Field(default=12, ge=1, le=30)
    max_tokens: int = Field(default=60_000, ge=1_000)
    language: str = "zh-CN"
    allow_sql_fallback: bool = False
    require_verification: bool = True

class AnalysisRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    conversation_id: str | None = None
    options: AnalysisOptions = AnalysisOptions()
```

### 9.2 AnalysisState

```python
class AnalysisState(BaseModel):
    run_id: str
    conversation_id: str | None
    user_id: str
    role: str
    allowed_domains: list[str]

    user_question: str
    analysis_goal: str | None
    time_range: TimeRange | None
    domains: list[str] = []
    metrics: list[str] = []
    dimensions: list[str] = []

    hypotheses: list[Hypothesis] = []
    plan: list[PlanStep] = []
    current_step: int = 0

    observations: list[Observation] = []
    tool_results: list[ToolResult] = []
    evidence_ids: list[str] = []
    findings: list[Finding] = []
    conflicts: list[Conflict] = []

    confidence: float = 0.0
    iteration: int = 0
    tool_calls: int = 0
    tokens_used: int = 0
    next_action: NextAction | None = None
    stop_reason: str | None = None
```

### 9.3 ToolResult

```python
class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict = {}

class ToolResult(BaseModel):
    tool_call_id: str
    tool: str
    ok: bool
    data: dict
    rows_returned: int = 0
    evidence_drafts: list[EvidenceDraft] = []
    error: ToolError | None = None
    latency_ms: int
```

### 9.4 Evidence / Finding / Report

```python
class Evidence(BaseModel):
    evidence_id: str
    run_id: str
    claim: str
    metric: str
    value: float | int | None
    unit: str
    period: str | None
    comparison: Comparison | None
    dimensions: dict[str, str] = {}
    source_table: str
    source_columns: list[str]
    filters: dict = {}
    calculation: str
    tool_call_id: str
    query_hash: str | None
    confidence: float = 1.0
    created_at: datetime

class Finding(BaseModel):
    finding_id: str
    statement: str
    domain: str
    severity: Literal["primary", "secondary", "contributing", "context"]
    evidence_ids: list[str]
    contribution_pct: float | None
    confidence: float
    counter_evidence_ids: list[str] = []

class AnalysisReport(BaseModel):
    run_id: str
    question: str
    executive_summary: str
    conclusion: str
    findings: list[Finding]
    contribution_breakdown: list[ContributionItem]
    recommendations: list[Recommendation]
    risks: list[str]
    limitations: list[str]
    confidence: float
    evidence: list[Evidence]
    generated_at: datetime
```

### 9.5 契约演进规则

- 所有对外契约带 `schema_version`；
- 新增字段必须可空或有默认值；
- 删除/重命名字段需要迁移说明；
- Tool 输入输出 schema 与 API schema 分开，不共用“万能 dict”；
- 契约变更必须同步测试和文档。

---

## 10. 部署与运行形态

### 10.1 本地开发

```text
SQLite / Docker PostgreSQL
  + Python venv
  + uvicorn（Phase 2 起）
  + 可选 Langfuse 本地容器（Phase 7）
```

### 10.2 演示 / 预发

```text
Docker Compose:
  postgres
  api (FastAPI + uvicorn)
  (可选) langfuse / phoenix
  (可选) frontend
```

### 10.3 生产演进（远期）

- API 与 Agent Worker 分离；
- PostgreSQL 主从 / 备份；
- 只读副本供分析查询；
- 密钥管理（环境变量 / Secret Manager）；
- 审计日志归档；
- 限流与配额；
- 仍不引入 K8s，直到有明确规模需求。

---

## 11. 阶段边界

| 能力 | 最早引入阶段 | 原因 |
| --- | --- | --- |
| FastAPI | Phase 2 | 先把查询引擎做成可调用服务 |
| LLM | Phase 3 | 先把确定性指标查询做对 |
| 自由 SQL | Phase 3 | 必须同时有 Guard |
| LangGraph | Phase 4 | 需要状态、条件边、checkpoint |
| Evidence Chain | Phase 5 | 工具结果稳定后再建账本 |
| RBAC | Phase 6 | 先有工具和 run，再做权限矩阵 |
| OTel / Langfuse | Phase 7 | 先有稳定的 trace 结构 |
| MCP | Phase 8 | 先有内部 Tool Registry |
| 前端 | Phase 8 | 先验证后端分析质量 |

---

## 12. 风险与权衡

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 语义层口径漂移 | 同一问题不同答案 | 指标版本化、口径测试、单一 canonical source |
| LLM 编造数字 | 报告不可信 | Evidence Ledger + Number Validator + 模板兜底 |
| SQL Guard 被绕过 | 数据泄露 / 破坏 | AST + 白名单 + 只读账号 + 超时 + 审计 |
| Agent 过度调查 | 延迟 / 成本高 | 预算、停止条件、无新信息检测 |
| 模拟数据过于理想 | 评测虚高 | 加入噪声、缺失、冲突、超范围问题 |
| 多 Agent 复杂度 | 调试困难 | 坚持单 Agent + 工具 |
| 过早引入 RAG | 维护成本 | 先用语义层 JSON + 规则表 |
| 文档与实现脱节 | 面试/交付风险 | 每阶段更新文档状态标记 |

---

## 13. 架构验收标准

- [ ] 每个模块职责单一，依赖方向清晰（api → agent → tools → semantic/query → db）；
- [ ] Agent 不直接 import 数据库 session，只能通过 Tool；
- [ ] Tool 不直接拼接用户输入到 SQL；
- [ ] 所有对外契约有 Pydantic 模型和版本；
- [ ] 任一结论可通过 `evidence_id` 追溯到 `tool_call_id` 和 SQL / 计算；
- [ ] Guard 有单元测试和对抗样例；
- [ ] 一次 run 的完整 trace 可回放；
- [ ] 文档中的“已实现 / 计划中”与代码一致。
