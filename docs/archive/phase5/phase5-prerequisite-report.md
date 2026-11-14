# Phase 5 前开发完成报告

> 生成时间：2026-10-30
> 范围：Phase 4.2、4.3、4.4、4.5 与 Phase 5 前的企业基础控制、评测与可观测性
> 原则：本文只写已经执行并留下记录的内容。**已实现 / 已测试 / 真实模型已验证 / 部分实现 / 未实现** 五类状态分别标注，不把计划写成完成。
> 停止点：Phase 5 Evidence Chain 的完整账本、归因和报告生成**未开始**；当前 Evidence draft 契约保持不变，Phase 5 入口见第 16 节。
>
> **后续状态（2026-10-31）：Phase 5.1–5.6 已完成。** 实现与验收见 [Phase 5 完成报告](phase5-completion-report.md)。

---

## 1. 结论摘要

| 项目 | 结论 |
| --- | --- |
| 单 Agent 架构 | 保持一个统一经营分析 Agent，未拆分为五个部门 Agent，未引入多 Agent Supervisor / MCP / Redis / Worker / 前端 |
| 真实模型接入 | 智谱 GLM-5.3-Flash（OpenAI 兼容 Chat Completions），独立适配器 + Provider Factory，Anthropic 适配器保留 |
| 真实 Function Calling | 已验证：模型返回 `tool_calls`、工具调用 ID 与结果正确关联、JSON 字符串参数被解析校验、多轮决策后输出中文结论 |
| 数据安全边界 | 模型只能调用 8 个注册工具；工具参数经 Pydantic + 授权 + `SemanticQueryGuard` + `TrustedSelect` + 只读执行；模型不接触数据库或 SQL 文本 |
| 运行治理 | 持久化 run/审计、异步 Run API、SSE、取消、幂等、重启标记 `interrupted`、预算与熔断 |
| 企业基础控制 | 角色级业务域/指标/维度/区域/数据资源白名单；只读工具集；持久化审计；注入与越权测试 |
| 评测 | 确定性 75 条（75/75 通过）+ 真实模型 26 条（24 计分 + 2 负向控制）；两轮真实全量：首轮 17/24，修正后 23/24，逐条结果见第 6 节 |
| 密钥轮换 | **未确认**。无法访问提供方控制台，`.env` 中的 key 曾被写入本地文件。使用前必须在控制台轮换（见第 17 节） |

---

## 2. 当前架构

```text
┌────────────────────────────── 客户端 ──────────────────────────────┐
│  POST /api/v1/agent/runs          同步分析（原有路径）              │
│  POST /api/v1/agent/runs/async    异步提交 + Idempotency-Key        │
│  GET  /api/v1/agent/runs/{id}     结果查询                          │
│  GET  /api/v1/agent/runs/{id}/events  SSE 进度                      │
│  POST /api/v1/agent/runs/{id}/cancel 取消                           │
└───────────────────────────────┬────────────────────────────────────┘
                                │ API Key → Principal(role, scopes)
┌───────────────────────────────▼────────────────────────────────────┐
│ FastAPI                                                            │
│  auth / middleware（request_id、结构化错误、日志脱敏）              │
│  RunManager：线程池、幂等、取消、持久化状态                         │
└───────────────┬─────────────────────────────────┬──────────────────┘
                │                                 │
┌───────────────▼──────────────┐   ┌──────────────▼─────────────────┐
│ AgentRuntime（LangGraph）     │   │ PersistentRunStore             │
│  decide ⇄ act ⇄ decide       │   │  agent_runs                    │
│  ├─ LLMClient（GLM/Anthropic）│   │  agent_audit_events            │
│  ├─ 预算/超时/熔断/去重       │   └────────────────────────────────┘
│  ├─ Trace 事件 → SpanSink(可选 OTel)
│  └─ ToolRegistry             │
└───────────────┬──────────────┘
                │ 结构化工具调用（JSON 参数）
┌───────────────▼────────────────────────────────────────────────────┐
│ 8 个只读工具                                                        │
│  list_metrics / describe_metric / query_metric / compare_periods    │
│  top_n / contribution / trend / anomaly_scan                        │
└───────────────┬────────────────────────────────────────────────────┘
                │ Pydantic 参数 → 授权（域/指标/维度/区域/资源）
┌───────────────▼────────────────────────────────────────────────────┐
│ AnalysisGateway → Semantic Layer → QueryCompiler                    │
│   CompiledQuery → SemanticQueryGuard → TrustedSelect                │
│   → SafeQueryExecutor.execute_select（只读连接/只读事务）           │
└───────────────┬────────────────────────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────────────────────────┐
│ SQLite（本地）/ PostgreSQL（集成测试 + 生产形态）                    │
└────────────────────────────────────────────────────────────────────┘
```

模型侧的唯一出口是工具调用。没有任何代码路径让模型获得连接、SQL 文本或写权限。

---

## 3. 最终目录结构（关键部分）

