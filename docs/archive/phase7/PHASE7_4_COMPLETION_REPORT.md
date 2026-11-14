# Phase 7.4 运行时优化完成报告

日期：2026-11-02
范围：Phase 7 第二轮真实模型评测暴露的运行时问题
本阶段性质：**仅代码修改、离线测试与离线评测；未发起任何真实 LLM 调用。**

> 本报告中所有“通过”均指离线/脚本化（TEST FAKE）验证，不代表真实模型表现。
> 第三轮真实模型评测**未启动**，需用户另行授权。

## 1. 第二轮暴露的问题

| 问题 | 第二轮证据 | 根因 |
| --- | --- | --- |
| 超时案例耗尽时间预算后仍发起新请求 | `phase7-026:llm_timeout`，单例 120 s 用尽，7/20 执行后停止 | 客户端重试与 Agent 决策各自计时：客户端 3 次尝试 × 20 s，Agent 侧仅以固定 5 s 下限判断是否继续，未参考实际单次调用耗时 |
| 同一查询被反复执行 | 模型可能先 `query_metric` 取全维度，再 `top_n` 重新排序同一数据 | 仅按工具名+参数做完全相同的调用判重，语义等价但参数不同的调用仍会真正查询 |
| 决策过程不可解释 | 归档只有 steps/llm_calls/tool_call_trace 的基础字段 | 没有重复判定、拦截原因、剩余预算、重试次数、预算终止原因 |

## 2. 修复内容

### 2.1 重试与单例预算协同

`src/enterprise_business_agent/llm/client.py`、`src/enterprise_business_agent/llm/glm.py`：

- 新增 `MIN_REQUEST_WINDOW_SECONDS = 5.0`。
- `_budgeted_attempt_timeout(deadline, configured)`：每次尝试的 HTTP 超时取“配置超时”和“剩余预算”的较小值；剩余不足 5 s 时返回 `None`，不发请求。
- `_budgeted_retry_delay(attempt, deadline)`：指数退避 0.5 s→2.0 s；若“退避 + 最少请求窗口”已装不下，返回 `None`，不再重试。
- 预算不足时抛出 `LLMTimeout("insufficient remaining budget for a model request")`，并打上 `attempts` 与 `budget_limited=True` 属性；正常超时/HTTP 失败同样带 `attempts`。
- 成功响应通过 `LLMToolResponse.attempts` 报告实际尝试次数。

`src/enterprise_business_agent/agent/graph.py`：

- `_required_request_seconds(settings, observed)` = `max(5.0, min(llm_timeout_seconds, 最近一次逻辑调用耗时))`。用实测耗时而不是固定下限估计下一跳需求，避免“快模型被误拦、慢模型被放行”。
- `_decide` 重新决策前校验：只有在**已经至少完成一次工具调用**、且剩余时间不足一次预估请求时，才以 `TIMEOUT / time_budget_exhausted / budget_stop_reason=insufficient_remaining_time` 停止，并保留已有 Evidence 与部分结论。
- LLM 超时后重试前额外校验：`retryable = timeout_retries < agent_llm_max_retries and 剩余时间 >= required`；不满足即停止，`budget_stop_reason` 为 `llm_request_budget_exhausted`（客户端预算拒绝）或 `llm_request_timeout`（真实超时）。
- `_continue_final_answer` 的续写判断由固定 0.5 s 改为同一动态需求。
- 所有路径记录 `last_llm_call_seconds`，Stop 时状态保持原超时语义（`llm_timeout` / `timeout`），`passed=false`，不会把部分结论计为通过。

### 2.2 语义重复工具调用拦截

`src/enterprise_business_agent/agent/graph.py`：

