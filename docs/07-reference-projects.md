# 07 — GitHub 参考项目与借鉴点

> 本文档列出与本项目相关的开源项目和工程实践，说明**借鉴什么、不借鉴什么、什么时候引入**。项目状态、许可证和维护活跃度会变化，**引入依赖前必须核对最新 LICENSE、release 和 issue 状态**；本文不替代尽调。

---

## 1. 使用原则

1. **参考设计，不直接 fork 平台。** 这些项目大多比本项目大得多，直接 fork 会带来无法维护的复杂度。
2. **只借用解决当前阶段问题的部分。** 例如 Phase 2 只需要语义层和查询编译器，不需要 Agent。
3. **优先标准协议。** SQL 用 sqlglot，Agent 用 LangGraph，观测用 OpenTelemetry，避免自造不兼容格式。
4. **许可证先看。** 特别是 AGPL/SSPL/商业限制条款；不确定时不要复制代码。
5. **不为了简历引入技术。** MCP、向量库、多 Agent 都在明确需要时才加。

---

## 2. 参考项目总览

| 项目 | 类型 | 最值得借鉴 | 本项目采用阶段 |
| --- | --- | --- | --- |
| Cube | Headless BI / Semantic Layer | 指标、维度、关系、访问策略的建模方式 | Phase 2（设计参考） |
| dbt Semantic Layer / MetricFlow | 指标语义层 | 指标定义、时间粒度、可加性、派生指标 | Phase 2（设计参考） |
| WrenAI | GenBI / Text-to-SQL | 语义模型（MDL）、问题-SQL 对、UI 流程 | Phase 2–3（设计参考） |
| Vanna | RAG Text-to-SQL | schema/文档/SQL 的检索增强、训练样例 | Phase 3（设计参考） |
| Dataherald | 企业 Text-to-SQL | golden SQL、评估器、置信度、few-shot 检索 | Phase 3（设计参考） |
| DB-GPT | 数据 Agent 框架 | 工具抽象、沙箱、可观测、Text2SQL 流程 | Phase 4（设计参考） |
| LangGraph | Agent 编排 | 状态图、条件边、checkpoint、human-in-the-loop | Phase 4（直接采用） |
| sqlglot | SQL 解析 | AST 校验、方言、SQL 重写 | Phase 3（直接采用） |
| Great Expectations | 数据质量 | expectation 概念、检查报告 | Phase 1.5（设计参考） |
| Pandera | DataFrame 校验 | schema 校验、统计断言 | Phase 2（可选） |
| OpenTelemetry | 可观测标准 | trace/span/log 标准 | Phase 7（直接采用） |
| Langfuse | LLM 可观测 | trace、prompt、cost、eval | Phase 7（可选） |
| Arize Phoenix | LLM 可观测 | OTel 原生 trace、eval | Phase 7（可选） |
| DeepEval | LLM 评测 | 断言式评测、指标框架 | Phase 7（可选） |
| Ragas | RAG 评测 | faithfulness / context 指标 | Phase 5+（可选） |
| BIRD | Text-to-SQL 基准 | evidence 概念、评测严谨性 | Phase 3/7（评测参考） |
| Spider | Text-to-SQL 基准 | 复杂 SQL 覆盖 | Phase 3/7（评测参考） |
| pgvector | 向量检索 | 语义层/知识库检索 | Phase 5+（可选） |
| Uber QueryGPT | 工程实践（非开源） | 意图路由、workspace、few-shot | Phase 3–4（产品参考） |

---

## 3. 分类详解

### 3.1 Semantic Layer / BI

#### Cube（`cube-js/cube`）

**是什么：** Headless BI 语义层，用 YAML/JS 定义 cube、measure、dimension、join、pre-aggregation、access policy，通过 SQL API 对外提供指标查询。

**借鉴：**

- 指标/维度/关系的建模结构；
- “度量 + 维度 + 过滤 + 时间粒度”的查询接口；
- 访问策略（row-level / member-level）与语义层结合；
- 预聚合思路（本项目暂不需要）。

**不借鉴：**

- 完整的 pre-aggregation 引擎和 JS 技术栈；
- 为每个查询生成复杂 cube SQL 的运行时；
- 多租户/缓存平台级功能。

**风险：** 引入 Cube 会把项目变成“部署一个 BI 平台”，而不是“做一个分析 Agent”。建议只读它的建模文档和示例，不引入依赖。

