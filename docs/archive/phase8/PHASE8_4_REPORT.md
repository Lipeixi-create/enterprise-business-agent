# Phase 8.4 生产测试与最终验收报告

日期：2026-11-03
状态：**离线生产验收核心完成；真实模型、Docker 实跑、浏览器 E2E、PostgreSQL 集成在本环境未执行**
基线：Phase 8.3 `709 passed, 25 skipped`
本阶段结果：新增 13 个生产验收测试；全量回归 **`722 passed, 25 skipped in 66.48s`**

> 证据分级：本报告中的测试结果全部来自本机实际执行的 pytest。
> 这些测试使用真实只读 SQLite、真实 LangGraph、真实 Evidence/Finding/Report/Approval 链路和真实 FastAPI，
> 但 LLM 为仓库内脚本化假客户端。因此它们是**离线功能与故障模式验收**，不是真实模型质量评测，
> 也不能替代 Phase 7.9 的真实 GLM 归档。

---

## 1. 阶段目标与执行边界

Phase 8.4 要求覆盖：

- 真实业务场景与全 Agent 链路；
- 功能、权限/安全、MCP、API；
- 并发/压力、长任务/超时恢复、服务重启恢复、数据库故障、模型服务故障；
- 报告数字准确性、Evidence 可追溯性、审批隔离；
- 保持 Phase 7.9 基线，真实与离线结果分开归档，不修改答案/容差、不伪造成功。

本阶段实际完成的是**可重复的离线生产验收套件**。没有新增业务功能，没有修改 SQL Guard、权限、
Tool Registry、Agent 决策循环、Evidence/Finding/Report 规则或审批规则。

未执行的部分及原因：

| 项目 | 状态 | 原因 |
| --- | --- | --- |
| 真实 LLM 业务评测 | 未执行 | 当前环境未配置可用的模型 Key；不伪造真实运行结果 |
| Docker 镜像构建 / `docker compose up` | 未执行 | 当前网络无法从 Docker Hub 拉取 `python:3.12-slim`（Phase 8.3 已记录） |
| 浏览器自动化 E2E | 未执行 | 当前环境没有 Playwright/Selenium 等浏览器自动化工具 |
| PostgreSQL 集成测试 | 未执行 | `EBA_TEST_POSTGRES_URL`、`EBA_TEST_POSTGRES_RUNTIME_URL` 未设置，相关测试自动 skip |
| 真实 OTLP 导出 | 未执行 | 本机没有 OTLP Collector/Jaeger/厂商后端 |
| 长时间压力/容量基准 | 未执行 | 本阶段只做并发功能验证，不是吞吐/容量压测 |

---

## 2. 执行前现状

Phase 8.3 结束时已有：

- 709 个通过测试、25 个 skip；
- 同步/异步 Run、SSE、取消、幂等、持久化 Run Store；
- PostgreSQL 运行时持久化、Checkpoint 恢复、Claim/Lease/Fencing；
- Evidence Ledger、Finding Engine、报告生成与 Numeric Validator；
- 人工审批版本与报告指纹校验；
- Phase 8.1 全链路 trace、日志脱敏、告警；
- Phase 8.2 权限加固、安全审计、MCP 服务端；
- Phase 8.3 工作台、身份、历史、Docker 编排；
- Phase 7 的 92 条评测语料与真实模型归档。

Phase 8.4 前缺少的是一条**把上述能力串起来、可重复执行的生产验收套件**：业务场景到报告的端到端断言、
并发下的归属与幂等、超时/数据库/模型故障的终态语义、重启中断标记和跨账号资源隔离。

---

## 3. 新增测试套件

文件：`tests/test_phase8_production.py`（13 个测试）

测试环境：

- 每个用例独立创建临时 SQLite 业务库；
- 使用 `generate_data` 生成 24 个月（2024-01 至 2025-12）业务数据；
- 通过 `make_readonly_engine` 以只读方式挂载；
- 使用真实 `PersistentRunStore`、真实 `AgentRuntime`/LangGraph、真实 `AnalysisGateway`、
  `SemanticQueryGuard`、`SafeQueryExecutor`、Evidence/Finding/Report/Approval 链路；
- 通过 `create_app` 启动真实 FastAPI 应用，使用 `TestClient` 发请求；
- LLM 使用 `tests/agent_fakes.py` 的 `ScriptedLLM` / `tool_response` / `finish_response`，
  属于脚本化假客户端。

### 3.1 需求到测试映射

| 要求 | 测试 |
| --- | --- |
| 真实业务场景 + 全 Agent 链路 | `test_business_scenarios_complete_the_full_chain`（5 个参数化场景） |
| 跨域取证 | `test_cross_domain_scenario_keeps_both_evidence_sources` |
| 并发运行、Run 唯一、账号隔离 | `test_concurrent_async_runs_are_unique_and_owner_isolated` |
| 并发幂等 | `test_concurrent_duplicate_idempotency_creates_one_run` |
| 长任务/超时恢复 | `test_timeout_is_terminal_and_does_not_fabricate_success` |
| 数据库连接故障 | `test_business_database_outage_becomes_a_structured_tool_failure` |
| 模型服务故障 | `test_model_service_failure_returns_llm_unavailable_without_tools` |
| 服务重启恢复 | `test_restart_marks_unfinished_run_interrupted_and_history_exposes_it` |
| 报告/Evidence/审批隔离 | `test_report_evidence_and_approval_are_owner_isolated` |