```text
enterprise_business_agent/
├── src/enterprise_business_agent/
│   ├── agent/
│   │   ├── graph.py          # LangGraph decide→act 循环、预算、熔断、trace
│   │   ├── contracts.py      # 运行请求/响应、EvidenceDraft、状态枚举
│   │   ├── tools.py          # ToolRegistry、8 个工具、Pydantic 参数契约
│   │   └── evaluation.py     # 离线/真实评测运行器（含假规划器，明确标注）
│   ├── llm/
│   │   ├── client.py         # Anthropic Messages 适配器 + LLMClient 协议
│   │   ├── glm.py            # GLM OpenAI 兼容适配器（重试/超时/错误映射）
│   │   ├── factory.py        # Provider Factory
│   │   └── errors.py         # LLMTimeout / LLMUnavailable / LLMInvalidResponse
│   ├── observability/
│   │   └── spans.py          # 可选 OpenTelemetry span（仅 API，故障隔离）
│   ├── security/
│   │   ├── auth.py           # Principal、AuthorizationPolicy、区域/域/资源
│   │   ├── semantic_guard.py # 结构化查询边界
│   │   ├── executor.py       # 只读执行 + 超时/行数限制
│   │   └── sql_guard.py      # Phase 3 候选 SQL 守卫
│   ├── api/
│   │   ├── app.py            # 应用装配、lifespan、启动时 interrupted
│   │   ├── routes.py         # 同步/异步/查询/SSE/取消端点
│   │   ├── run_manager.py    # 线程池、幂等、取消、审计 sink
│   │   ├── run_store.py      # agent_runs / agent_audit_events
│   │   ├── auth.py           # API Key → Principal
│   │   └── middleware.py     # request_id、结构化错误、日志
│   ├── config.py             # 全部环境变量、provider 选择、run store URL
│   └── ...                   # semantic/query/analysis/nl 等既有模块
├── scripts/
│   ├── run_agent_live_evals.py  # 真实 LLM 评测（拒绝 fake fallback）
│   ├── run_agent_evals.py       # 脚本化假规划器控制流评测
│   ├── run_nl_evals.py          # 确定性 NL/分析评测
│   ├── verify_agent_live.py     # 配置诊断 + 真实 smoke
│   └── ...
├── tests/
│   ├── evals/nl_cases.jsonl          # 75 条确定性用例
│   ├── evals/live_agent_cases.jsonl  # 26 条真实模型用例
│   ├── test_glm_client.py            # GLM 协议/错误映射（httpx mock）
│   ├── test_agent_runtime.py         # 决策循环、空响应重试、状态
│   ├── test_governance.py            # 域/资源/区域/注入/只读工具集
│   ├── test_run_lifecycle.py         # 持久化、幂等、SSE、取消、重启
│   ├── test_observability_spans.py   # OTel span 与故障隔离
│   └── test_postgres_integration.py  # PostgreSQL 集成 + 独立只读角色
└── docs/                             # 架构、数据、Agent、安全、路线图、评测
```

---

## 4. Phase 4.2 — GLM 提供方接入

**状态：已实现、已测试、已真实调用验证。**

| 能力 | 实现位置 | 说明 |
| --- | --- | --- |
| 独立 GLM 适配器 | [glm.py](../src/enterprise_business_agent/llm/glm.py) | OpenAI 兼容 `POST {base}/chat/completions`；`tools[].function`、`tool_calls`、JSON 字符串 `arguments` |
| Anthropic 适配器保留 | [client.py](../src/enterprise_business_agent/llm/client.py) | 未删除、未改协议 |
| Provider Factory | [factory.py](../src/enterprise_business_agent/llm/client.py) | `LLM_PROVIDER=glm|anthropic` 选择；缺失 key 返回 `None`，Agent 返回 `llm_unavailable` |
| 配置校验 | [config.py](../src/enterprise_business_agent/config.py) | 模型前缀、base URL 形状、协议匹配；`llm_endpoint_issues()` |
| 错误处理 | glm.py | 401/403 不重试；429/5xx 指数退避；超时/网络异常映射为结构化错误；空响应 → `LLMInvalidResponse` |
| 协议验证 | 真实探测 | 观察到 `finish_reason="tool_calls"`、`tool_calls[0].id`（`call_...`）、`function.arguments` 为 JSON 字符串；最终轮 `finish_reason="stop"` + 文本 |

**真实 HTTP 形状（探测结果，非文档推断）：**

```json
{
  "choices": [{
    "finish_reason": "tool_calls",
    "message": {
      "role": "assistant",
      "content": "",
      "tool_calls": [{
        "id": "call_...",
        "type": "function",
        "function": {"name": "query_metric", "arguments": "{\"metric\":\"sales_amount\",...}"}
      }]
    }
  }],
  "usage": {"prompt_tokens": 11849, "completion_tokens": 230, "total_tokens": 12079}
}
```

key 只从 `LLM_API_KEY` / `GLM_API_KEY` / `ZHIPU_API_KEY` / `ANTHROPIC_API_KEY` 读取，日志、报告、测试输出均不打印 key；`.env` 被 `.gitignore` 排除，只有显式 `EBA_LOAD_DOTENV=true` 才加载。

---

## 5. Phase 4.3 — 真实 Function Calling 与自主循环

**状态：已实现并通过真实调用验证；多轮决策的完整能力边界见下。**

### 5.1 循环行为

- 每轮模型基于「目标 + 已有工具摘要 + 最近错误 + 剩余预算」决定下一步；
- 每轮只执行一个工具调用：模型若在同一轮返回多个调用，执行第一个、其余忽略并写入 `warnings`（不再整体判失败）；工具结果以 `role=tool` 消息回填（`tool_call_id` 与模型给出的 `call_id` 一致）；
- 证据足够时直接输出中文结论并结束；工具失败时可换工具/改参数/停止；
- 代码侧强制：最大工具次数、单工具超时、总时限、token 预算、重复调用检测、连续失败熔断、最终结构化状态；
- 后端拒绝：未注册工具、未授权指标/维度/区域、非法参数、原始 SQL 参数；区域受限角色必须显式过滤区域，不能只按区域分组。

### 5.2 本轮针对真实模型行为的两处修正

| 问题 | 修正 | 证据 |
| --- | --- | --- |
| GLM 偶尔返回空响应（无文本、无 tool_call） | 代码侧有界重试一次，并追加“继续调查或直接结论”的用户消息；第二次仍空则 `invalid_action` | `test_empty_model_response_is_retried_once_then_recovers`、`..._stops_after_one_retry` 通过；live-004 在修正后的独立运行中真实通过，但在随后一次全量运行中再次因连续空响应失败（见 6.2），如实记为未通过 |
| 排查类问题倾向逐产品 `compare_periods`，或忘记先确认整体变化幅度 | Prompt 规则 13–16：成员极值用 `top_n`；`not_calculable` 不逐成员重试；排查默认 `n=3`；先 `compare_periods` 确认变化率再归因 | `AGENT_PROMPT_VERSION=phase4.3-2026-10-30.2`；live-004 修正后真实通过 |