#### dbt Semantic Layer / MetricFlow

**是什么：** dbt 生态的指标语义层，强调 metric spec、time grain、dimensions、derived/cumulative metrics、time spine。

**借鉴：**

- 指标定义的结构化与版本化；
- 时间粒度、时间 spine、累计指标的处理；
- 可加性（additive / semi-additive / non-additive）的严谨表达；
- “一个指标一个定义”的治理理念。

**不借鉴：**

- 依赖 dbt 项目结构和编译链；
- 为数据仓库建模服务的复杂度；
- 把语义层绑定到某个 BI 工具。

**对本项目的启示：** `metrics.json` v2 的 `metric_type` / `additivity` / `time_grains` 设计可以直接参考其概念，但用更轻的 JSON + Pydantic 实现。

#### WrenAI（`Canner/WrenAI`）

**是什么：** 开源 GenBI 平台，包含语义模型（MDL）、Text-to-SQL、对话式 BI、图表。

**借鉴：**

- 语义模型如何描述表、列、关系、指标；
- 问题 → 语义模型 → SQL → 结果 → 图表的产品流程；
- 问题-SQL 对（question-SQL pairs）的积累方式；
- 对“业务用户不需要懂 SQL”的交互设计。

**不借鉴：**

- Node/TypeScript 全栈平台；
- 复杂的 UI/图表/部署体系；
- 把所有问题都交给 LLM 生成 SQL（本项目 semantic-first）。

**风险：** 平台功能很多，容易让人误以为“接上就能用”。本项目的差异化是跨部门自主调查和证据链，不是 BI 看板。

#### Lightdash（`lightdash/lightdash`）

**是什么：** 基于 dbt 的开源 BI，强调指标层和自助分析。

**借鉴：** 指标目录、维度探索、权限模型的产品化方式。

**不借鉴：** 与 dbt 强绑定；本项目不引入 dbt。

---

### 3.2 Text-to-SQL / Analytics Agent

#### Vanna（`vanna-ai/vanna`）

**是什么：** Python RAG 框架，用 DDL、文档、历史 SQL 训练，检索相关上下文后让 LLM 生成 SQL；支持多种 LLM 和数据库。

**借鉴：**

- schema / 文档 / SQL 示例的检索增强；
- “训练数据”作为可积累资产；
- 数据库适配器与结果展示；
- 对模糊问题的澄清。

**不借鉴：**

- 以“LLM 直接生成 SQL”为核心路径；
- 把所有业务口径都交给向量检索；
- 缺少强 Guard 的执行方式。

**对本项目的启示：** 可以把 Vanna 的检索思路用于 `run_sql` 兜底和知识库，但主路径仍是 Metric Resolver + Query Compiler。

#### Dataherald（`Dataherald/dataherald`）

**是什么：** 面向企业的开源 Text-to-SQL 引擎，包含数据库扫描、向量存储、golden SQL、SQL 执行、评估器、置信度等。

**借鉴：**

- golden SQL / 问题-SQL 对的资产化；
- 评估器（evaluator）与置信度；
- few-shot 检索与 schema 上下文；
- 企业级 API 设计。

**不借鉴：**

- 以 Text-to-SQL 为唯一入口；
- 平台化的扫描/同步/多数据源功能；
- 复杂的向量库依赖（可后置）。

**风险：** 项目维护状态可能变化；引入前确认版本、依赖和许可证。

#### DB-GPT（`eosphoros-ai/DB-GPT`）

**是什么：** 面向数据库的 Agent 框架，包含多 Agent、AWEL 工作流、RAG、Text2SQL、沙箱、可视化。

**借鉴：**

- 工具/Agent 的抽象方式；
- SQL 沙箱与执行隔离；
- 可观测与调试界面；
- 数据 Agent 的产品形态。

**不借鉴：**

- 多 Agent / Supervisor 架构；
- 重量级工作流引擎；
- 大而全的部署依赖。

**对本项目的启示：** 单 Agent + Tool Registry 更符合当前规模；DB-GPT 的工具抽象和沙箱思路可参考。

#### LangChain SQL Agent（`langchain-ai/langchain`）

**是什么：** LangChain 的 SQL Agent 示例/工具包，提供 SQLDatabase、SQL tool、agent executor 等。

**借鉴：**

- 工具接口、数据库抽象、结果格式化；
- 快速原型的写法；
- 与 LangGraph 的衔接。