- 每次成功的 `query_metric` / `trend` / `top_n` 生成覆盖记录（metric、时间范围、过滤条件、维度集合、粒度、排序方向、n、成员、完整性、Evidence ID）。
- 新调用进入 `_act` 时先做语义判定；命中则返回 `duplicate_suppressed`，**不执行查询、不消耗工具预算**，并向模型回传已有结果的摘要、Evidence ID 与成员列表，随后正常回到 `decide`。
- 过滤条件按 (dimension, operator, value) 规范化后排序比较，顺序不同不算差异。
- 拦截次数超过 `_MAX_BLOCKED_DUPLICATES = 2` 时以 `REPEATED_TOOL_CALL / repeated_tool_call` 停止，防止模型无限循环。

允许放行（不误拦）的条件：

| 场景 | 判定 |
| --- | --- |
| 时间范围不同 | 放行 |
| 粒度不同（如月度序列 vs 区间聚合） | 放行；仅当区间为单周期且 month/total 等价时视为相同 |
| 过滤条件不同（值或维度） | 放行 |
| `top_n` 排序方向相反 | 放行 |
| 请求的 n 大于已有 n | 放行 |
| 已有 `top_n` 返回满页（成员数 == n） | 不视为完整枚举，放行后续全维度查询 |
| 计算目标不同（比较、贡献、异常检测） | 不参与判重，放行 |

### 2.3 决策可观测性

- `AgentStep`：新增 `blocked_reason`、`remaining_seconds`、`remaining_tool_calls`、`retry_count`，动作新增 `call_tool_blocked`。
- `AgentStateSnapshot`：新增 `duplicate_tool_calls`、`budget_stop_reason`。
- `AgentRunResponse`：新增 `blocked_tool_calls`（默认空列表，向后兼容）。
- 评测归档：
  - `decisions` 每条包含上述字段；
  - `llm_calls` 每条包含 `budget_seconds`、`attempts`、`budget_limited`（成功事件为 `False`），原字段保持不变；
  - `tool_call_trace` 原有条目带 `blocked: false`，被拦截条目追加在各条目之后并带 `blocked: true`、`blocked_reason`；
  - `LiveEvalResult` 新增 `duplicate_tool_calls`、`budget_stop_reason`。
- `scripts/run_phase7_evals.py`：运行信息新增 `budget_stop_reason`（Markdown 表格“预算终止原因”），映射：`llm_timeout`/`timeout` → `case_time_budget_exhausted`，`budget_exhausted` → `model_or_token_budget_exhausted`。

## 3. 兼容性

- 未修改 `.env`、API Key、数据库密码或任何凭证；未打印或记录密钥。
- 未修改 Prompt 版本，仍为 `phase5-2026-10-31.2`。
- 未修改 canonical expected values，未删除测试、未降低断言、未放宽评分。
- 第一轮/第二轮归档未改动：
  - `data/exports/phase7_eval.first_round.json`
  - `data/exports/phase7_eval.second_round.json`
  - `docs/phase7/PHASE7_EVALUATION_REPORT.first_round.md`
  - `docs/phase7/PHASE7_EVALUATION_REPORT.second_round.md`
- 归档新增字段均为追加；旧归档缺少这些字段时按默认值读取。

## 4. 测试结果（全部离线）

| 命令 | 结果 |
| --- | --- |
| `.venv/bin/python -m pytest tests/test_agent_runtime.py -q` | 40 passed |
| `.venv/bin/python -m pytest tests/test_llm_client.py tests/test_glm_client.py -q` | 34 passed |
| `.venv/bin/python -m pytest tests/test_phase7_evals.py -q` | 33 passed |
| `.venv/bin/python -m pytest -q` | **578 passed, 25 skipped**（50.98 s） |
| `.venv/bin/python scripts/run_phase7_evals.py --check-only` | corpus: 92 cases；OK；no model call was made |

新增 19 个离线测试（相对 559 基线）：

- 运行时判重（10）：全维度→TopN 拦截；不同时间范围/粒度/过滤条件放行；过滤条件顺序等价；TopN 满页不判定完整；TopN 短页覆盖后续全维度查询；连续拦截达上限停止；慢调用后重试被时间预算拦下；新决策使用实测耗时估计预算。
- LLM 客户端（Anthropic 3 + GLM 3）：窗口不足时不发请求且 `attempts=0`、`budget_limited=True`；首跳超时后剩余预算装不下退避+5 s 时不重试；成功响应报告 `attempts` 且单次超时被剩余预算收紧。
- 评测归档（3）：归档包含拦截条目与新增字段；超时归档带 `budget_stop_reason`；运行脚本状态映射。