### 3.2 业务场景与数字断言

| 场景 | 问题 | 工具 | 断言字段 | 期望范围 |
| --- | --- | --- | --- | --- |
| `sales-region` | 2025年12月华东销售额是多少？ | `query_metric(sales_amount, EC)` | `points.0.value` | 18,129,454.18–18,129,454.20 |
| `marketing-roi` | 2025年12月营销ROI排名前三的活动 | `top_n(ROI, campaign, n=3)` | `items.0.value` | 1.73734–1.73736 |
| `finance-qoq` | 2025年第四季度净利润环比变化如何？ | `compare_periods(net_profit, qoq, 2025Q4)` | `items.0.change_rate` | -0.22474 至 -0.22473 |
| `supply-stockout` | 2025年12月缺货率最高的5个产品 | `top_n(stockout_rate, product, n=5)` | `items.0.value` | 0.22357–0.22358 |
| `hr-cost` | 2025年12月人力成本最高的3个部门 | `top_n(labor_cost, department, n=3)` | `items.0.value` | ≥ 0 |

`hr-cost` 只断言非负，因为该场景用于验证 HR 域工具链路和证据链，不把某个模拟值固化为业务真值。

### 3.3 全链路断言

`_assert_chain` 对每个成功场景执行：

1. Run 终态为 `completed` 或 `completed_with_warnings`；
2. 存在 `report_id`，且 `report_validation_passed=true`；
3. Evidence 数量等于预期工具调用数；
4. 每条 Evidence：
   - `run_id` 等于当前 Run；
   - 有 `tool_call_id`；
   - `status=valid`；
   - `traceable=true`；
   - 有 `source_tables`；
   - 有 `metric_definition_version`；
   - 有非空 `result`；
5. Findings 非空，且每个 Finding 的 `evidence_ids` 是 Evidence 集合的子集；
6. 报告校验 `passed=true`、`issues=[]`；
7. 报告 `evidence_references` 的 ID 集合与 Evidence 集合完全相等；
8. 报告结论的 `finding_ids` 是 Finding 集合的子集；
9. `key_metrics` 的每个值都引用 Evidence；
10. 每个结论的 `evidence_ids`、`finding_ids` 都指向本次 Run 的 Evidence/Finding；
11. 每个 `value_refs[].evidence_id` 都能在 Evidence 集合中找到。

这组断言同时验证了数字准确性、Evidence 可追溯性和报告引用完整性。

### 3.4 跨域场景

问题：`华东2025年12月销售额环比下降，请结合缺货数据找原因`

脚本化模型连续调用：

1. `compare_periods(sales_amount, mom, 2025-12-01, EC)`；
2. `top_n(stockout_rate, product, 2025-12-01, n=3, EC)`；
3. `finish`。

断言：

- Evidence 类型顺序为 `["comparison", "ranking"]`；
- 销售额环比变化率在 `-0.364968` 至 `-0.364965`；
- 华东缺货率第一名为 `0.72021` 至 `0.72023`；
- 两条 Evidence 都进入 Finding 和 Report 引用链。

该场景只证明系统能把销售与供应链证据放在同一条证据链里，不证明因果结论本身成立。

### 3.5 并发与幂等

`test_concurrent_async_runs_are_unique_and_owner_isolated`：

- 两个 analyst 账号各提交 4 个异步 Run；
- `ThreadPoolExecutor(max_workers=8)` 并发提交；
- 8 个 `run_id` 全部唯一且跨账号不重叠；
- 轮询历史直到 8 个 Run 全部进入终态（上限 20 秒）；
- 每个账号的历史 `scope=self`，只能看到自己的 4 个 Run；
- 每个 Run 最终为 `completed`，报告校验通过，Evidence 数为 1；
- 账号 B 访问账号 A 的 Run 返回 404。

`test_concurrent_duplicate_idempotency_creates_one_run`：

- 4 个线程用同一 `Idempotency-Key` 同时提交；
- 全部返回 202；
- 只生成 1 个 `run_id`；
- 历史中该问题只有 1 条记录。

本机实测该并发测试耗时约 0.65 秒，只能说明功能正确，**不能当作吞吐量或容量基准**。

### 3.6 故障注入

**超时**

- 请求 `max_seconds=0.000001`；
- Run 终态 `timeout`，`stop_reason=time_budget_exhausted`；
- 没有工具调用；
- Evidence 数为 0；
- 如果存在报告，结论为空；
- 不把超时包装成成功。