**不借鉴：**

- 依赖 Prompt 让 Agent 自由写 SQL；
- 缺少语义层和证据链；
- 老式 AgentExecutor 的黑盒循环。

#### Uber QueryGPT（工程博客，非开源）

**是什么：** Uber 的 Text-to-SQL 产品实践，强调意图分类、workspace、表/列检索、few-shot、用户反馈。

**借鉴：**

- 意图路由（不同问题走不同 workspace）；
- 检索相关表/列而不是塞全量 schema；
- 用户反馈闭环；
- 评估与灰度。

**不借鉴：** 具体内部架构无法验证；不要照搬未公开细节。

---

### 3.3 Agent Runtime

#### LangGraph（`langchain-ai/langgraph`）

**是什么：** 基于状态图的 Agent 编排库，支持条件边、checkpoint、流式、human-in-the-loop、子图。

**借鉴/采用：**

- `StateGraph` 表达“理解 → 计划 → 行动 → 观察 → 决策”；
- 条件边实现“下一步由状态决定”；
- checkpointer 支持恢复/回放；
- 流式事件对接 SSE；
- human-in-the-loop 对接审批。

**不借鉴：**

- 为了用 LangGraph 而把流程写成固定 DAG；
- 多 Agent 子图滥用；
- 把所有逻辑塞进一个巨大节点。

**采用建议：** Phase 4 直接使用；节点保持小而可测，状态用 Pydantic。

#### OpenAI Agents SDK / Pydantic AI

**是什么：** 其他 Agent 框架，强调工具调用、结构化输出、handoff。

**借鉴：** 结构化输出、工具 schema、错误处理。

**不借鉴：** handoff/多 Agent 模式（当前不需要）。

**结论：** 本项目选 LangGraph，其他只作为设计参考。

---

### 3.4 SQL 安全

#### sqlglot（`tobymao/sqlglot`）

**是什么：** 纯 Python SQL 解析/转译库，支持多种方言、AST 遍历/重写。

**采用：**

- 解析候选 SQL；
- 检查语句类型、表、列、函数；
- 重写 LIMIT；
- 生成规范化 SQL / 哈希。

**注意：**

- 解析成功 ≠ 安全；仍需白名单、只读账号、超时；
- 不同方言 AST 有差异，固定 `dialect="postgres"`；
- 对无法解析的 SQL 直接拒绝（deny by default）。

#### sqlparse

**是什么：** 轻量 SQL 格式化/分词库。

**结论：** 不足以做安全校验（不理解 AST），仅可用于格式化。

---

### 3.5 数据质量

#### Great Expectations（`great-expectations/great_expectations`）

**是什么：** 数据质量框架，用 expectation 描述数据断言，生成数据文档和检查报告。

**借鉴：**

- expectation 的组织方式（表级/列级/跨表）；
- 检查结果报告；
- 数据文档与质量检查结合。

**不借鉴：** 完整平台、复杂配置、云端依赖；本项目实现轻量检查即可。

#### Pandera（`unionai-oss/pandera`）

**是什么：** DataFrame schema/统计校验库。

**借鉴：**

- schema 校验、统计断言；
- 与 pandas 分析结果结合。

**采用建议：** 如果 Phase 2 用 pandas 做贡献/趋势计算，可用 Pandera 校验中间结果；否则不必引入。

---

### 3.6 可观测性

#### OpenTelemetry

**是什么：** 厂商中立的 trace/metrics/logs 标准。

**采用：**

- run/step/tool/sql/llm 的 span 层级；
- 统一属性命名；
- 导出到 console / Langfuse / Phoenix / 其他后端。

**建议：** Phase 7 直接采用，不要自造 trace 格式。

#### Langfuse（`langfuse/langfuse`）

**是什么：** LLM 可观测与评测平台，支持 trace、prompt 管理、成本、数据集、评分。

**借鉴/采用：**

- LLM 调用 trace；
- token/成本统计；
- prompt 版本；
- 人工评分与数据集。

**注意：** 自托管有运维成本；前期可只用结构化日志 + 本地 trace。

#### Arize Phoenix（`Arize-ai/phoenix`）

**是什么：** OTel 原生的 LLM 可观测/评测工具。

**借鉴：** OTel span、LLM eval、可视化。

**结论：** Langfuse 与 Phoenix 二选一即可，不要同时上。