测试中的 LLM 均为脚本化假实现（TEST FAKE），没有网络请求。

## 5. 状态分级

| 项目 | 状态 |
| --- | --- |
| 客户端重试/超时受剩余预算约束 | 已修复，离线验证 |
| Agent 决策与重试的动态时间守卫 | 已修复，离线验证 |
| 语义重复调用拦截及结果复用 | 已修复，离线验证 |
| 决策/调用/预算可观测字段 | 已修复，离线验证 |
| 026 在真实模型下“提前停止并给出部分结论” | **待真实模型验证** |
| 语义判重对真实模型查询习惯的覆盖率 | **待真实模型验证** |
| 归档新增字段在真实运行中的可读性 | **待真实模型验证** |

## 6. 剩余风险

1. 语义判重仅覆盖 `query_metric`、`trend`、`top_n`；`compare_periods`、`contribution`、`anomaly` 不参与判重，重复调用仍会执行。
2. TopN 完整性由“成员数 < n”推断。若数据源恰好只有 n 个成员，结果会被视为不完整，从而允许一次冗余查询——这是保守策略，不会误拦合法查询。
3. 时间估计基于最近一次逻辑调用耗时，只能防止“明显装不下”的请求；若下一次调用突然变慢，客户端会把单次尝试压到剩余预算内，但仍可能以超时结束（保持 `llm_timeout`，不会误判为通过）。
4. 判重基于当前运行内已成功的结果；跨运行的结果缓存（tool cache）不参与语义判定。
5. 新增字段会增大归档体积（每例最多几 KB），不影响现有字段解析。

## 7. 下一轮真实评测建议（尚未启动）

1. 仍使用 `first-round-20` 预置集、GLM `GLM-5.3-Flash`、Prompt `phase5-2026-10-31.2`、单例 120 s、硬上限 200 次调用，便于与第二轮直接对比。
2. 重点观察 `phase7-026`：预期不再把预算耗在无意义的重试上，而是在剩余时间不足时停止并保留 Evidence、给出部分结论，归档 `budget_stop_reason=llm_request_timeout` 或 `case_time_budget_exhausted`。
3. 检查同一次运行中是否出现 `duplicate_suppressed` 条目，并核对被拦截调用确有对应 Evidence ID。
4. 对比第二轮：实际调用数、P95 单例耗时、超时案例是否减少；确认通过率变化来自质量而非预算提前终止。
5. 只有在 20 例轮次稳定后，再考虑完整 92 例评测。

## 8. 变更文件

| 文件 | 变更 |
| --- | --- |
| `src/enterprise_business_agent/llm/client.py` | 预算感知的尝试超时、重试退避、`attempts`/`budget_limited` |
| `src/enterprise_business_agent/llm/glm.py` | 同上，`_request` 返回尝试次数 |
| `src/enterprise_business_agent/agent/contracts.py` | 步骤/快照/响应的可观测字段 |
| `src/enterprise_business_agent/agent/graph.py` | 动态时间守卫、语义判重与拦截、覆盖记录、预算终止原因 |
| `src/enterprise_business_agent/agent/evaluation.py` | 归档 decisions/llm_calls/tool_call_trace 新字段与拦截条目 |
| `scripts/run_phase7_evals.py` | 运行级 `budget_stop_reason` 与报告表格 |
| `tests/test_agent_runtime.py` | 10 个运行时判重/预算测试 |
| `tests/test_llm_client.py` | 3 个 Anthropic 预算测试 |
| `tests/test_glm_client.py` | 3 个 GLM 预算测试 |
| `tests/test_phase7_evals.py` | 3 个归档可观测性测试 |
| `docs/phase7/PHASE7_4_COMPLETION_REPORT.md` | 本报告 |