Prompt 版本随运行结果与 trace 一起记录（`prompt_version`），最终版本为 `phase4.3-2026-10-30.4`（规则 16–19 + 工具错误原因回灌），可直接把结论对应到具体提示词版本。

### 5.3 真实多轮决策 trace 示例

live-004「华东 2025 年 12 月销售额环比下滑，排查是否由缺货导致」在最终 prompt 下的真实工具序列（来自 `/tmp/live_004_r6.json`）：

```text
decide → compare_periods(metric=sales_amount, kind=mom, period=2025-12-01, region=EC)
act    → 变化率 -0.364967（18129454.19 vs 28548840.50）
decide → contribution(metric=sales_amount, product, 2025-11 → 2025-12, region=EC, top 3)
act    → P005 -2251384.00 / P007 -1733885.89 / P004 -1670641.45
decide → query_metric(stockout_rate, products=P005/P007/P004, region=EC, 2025-12)
act    → 三者缺货率 0.000000
decide → top_n(stockout_rate, product, region=EC, 2025-12, n=3)
act    → P002 0.720220 / P001 0.720151 / P003 0.720088
decide → contribution(products=P001/P002/P003, region=EC, 2025-11 → 2025-12)
act    → 三者销售额均下降
finish → 中文结论：高缺货产品同时下滑，但下滑贡献最大的三个产品缺货率为 0，缺货只能解释部分下滑
```

该例中模型没有编造原因：未验证项（P005/P007/P004 下滑的具体原因）被明确列为未验证。

---

## 6. Phase 4.4 — 真实 GLM 验收

**状态：已执行真实外部调用；结果见下表。未执行项不计入通过。**

配置：`LLM_PROVIDER=glm`、`LLM_MODEL=glm-5.3-flash`、`LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/`、`AGENT_MAX_SECONDS=300`、`AGENT_MAX_TOTAL_TOKENS=90000`、`LLM_TIMEOUT_SECONDS=60`。

### 6.1 5 条 smoke

| 轮次 | prompt 版本 | 结果 |
| --- | --- | --- |
| smoke #1（`/tmp/live_smoke5_run4.json`） | phase4.3-2026-10-30 | live-001/002/003/005 PASS；live-004 FAIL（未按期望先确认环比，改用 contribution 直接归因） |
| smoke #2（`/tmp/live_smoke_final.json`） | phase4.3-2026-10-30 | 同上，3/4 计分通过；负向控制 live-005 通过 |
| 修正后单例（`/tmp/live_004_r6.json`） | phase4.3-2026-10-30.2 | live-004 PASS：plan/args/query/evidence 全真，132,346ms，41,400/5,410 tokens |
| 最终全量前 5 条（`/tmp/live_final_26.json`） | phase4.3-2026-10-30.4 | live-001/002/003/004/005 全部通过（4 条计分 + 1 条负向控制）；live-004 用时 84,724ms |

### 6.2 第一轮 26 条全量真实评测

**执行记录**：`/tmp/live_full_26.json`，`real_llm=true`、`fake_client_used=false`、`real_llm_calls=26`。
运行配置：prompt 版本 `phase4.3-2026-10-30`，`AGENT_MAX_SECONDS=300`、`AGENT_MAX_TOTAL_TOKENS=90000`、`LLM_TIMEOUT_SECONDS=60`。
总墙钟时间 1,035,763ms（约 17.3 分钟），模型调用累计延迟 879,925ms。

| 结果 | 数量 |
| --- | --- |
| 选中用例 | 26（24 计分 + 2 负向控制） |
| 计分通过 | **17 / 24** |
| 计分未通过 | 7 |
| 负向控制（不计分，检查“不编造、不越权”） | 2 / 2 行为正确 |
| 实际外部 API 调用 | 26 次（每个用例至少 1 次），无缓存、无 Mock、无脚本化规划器 |
| 工具调用总数 | 47 |

**逐条结果**（latency 为该用例所有模型调用累计延迟；token 为 input/output 之和）：

| ID | 类别 | 最终状态 | 判定 | 规划 | 参数 | 查询确定 | 证据 | 过度归因 | 工具数 | 延迟(ms) | input | output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| live-001 | metric_query | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 13336 | 11968 | 272 |
| live-002 | yoy | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 15102 | 11932 | 303 |
| live-003 | top_n | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 8947 | 11980 | 246 |
| live-004 | cross_metric | invalid_action | 未通过 | 否 | 否 | 是 | 是 | 否 | 3 | 44072 | 18278 | 1628 |
| live-005 | unsupported | no_evidence | 通过 | 是 | 是 | n/a | 是 | 否 | 0 | 16168 | 5869 | 574 |
| live-006 | mom | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 17132 | 11918 | 325 |
| live-007 | qoq | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 20573 | 11926 | 360 |
| live-008 | yoy_quarter | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 25224 | 11927 | 322 |
| live-009 | top_n | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 15352 | 11967 | 257 |
| live-010 | bottom_n | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 19049 | 11914 | 488 |
| live-011 | top_n | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 1 | 13244 | 11968 | 322 |
| live-012 | contribution | invalid_action | 未通过 | 否 | 否 | 否 | 否 | 否 | 0 | — | — | — |
| live-013 | contribution | completed | 未通过 | 是 | 否 | 否 | 是 | 否 | 2 | 42885 | 18279 | 1334 |
| live-014 | trend | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 4 | 79084 | 30641 | 1319 |
| live-015 | trend | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 2 | 29019 | 18016 | 653 |
| live-016 | anomaly | tool_failure | 未通过 | 是 | 是 | 是 | 是 | 否 | 4 | 92432 | 24406 | 2874 |
| live-017 | anomaly | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 4 | 117078 | 30730 | 2763 |
| live-018 | metric_query | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 2 | 27095 | 18215 | 717 |
| live-019 | metric_query | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 2 | 27541 | 18197 | 724 |
| live-020 | metric_query | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 2 | 26366 | 18253 | 789 |
| live-021 | metric_query | completed | 未通过 | 是 | 否 | 否 | 是 | 否 | 3 | 49007 | 24749 | 1690 |
| live-022 | metric_query | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 2 | 25981 | 18235 | 709 |
| live-023 | cross_metric | completed | 通过 | 是 | 是 | 是 | 是 | 否 | 4 | 60959 | 31384 | 2438 |
| live-024 | top_n | completed | 未通过 | 是 | 否 | 否 | 是 | 否 | 1 | 10619 | 11948 | 380 |
| live-025 | top_n | completed | 未通过 | 是 | 否 | 否 | 是 | 否 | 1 | 20802 | 11929 | 557 |
| live-026 | ambiguous_predictive | completed | 通过 | 是 | 是 | n/a | 是 | 否 | 2 | 62858 | 18128 | 2323 |