#### OpenMetadata / DataHub（可选）

**是什么：** 数据目录、血缘、治理平台。

**借鉴：** 表/字段/指标的血缘与 ownership；数据字典。

**结论：** 当前规模不需要；数据字典用脚本生成即可。

---

### 3.7 评测

#### DeepEval（`confident-ai/deepeval`）

**是什么：** LLM 评测框架，提供断言式测试和指标（faithfulness、answer relevancy 等）。

**借鉴：**

- 断言式评测的写法；
- 与 pytest/CI 集成；
- 指标组织。

**注意：** 通用指标不能替代业务 golden 值；数字必须确定性校验。

#### Ragas（`explodinggradients/ragas`）

**是什么：** RAG 评测框架，提供 context precision/recall、faithfulness 等。

**借鉴：** 如果 Phase 5+ 引入知识库/RAG，可用其指标评估检索质量。

**不借鉴：** 当前主路径不是 RAG，不要为了用 Ragas 而引入 RAG。

#### BIRD（`bird-bench/BIRD`）

**是什么：** 大规模 Text-to-SQL 基准，强调真实数据库、外部知识证据（evidence）。

**借鉴：**

- “evidence” 作为 SQL 生成/评估的一部分；
- 评测的严谨性（执行结果对比、难度分层）；
- 真实 schema 的复杂性。

**不直接使用：** 本项目是模拟企业数据，不需要跑 BIRD 榜单；但可以借用其“证据”理念。

#### Spider（`taoyds/spider`）

**是什么：** 经典跨域 Text-to-SQL 基准。

**借鉴：** 复杂 SQL 覆盖、schema 泛化评测。

**不直接使用：** 与业务场景差距大。

---

### 3.8 知识 / RAG

#### pgvector（`pgvector/pgvector`）

**是什么：** PostgreSQL 向量扩展。

**采用建议：**

- Phase 5+ 如果知识库超过几十条，再引入；
- 指标定义、规则、历史案例用向量检索；
- 不把业务数据放进向量库替代 SQL。

**不采用：** 单独部署 Chroma/Milvus 等向量数据库（当前规模不需要）。

#### LlamaIndex / Haystack（可选）

**是什么：** RAG/检索框架。

**结论：** 当前不需要；语义层 JSON + 规则表足够。引入前先证明检索是瓶颈。

---

## 4. 综合出的架构模式

从这些项目可以提炼出几条共识：

### 4.1 Semantic-first 优于 Text-to-SQL-first

- Cube / MetricFlow / WrenAI 都强调指标和维度的统一定义；
- Vanna / Dataherald 解决“LLM 写 SQL”，但企业口径问题仍然存在；
- 本项目的差异化是：**先用语义层把能确定的问题确定下来，再让 LLM 处理长尾。**

### 4.2 Golden SQL / 评测资产化

- Dataherald 的 golden SQL、BIRD 的 evidence、WrenAI 的 question-SQL pairs；
- 本项目应维护：
  - `tests/evals/questions.jsonl`（问题 + golden values + 期望工具）；
  - `tests/evals/text2sql.jsonl`（问题 + golden SQL）；
  - 每次失败都沉淀为回归题。

### 4.3 SQL Guard 必须是 AST + 权限，不是 Prompt

- sqlglot 提供 AST；
- 只读账号提供最后防线；
- 白名单限制表/列；
- Prompt 只是第一层，不是安全边界。

### 4.4 Agent 用状态图，不用黑盒循环

- LangGraph 的条件边、checkpoint、流式；
- 状态可序列化、可回放；
- 节点职责单一；
- 预算/停止条件在代码里，不在 Prompt 里。

### 4.5 证据链是差异化，不是附属功能

- 大多数 Text-to-SQL 项目停在“SQL + 结果”；
- 本项目的价值是 Finding → Evidence → Source / Query / Calculation；
- 数字校验器是必需组件，不是可选项。

### 4.6 可观测性从第一天设计

- OTel span 结构先定好；
- 即使 Phase 7 才接后端，Phase 4 就要有 run/step/tool ID；
- 否则后期补 trace 会改大量代码。

---

## 5. 明确不借鉴的模式