**数据库故障**

- 注入的 session factory 抛出 `OperationalError("connection lost")`；
- 工具调用状态和 `error_code` 均为 `database_unavailable`；
- Run 终态为 `no_evidence` / `completed_with_warnings` / `tool_failure` 之一；
- Evidence 为空；
- API 响应不包含底层异常文本 `connection lost`，也不包含 `password`。

**模型服务故障**

- 脚本化 LLM 抛出 `LLMUnavailable`；
- Run 终态 `llm_unavailable`；
- 没有工具调用、没有 Evidence。

**服务重启恢复**

- 预先写入一个 `running` 状态的 Run；
- 应用启动时将其标记为 `interrupted`，`stop_reason=process_restart`；
- 历史接口和单 Run 接口都能看到该状态；
- 不把它继续伪装成运行中或已完成。

### 3.7 权限与审批隔离

对一个由账号 A 创建的完整 Run：

- 账号 B 访问 Evidence、Findings、Report、Approval 均返回 404；
- admin 访问同样资源返回 200；
- 证明普通账号之间不能读取彼此的证据、报告和审批，admin 的 `resource:read_any` 仍可用。

Phase 8.2 的 13 个安全/MCP 测试仍在全量回归中通过，覆盖：

- `metrics:read` scope；
- 受限角色 SQL fallback 阻断；
- 常量时间 API Key 比较；
- 安全审计事件；
- MCP 初始化、工具列表、工具调用、权限拒绝和协议错误；
- MCP 调用仍经过 `ToolRegistry` → 权限检查 → `AnalysisGateway` → `SemanticQueryGuard` → `SafeQueryExecutor`。

---

## 4. 测试结果

### 4.1 Phase 8.4 定向

```text
13 passed in 4.81s
```

最慢用例为并发提交与终态轮询，约 0.65 秒。

### 4.2 Phase 8 全部定向

```text
tests/test_phase8_observability.py
tests/test_phase8_security_mcp.py
tests/test_phase8_workbench.py
tests/test_phase8_production.py

37 passed in 9.40s
```

### 4.3 全量回归

```text
722 passed, 25 skipped in 66.48s
```

25 个 skip 为未配置 PostgreSQL 集成环境等条件性测试；没有把 skip 计为通过。

### 4.4 Phase 7 语料门禁

```text
corpus: 92 cases
corpus check: OK (no model call was made)
```

只校验语料规模和结构，没有调用模型，也没有改变 Phase 7.9 的历史结果。

---

## 5. 未验证项与阻塞

### 5.1 真实模型

当前环境未配置可用的真实模型 Key（未打印、未读取任何密钥值）。本阶段没有执行真实 GLM 调用，
因此不能声称：

- 真实模型下五个业务场景全部通过；
- 真实模型下报告数字准确率为某个百分比；
- 真实模型下并发/故障恢复表现与脚本化模型一致。

Phase 7.9 的真实运行记录和已知失败保持不变：

- `live-004` / `live-005`：提供方超时；
- `live-023`：结论越界；
- 模型成本单价未配置。

### 5.2 Docker 与浏览器

沿用 Phase 8.3 的阻塞：

- `docker compose config` 已用占位配置静态验证；
- `docker build` 因无法拉取 `python:3.12-slim` 失败；
- `docker compose up` 未实跑；
- 没有浏览器自动化，工作台未做真实浏览器视觉/交互验收。

### 5.3 PostgreSQL 与 OTLP

- PostgreSQL 集成测试因缺少测试连接环境变量而 skip；
- 没有真实 OTLP 后端，外部 trace 导出未验证；
- 本机 Phase 8.1 已验证 `file` 导出器写出真实 span，但那是本地文件，不是生产可观测平台。

### 5.4 压力与容量

并发测试只覆盖 8 个异步 Run 和 4 线程幂等，不是负载测试。没有以下数据：

- 每秒请求数；
- 持续并发下的内存/连接池变化；
- 数据库连接耗尽行为；
- 多实例部署下的租约竞争和指标聚合。

---

## 6. 文件变更

新增：

- `tests/test_phase8_production.py`
- `docs/phase8/PHASE8_4_REPORT.md`

本阶段没有修改业务实现代码。

---

## 7. 结论

Phase 8.4 已建立一套可重复执行的离线生产验收套件，覆盖：

- 五个业务域场景的完整 Agent → 工具 → SQL → Evidence → Finding → Report 链路；
- 报告数字与 Evidence 引用一致性；
- 跨域证据合并；
- 并发 Run 唯一性、账号隔离和并发幂等；
- 超时、数据库故障、模型故障、重启中断的终态语义；
- 报告/Evidence/审批的跨账号隔离。

全量回归为 **722 passed, 25 skipped**。

但本阶段**没有**完成真实模型评测、Docker 实跑、浏览器 E2E、PostgreSQL 集成和 OTLP 外部导出验证。
这些项目必须在具备对应环境和凭据后补测，不能标记为已通过。