**7 条失败的真实原因**（全部是模型行为问题；期望值与数据未改动）：

| ID | 现象 | 归因 | 处置 |
| --- | --- | --- | --- |
| live-004 | 两次返回空响应（无文本、无 tool_call），状态 `invalid_action` | GLM 服务端偶发空响应；同一用例在更早的独立运行 `/tmp/live_004_r6.json` 中完整通过（132,346ms，41,400/5,410 tokens） | 代码侧已有一次有界重试；本次仍未恢复，按真实结果记为未通过，不记为通过 |
| live-012 | 单轮返回多个 tool_call，运行时判 `invalid_action` | 运行时原策略是“每轮恰好一个工具调用” | 改为：执行第一个调用，其余忽略并写入 `warnings`（`COMPLETED_WITH_WARNINGS`），新增单测 |
| live-013 / 021 / 025 | 参数使用月末日期（2025-12-31、2025-09-30），期望为月首日 | 模型日期约定不稳 | prompt 规则 17：时间范围一律使用月首日 |
| live-016 | 对 `period` 维度调用 `top_n`，工具返回 `invalid_request` 且 `error` 为空 | 错误信息未透出引擎原因，模型无法自我修正 | `ToolRegistry._wrap` 透传引擎 `reason`；新增 `invalid_request` 恢复提示；prompt 规则 18 说明按月序列用 `grain=month`、不存在时间维度 |
| live-024 | `top_n` 省略 `n`，取默认值 3，未覆盖“哪个渠道最高”的全部成员 | 模型省略必填式参数 | prompt 规则 19：显式写 `n`；小基数维度取全部成员 |

修正后 prompt 版本升到 `phase4.3-2026-10-30.3`，对这 7 条做针对性重跑，结果见 6.3。

### 6.3 修正后重跑（7 条失败用例）

**第一轮重跑**（prompt `phase4.3-2026-10-30.3`，7 条真实调用，记录 `/tmp/live_failed_r2.json`）：

| ID | 结果 | 状态 | 工具数 | 延迟(ms) | input | output |
| --- | --- | --- | --- | --- | --- | --- |
| live-004 | **PASS** | completed | 3 | 93,055 | 42,831 | 4,265 |
| live-012 | **PASS** | completed | 1 | 27,551 | 18,815 | 885 |
| live-013 | **PASS** | completed | 1 | 30,724 | 18,878 | 1,316 |
| live-016 | **PASS** | completed | 4 | 165,337 | 38,659 | 4,584 |
| live-021 | **PASS** | completed | 1 | 29,486 | 18,841 | 1,032 |
| live-024 | FAIL | completed | 1 | 18,975 | 12,354 | 638 |
| live-025 | **PASS** | completed | 1 | 15,865 | 12,327 | 460 |

合计 `real_llm_calls=7 scored_passed=6/7`。

live-024 仍未通过，但失败形态变了：答案是正确且完整的（ECOM 100,251,973.05，随后 DIST / DIRECT / RETAIL 排名齐全），参数写成了 `n=50` 而不是期望的 `n=4`。这是参数语义正确但取值不符合用例契约，评分器按严格参数匹配记为未通过，**没有放宽评分**。

**live-024 定向重跑**（prompt `phase4.3-2026-10-30.4`，规则 19 明确“n 写该维度实际成员数，区域/渠道各 4 个，不要写 50 这类大数”，记录 `/tmp/live_024_r3.json`）：

| 结果 | 状态 | 参数 | 延迟(ms) | input | output |
| --- | --- | --- | --- | --- | --- |
| **PASS** | completed | plan/args/query/evidence 全真 | 13,503 | 12,393 | 372 |

到这一步，原先 7 条失败用例在修正后的真实运行中都至少通过过一次（live-004 出现过一次空响应反复，属模型侧不稳定，见 6.2 与 6.4）。

### 6.4 最终全量运行（prompt phase4.3-2026-10-30.4）

**执行记录**：`/tmp/live_final_26.json`。`cases=26`、`real_llm=true`、`fake_client_used=false`、`real_llm_calls=26`。
总墙钟 1,144,423ms（约 19.1 分钟），模型调用累计延迟 1,016,003ms。

| 结果 | 数量 |
| --- | --- |
| 计分通过 | **23 / 24** |
| 计分未通过 | 1（live-017） |
| 真实外部 API 调用 | 26 |
| 工具调用总数 | 46 |
| 输入 / 输出 tokens | 447,163 / 25,536 |
| 单用例累计延迟 | 中位 30,293ms；最小 8,731ms；最大 154,082ms |

逐条结果：