| 模式 | 来源 | 为什么不适合 |
| --- | --- | --- |
| 多 Agent / Supervisor | DB-GPT、部分 Agent 框架 | 五个域是工具维度，不是 Agent 维度；复杂度和调试成本高 |
| 全量 schema 塞 Prompt | 早期 Text-to-SQL | 上下文浪费、易混淆；应按指标/问题检索相关表 |
| 只靠 Prompt 防危险 SQL | LangChain SQL Agent 示例 | 易绕过；必须 AST + 只读账号 |
| 把向量库当数据库 | 部分 RAG 项目 | 业务事实必须来自结构化数据库 |
| 直接 fork 完整 BI 平台 | WrenAI、Cube | 项目会变成平台运维，偏离 Agent 目标 |
| 为了 MCP 而 MCP | 部分教程 | 先有稳定 Tool Registry，再考虑 MCP |
| LLM 自由计算数字 | 多数 ChatBI | 不可追溯；必须确定性计算 + 证据 |
| 用 LLM Judge 评数字 | 部分评测项目 | 不稳定；数字用 golden 值校验 |

---

## 6. 采纳顺序

### 第一阶段（Phase 1.5–2）

1. 读 Cube / MetricFlow / WrenAI 的语义层建模文档；
2. 设计 `metrics.json` v2、`dimensions.json`、`relationships.json`；
3. 参考 Great Expectations 的 expectation 思路，落地轻量质量检查；
4. 不引入任何重依赖。

### 第二阶段（Phase 3）

1. 引入 `sqlglot`；
2. 实现 SQL Guard；
3. 参考 Dataherald / Vanna 的 golden SQL 与 few-shot 检索；
4. 建立 Text-to-SQL 评测集。

### 第三阶段（Phase 4）

1. 引入 `langgraph`；
2. 实现 StateGraph + Tool Registry；
3. 参考 DB-GPT 的工具抽象，但不引入多 Agent；
4. 建立 run/step/tool 持久化。

### 第四阶段（Phase 5–7）

1. 证据账本 + 数字校验；
2. 需要时引入 pgvector；
3. 引入 OpenTelemetry；
4. 可选 Langfuse / Phoenix；
5. 参考 DeepEval / Ragas / BIRD 建立评测；
6. CI 分层。

### 第五阶段（Phase 8）

1. Tool Registry → MCP adapter；
2. 前端；
3. 生产部署。

---

## 7. 依赖引入检查清单

引入任何开源项目前，逐项确认：

- [ ] LICENSE 是否允许商业/闭源使用；
- [ ] 是否有 AGPL/SSPL 等传染性条款；
- [ ] 最近 6 个月是否有 release / commit；
- [ ] issue 响应情况；
- [ ] Python 版本 / 依赖冲突；
- [ ] 是否引入额外基础设施（数据库、消息队列、容器）；
- [ ] 是否可以用更小的库替代；
- [ ] 是否有安全漏洞（CVE / 依赖扫描）；
- [ ] 是否会把项目变成“平台运维”；
- [ ] 是否有清晰的退出方案（不锁定）。

---

## 8. 与本项目阶段的对应表

| 本项目阶段 | 主要参考 | 直接采用 |
| --- | --- | --- |
| Phase 1.5 数据/指标 | Cube, MetricFlow, WrenAI, Great Expectations | 无（设计参考） |
| Phase 2 语义查询 | MetricFlow, Cube, WrenAI | SQLAlchemy, Pydantic |
| Phase 3 Text-to-SQL | Vanna, Dataherald, sqlglot | sqlglot |
| Phase 4 Agent | LangGraph, DB-GPT | LangGraph |
| Phase 5 证据/报告 | BIRD evidence, Dataherald evaluator | 无（自研） |
| Phase 6 权限/审计 | Cube access policy, OpenMetadata | 无（自研） |
| Phase 7 评测/观测 | OTel, Langfuse/Phoenix, DeepEval, BIRD | OTel |
| Phase 8 扩展 | MCP 规范, WrenAI UI | 按需 |

---

## 9. 结论

最值得借鉴的不是某个项目的代码，而是三条工程共识：

1. **语义层是可信分析的前提**（Cube / MetricFlow / WrenAI）；
2. **SQL 安全必须靠 AST + 权限，不靠 Prompt**（sqlglot + 只读账号）；
3. **Agent 要用显式状态和证据链，而不是黑盒循环**（LangGraph + 自研 Evidence Ledger）。

本项目的差异化不在“Text-to-SQL”，而在：

> **跨部门自主调查 + 可追溯证据链 + 有数据支撑的经营建议。**