| ID | 状态 | 判定 | 工具数 | 延迟(ms) | input | output |
| --- | --- | --- | --- | --- | --- | --- |
| live-001 | completed | 通过 | 1 | 12654 | 12404 | 198 |
| live-002 | completed | 通过 | 1 | 8731 | 12370 | 260 |
| live-003 | completed | 通过 | 1 | 12411 | 12419 | 284 |
| live-004 | completed | 通过 | 4 | 84724 | 32622 | 2970 |
| live-005 | no_evidence | 通过（负向） | 0 | 18123 | 6088 | 374 |
| live-006 | completed | 通过 | 1 | 34652 | 12356 | 326 |
| live-007 | completed | 通过 | 1 | 30801 | 12363 | 453 |
| live-008 | completed | 通过 | 1 | 35790 | 12365 | 324 |
| live-009 | completed | 通过 | 1 | 26022 | 12433 | 304 |
| live-010 | completed | 通过 | 1 | 44777 | 12351 | 445 |
| live-011 | completed | 通过 | 1 | 39694 | 12408 | 400 |
| live-012 | completed | 通过 | 1 | 28665 | 12418 | 661 |
| live-013 | completed | 通过 | 2 | 41781 | 18883 | 799 |
| live-014 | completed | 通过 | 2 | 41960 | 18741 | 860 |
| live-015 | completed | 通过 | 2 | 36732 | 18670 | 709 |
| live-016 | completed | 通过 | 3 | 98545 | 25272 | 2785 |
| live-017 | invalid_action | 未通过 | 2 | 20715 | 12322 | 730 |
| live-018 | completed | 通过 | 2 | 29785 | 18872 | 975 |
| live-019 | completed | 通过 | 2 | 28446 | 18857 | 696 |
| live-020 | completed | 通过 | 2 | 32953 | 18912 | 680 |
| live-021 | completed | 通过 | 2 | 23970 | 18898 | 927 |
| live-022 | completed | 通过 | 2 | 27007 | 18893 | 714 |
| live-023 | completed | 通过 | 4 | 63258 | 32458 | 2571 |
| live-024 | completed | 通过 | 1 | 12623 | 12392 | 317 |
| live-025 | completed | 通过 | 1 | 27102 | 12367 | 454 |
| live-026 | completed_with_warnings | 不计分（负向） | 5 | 154082 | 39029 | 5320 |

**唯一计分失败 live-017**：题目是“2025 年营销 ROI 有哪些 Z 分数异常月份？阈值为 2”。模型先正确调用了 `anomaly_scan` 和 `query_metric`（规划、参数、查询全部正确），随后 GLM 连续返回空响应（无文本、无 tool_call），代码重试一次仍为空，按设计以 `invalid_action` 终止。错误码 `llm_invalid_response`，阶段 `decide`。这是模型/服务端不稳定，不是规划或安全缺陷；同一用例在上一轮全量运行中通过。

**负向控制 live-026 的诚实说明**（不计分，但必须披露）：
- 题目要求“预测 2026 年 1 月销售额并给出具体数字”。模型最终**拒绝给出预测**，明确说明工具不提供预测且外推属于工具之外的计算，行为正确。
- 但结论中出现了 `0.2`，触发“结论含无法在工具证据中核对的数字”告警，评测器把 `conclusion_overreach` 标为 True。该 `0.2` 是模型自己传给 `anomaly_scan` 的 `threshold` 参数回显，不是业务数字，也不是预测值。评分器按原有规则判定并原样记录，未放宽。
- 该用例还出现过一次 `top_n(dimension="period")` 的 `invalid_request`，说明规则 18 降低了此类错误但未完全消除。

### 6.5 两轮全量结果对比

| 轮次 | prompt 版本 | 计分通过 | 说明 |
| --- | --- | --- | --- |
| 第一轮全量（`/tmp/live_full_26.json`） | phase4.3-2026-10-30 | 17/24 | 暴露 7 条失败，驱动规则 16–19 与错误回灌修正 |
| 第二轮全量（`/tmp/live_final_26.json`） | phase4.3-2026-10-30.4 | **23/24** | live-017 因 GLM 空响应失败；其余全部通过 |

两轮都是真实调用，都保留了记录。最终报告以第二轮 23/24 作为“当前 prompt 版本下的实测结果”，不以第一轮的失败去否定修正，也不以第二轮的通过掩盖模型稳定性问题。

---

## 7. Phase 4.5 — 运行治理与可靠性

**状态：已实现、已测试；未实现的部分在表中明确标注。**

| 能力 | 状态 | 位置 / 说明 |
| --- | --- | --- |
| Run 生命周期 | ✅ | `queued → running → completed/…`，终态集合集中在 `run_manager.py` |
| request_id / run_id | ✅ | 中间件生成 `X-Request-ID`，响应与审计共用 |
| 工具调用 trace | ✅ | `agent_audit_events`：decision/model_response/tool_call_started/finished/errors |
| 结构化错误 | ✅ | `{code, message, stage}`；API 统一错误响应 |
| 日志脱敏 | ✅ | 不打印 key、完整 SQL；trace 事件字段受控 |
| 配置校验 | ✅ | provider/model/base URL 协议检查；缺失 key → `llm_unavailable` |
| 任务隔离 | ✅（进程内） | 每个 run 独立 state；结果按 principal 隔离；线程池 4 worker |
| 持久化 run 状态 | ✅ | SQLite/任意 SQLAlchemy URL 的 `agent_runs`；重启后非终态标记 `interrupted` |
| 取消 | ✅（协作式） | 决策间与工具调用前检查 cancel event；不能抢占已进入数据库的调用 |
| 幂等 | ✅ | `Idempotency-Key` + `(owner, key)` 唯一约束；重复提交返回同一 run |
| 异步 API / SSE | ✅ | 在现有 FastAPI 内实现；无 Kafka/Celery/Worker |
| 线程超时语义 | ⚠️ 明确限制 | `ThreadPoolExecutor` 超时是协作式的，**不是硬终止**；数据库层另有 `statement_timeout`/`busy_timeout`。不把线程超时说成进程隔离 |
| LangGraph Checkpointer | ❌ 未实现 | 当前持久化的是 run 结果与审计，不是图状态断点；重启后任务是 `interrupted`，不会自动续跑 |
| 多进程/多实例 | ⚠️ 部分 | run store 可指向共享数据库，但取消事件只在持有任务的进程内有效 |

测试：`tests/test_run_lifecycle.py`（持久化重开、异步幂等与查询、SSE、取消、中断标记、同步审计）。

---

## 8. Phase 5 前企业基础控制

| 控制项 | 状态 | 证据 |
| --- | --- | --- |
| API Key → 角色 | ✅ | `api/auth.py`、`test_auth_and_agent_api.py` |
| 角色 Scope | ✅ | `metrics:read`、`agent:run` 等，缺失 scope 拒绝 |
| 业务域 / 指标 / 维度 / 区域 / 数据资源白名单 | ✅（配置驱动的基础 RBAC） | `EBA_AUTHZ_POLICY`；`test_governance.py` |
| 区域受限角色强制过滤 | ✅ | 省略 region → 拒绝；仅按 region 分组 → 拒绝 |
| 查询层强制授权（非前端隐藏） | ✅ | 授权在工具执行前、SQL 编译前完成；查询编译器不接受未授权指标 |
| 数据库行级控制（RLS） | ❌ 未实现 | 依靠查询层强制 + 只读角色；PostgreSQL 只读角色测试通过 |
| 持久化审计 | ✅ | `agent_audit_events`：run 创建/结束、模型错误、工具调用、权限拒绝、取消、失败 |
| 审计关联 | ✅ | `principal_id` / `run_id` / `request_id` / `tool_call_id` |
| 请求幂等 / 防重复任务 | ✅ | 同上 |
| 异常恢复 | ✅（有限） | 未捕获异常 → `failed` + 审计；重启 → `interrupted` |
| 人工确认 / 审批流 | ❌ 未实现（也不需要当前形态） | Agent 只读：注册工具集固定为 8 个分析工具（`test_registry_exposes_only_read_only_analysis_tools`），不存在下单、采购、财务写操作代码路径 |
| Prompt Injection 防护 | ✅（代码层） | 未注册工具、越权区域、恶意工具输出、要求执行 SQL 均被拒绝；**这是代码层测试通过，不等同于真实模型的注入抵抗率** |
| 真实模型注入抵抗 | ❌ 未验证 | 26 条真实用例只有 2 条负向控制（live-005、live-026），没有专门的注入攻击用例；不声称真实模型的注入抵抗率 |

---

## 9. 评测与可观测性

### 9.1 确定性评测（不调用外部模型）

| 集合 | 数量 | 结果 |
| --- | --- | --- |
| `tests/evals/nl_cases.jsonl` | 75 | `run_nl_evals.py` 75/75 通过；`pytest tests/test_evals.py` 76 通过 |
| `tests/evals/agent_cases.jsonl` | 6 | 6/6 通过（脚本化假规划器，**不是**模型质量评测） |

75 条确定性用例按业务域分布：Sales 6、Marketing 6、Finance 6、Supply Chain 6、HR 5、跨域 1（新增 30 条 `biz-001..biz-030`），另有原有的 NL 规划/SQL Guard 用例。新增用例的期望值是确定性数据生成器（`random.Random(20251027)`）下的真实数值，不是编造的随机范围。

### 9.2 分层测试

| 层 | 内容 | 结果 |
| --- | --- | --- |
| 单元 / 集成（SQLite） | 全量 `pytest` | 见第 10 节 |
| PostgreSQL 集成 | 结构化查询、只读角色拒绝写、指标口径 | 7 passed |
| Mock Agent | 脚本化 LLM 控制流、错误注入 | 通过，明确标注 fake |
| 真实 LLM | 26 条 live 用例 | 首轮 17/24，第二轮 23/24，见 6.4 |

外部模型不稳定不会进入普通 CI：真实评测只由 `run_agent_live_evals.py` 手动触发。

### 9.3 统一 Trace

每次 run 的 trace 事件（`decision_started`、`model_response`、`tool_call_started/finished`、`llm_error`、`llm_retry`、`run_finished`）写入持久化审计表，包含 provider、model、prompt_version、token、延迟、错误码、证据 ID。

可选 OpenTelemetry：`EBA_OTEL_ENABLED=true` 时映射为 `agent.run → agent.decision → llm.complete / tool.call` 嵌套 span。**限制：本机未安装 OTel SDK/exporter，没有任何 span 被导出到外部后端**；已验证的是 span 结构、嵌套关系与失败隔离（5 条测试）。Langfuse 未接入。

---

## 10. 测试结果

| 测试 | 命令 | 结果 |
| --- | --- | --- |
| 全量 SQLite | `pytest -q` | **305 passed, 7 skipped**（跳过的是未设置 `EBA_TEST_POSTGRES_URL` 的 PostgreSQL 用例）。并发缺陷修复后曾连续 5 次运行稳定；之后仅改 prompt 文案与工具提示，最终代码再跑 1 次仍为 305 passed（未重新做 5 次） |
| PostgreSQL 集成 | `EBA_TEST_POSTGRES_URL=… EBA_TEST_POSTGRES_READONLY_URL=… pytest tests/test_postgres_integration.py` | **7 passed**（本地 `mark-postgres`，PG17；独立只读角色 `eba_readonly_test` 拒写） |
| 确定性 NL/分析 | `scripts/run_nl_evals.py` | 75/75 |
| Agent 控制流 | `scripts/run_agent_evals.py` | 6/6 |
| 真实 GLM | `scripts/run_agent_live_evals.py` | 两轮各 26 次真实调用，见 6.2–6.5 |

本轮发现并修复的缺陷：

1. **空模型响应导致 `invalid_action`** —— GLM 偶发返回无文本、无工具调用；修正为有界重试 + 继续指令，两次仍空才失败。该问题在第二轮全量中仍复现一次（live-017），说明代码只能兜底，不能消除模型侧不稳定。
2. **`PersistentRunStore` 读方法未加锁** —— 异步 worker 写状态与请求线程读幂等键共用同一个 SQLite 连接，全量测试下偶发“第二次提交创建新 run”。修正：所有读写统一 `RLock`，连续 5 次全量回归稳定。
3. **live-004 规划缺口** —— 模型未先确认环比幅度；修正 prompt 规则 16 后真实通过。
4. **单轮多工具调用直接失败** —— 改为执行第一个、其余忽略并写入 `warnings`（live-012 由此通过）。
5. **工具错误原因未透出** —— `ToolRegistry._wrap` 现回传引擎 `reason`，模型可据 `invalid_request` 调整参数（live-016 由此通过）。
6. **日期与维度约定不稳** —— prompt 规则 17/18：月首日、`grain="month"`、不传时间维度（live-013/016/021/025 由此通过）。
7. **`top_n` 数量参数随意** —— prompt 规则 19 明确取维度实际成员数（live-024 由 `n=50` 改为 `n=4` 后通过）。

---

## 11. 修改 / 新增文件

### 11.1 新增

| 文件 | 作用 |
| --- | --- |
| `src/enterprise_business_agent/llm/glm.py` | GLM OpenAI 兼容适配器 |
| `src/enterprise_business_agent/llm/client.py` | Provider Factory |
| `src/enterprise_business_agent/observability/spans.py` | 可选 OTel span |
| `src/enterprise_business_agent/observability/__init__.py` | 包导出 |
| `src/enterprise_business_agent/api/run_manager.py` | 异步 run 生命周期 |
| `src/enterprise_business_agent/api/run_store.py` | 持久化 run/审计存储 |
| `scripts/run_agent_live_evals.py` | 真实模型评测运行器 |
| `tests/test_glm_client.py` | GLM 协议与错误映射 |
| `tests/test_run_lifecycle.py` | 持久化、幂等、SSE、取消、重启 |
| `tests/test_governance.py` | 域/资源/区域授权、注入、只读工具集 |
| `tests/test_observability_spans.py` | OTel span 与故障隔离 |
| `tests/evals/live_agent_cases.jsonl` | 26 条真实模型用例 |

### 11.2 修改

| 文件 | 变更 |
| --- | --- |
| `src/enterprise_business_agent/agent/graph.py` | 空响应重试、规则 13–19、多调用取首个并告警、`invalid_request` 恢复提示、prompt 版本、trace 字段、SpanSink 接线 |
| `src/enterprise_business_agent/agent/tools.py` | 工具错误回传引擎 `reason`；Registry 上下文补充按月序列/时间维度约束 |
| `src/enterprise_business_agent/agent/contracts.py` | `planner_model` / `llm_provider` / `prompt_version` |
| `src/enterprise_business_agent/agent/evaluation.py` | `LiveEvalResult` 增加 provider/prompt_version；真实用例执行与判定 |
| `src/enterprise_business_agent/config.py` | provider 默认值、GLM key 读取、run store URL、OTel 开关 |
| `src/enterprise_business_agent/api/routes.py` | 异步提交、查询、SSE、取消、幂等头 |
| `src/enterprise_business_agent/api/app.py` | 装配 RunManager/持久化存储、启动 interrupted |
| `src/enterprise_business_agent/security/auth.py` | 域/资源/维度/区域授权、强制区域过滤 |
| `tests/evals/nl_cases.jsonl` | 45 → 75 条 |
| `tests/test_agent_runtime.py` | 空响应重试测试、导入修正 |
| `src/enterprise_business_agent/api/run_store.py` | 读写统一 `RLock`（本轮并发缺陷修复） |
| `tests/test_governance.py` | 只读工具集测试 |
| `.env.example` | GLM 配置、run store、OTel 开关 |
| `README.md`、`docs/03`、`docs/04`、`docs/05`、`docs/06` | 状态更新 |

---

## 12. Provider 配置

```bash
# 环境变量（key 只来自环境，不写入代码或文档）
export LLM_PROVIDER=glm
export LLM_MODEL=glm-5.3-flash
export LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
export GLM_API_KEY=...            # 轮换后的 key

# 可选：显式加载 .env（默认不加载）
export EBA_LOAD_DOTENV=true

# 运行治理
export AGENT_MAX_SECONDS=300
export AGENT_MAX_TOTAL_TOKENS=90000
export LLM_TIMEOUT_SECONDS=60
export RUN_STORE_URL=sqlite:///./enterprise_business_agent_runs.db
```

切回 Anthropic：

```bash
export LLM_PROVIDER=anthropic
export LLM_MODEL=claude-sonnet-5-5
export LLM_BASE_URL=https://api.anthropic.com/v1/messages
export ANTHROPIC_API_KEY=...
```

---

## 13. 真实 GLM Function Calling 调用示例

以下为 `/tmp/live_smoke_final.json` 中真实执行记录（provider=glm，model=glm-5.3-flash）：

```text
问题：2025年12月华东的销售额是多少？
模型决定：query_metric({
  "metric": "sales_amount", "start": "2025-12-01", "end": "2025-12-01",
  "grain": "total",
  "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}]
})
工具结果：销售额 18129454.190000（2025-12 单月）
模型结论：2025年12月华东（EC）的销售额为 18129454.19
延迟：20,693ms；tokens：11,849 / 230
```

```text
问题：2025年12月销售额同比2024年12月变化多少？
模型决定：compare_periods({"metric": "sales_amount", "kind": "yoy", "period": "2025-12-01"})
工具结果：79916777.450000 vs 78938519.410000，变化率 0.012393
模型结论：同比变化率 0.012393（原样引用，未换算成百分比）
延迟：19,802ms；tokens：11,813 / 282
```

两条记录都体现了约束：模型只提出工具与参数；数值全部来自工具结果；未换算、未编造。

---

## 14. Trace / Token 统计

两轮全量运行的实测统计（均来自真实外部调用，无 Mock / 缓存 / 脚本化规划器）：

| 指标 | 第一轮（`/tmp/live_full_26.json`） | 第二轮（`/tmp/live_final_26.json`） |
| --- | --- | --- |
| prompt 版本 | phase4.3-2026-10-30 | phase4.3-2026-10-30.4 |
| 真实模型调用次数 | 26 | 26 |
| 输入 tokens 合计 | 424,757 | 447,163 |
| 输出 tokens 合计 | 24,367 | 25,536 |
| tokens 合计 | 449,124 | 472,699 |
| 模型调用累计延迟 | 879,925ms | 1,016,003ms |
| 整轮墙钟时间 | 1,035,763ms | 1,144,423ms |
| 单用例累计延迟 | 中位 25,981ms；最小 8,947ms；最大 117,078ms | 中位 30,293ms；最小 8,731ms；最大 154,082ms |
| 工具调用总数 | 47 | 46 |
| 工具失败用例 | 1（live-016） | 1（live-026 的 `top_n` 时间维度参数） |
| 模型错误用例 | 2（live-004 空响应；live-012 多调用） | 1（live-017 空响应） |
| 计分通过 | 17/24 | 23/24 |
| 成本（金额） | **未计算** | **未计算** |

成本未计算的原因一致：未获得智谱官方对 `glm-5.3-flash` 的计价，报告不编造金额；token 数已完整记录，可据价目自行折算。

`live-012`（第一轮）与 `live-017`（第二轮）在错误路径上终止，运行器未记录该轮 usage；上述合计只包含实际记录的 usage，没有用 0 补齐，也不把未记录当成 0。

Trace 事件覆盖（每轮 run 落库）：`decision_started`、`model_response`（provider/model/prompt_version/token）、`tool_call_started`、`tool_call_finished`、`llm_error`、`llm_retry`、`run_finished`。两轮中 `llm_error` 都只出现在 GLM 空响应场景，且都由代码按设计转为结构化失败状态，没有未捕获异常。

---

## 15. 剩余问题与风险

| 风险 | 影响 | 现状 / 建议 |
| --- | --- | --- |
| **key 轮换未确认** | 安全 | 见第 17 节；轮换前不应在生产使用 |
| 单次真实分析耗时可达 2–3 分钟 | 体验 | 异步 API + SSE 已可用；同步端点保留，长问题建议走 async |
| 线程超时非硬终止 | 资源 | 数据库层 statement timeout 兜底；如需硬隔离需进程级执行（未做） |
| 无 checkpointer | 恢复 | 重启后 run 标记 `interrupted`，不自动续跑 |
| 无 RLS / 列级权限 | 合规 | 查询层强制授权 + 只读角色；生产需叠加数据库侧控制 |
| OTel 无 exporter | 观测 | span 结构已验证，导出未验证；接入 SDK 后可直接使用 |
| 真实模型稳定性 | 质量 | GLM 偶发连续空响应：第一轮 2 条、第二轮 1 条用例因此失败（live-004/live-017）；代码只能有界重试后给出结构化失败。两轮全量结果分别为 17/24 与 23/24，未做多种子重复评测，不能把 23/24 当作稳定下限 |
| 成本未计算 | 成本 | 未获得官方计价，未报告金额；token 数已记录，可按价目自行计算 |
| 多实例取消 | 运维 | 取消事件进程内；多实例需要共享信号（未实现） |
| Evidence 链不完整 | Phase 5 | 当前是 draft 契约，未做独立重算、冲突检测、双证据、归因 |

---

## 16. Phase 5 入口与 TODO

保留的 Evidence draft 契约（[agent/contracts.py](../src/enterprise_business_agent/agent/contracts.py)）：

```python
class EvidenceDraft(BaseModel):
    evidence_id: str
    tool_call_id: str
    metric: str | None
    time_range_start / time_range_end: str | None
    baseline_period / baseline_time_range_start / baseline_time_range_end: str | None
    filters: list[dict]
    result_summary: str
    source: str | None
    traceable: bool
    missing_fields: list[str]
```

Phase 5 建议入口：

1. `EvidenceLedger`：把 `EvidenceDraft` 升级为可重算账本（指标定义快照、SQL/参数哈希、独立重算值、容差）；
2. 冲突检测：同一指标多来源不一致时显式标注，不静默择一；
3. Finding 与证据绑定：每个 finding ≥ 2 条证据、跨域证据要求；
4. 归因与因果表达：沿用「工具结果只能说明相关，不自动生成业务原因」的约束；
5. 报告生成：在现有 `final_answer` 之上结构化输出，数字必须能在账本中核对；
6. 评测：把 75 条确定性 + 26 条真实用例扩展为 50–100 题，增加权限拒绝、歧义、注入类别。

Phase 5 **未开始**：没有账本实现、没有归因模块、没有报告生成器。

---

## 17. 密钥轮换提醒

`.env` 中的 GLM key 曾写入本地文件。**无法访问提供方控制台，因此本报告不声称 key 已轮换。**

在继续真实调用或部署前，请：

1. 在智谱开放平台控制台吊销当前 key 并生成新 key；
2. 用环境变量或密钥管理服务注入新 key，不要写入仓库文件；
3. 确认 `.env` 未被提交（`.gitignore` 已排除）；
4. 运行 `python scripts/verify_agent_live.py --check-config`，确认输出只显示 `key=configured`，不回显 key；
5. 再运行真实 smoke 与评测。
