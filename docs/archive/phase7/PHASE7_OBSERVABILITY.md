# Phase 7 可观测性说明

项目：`enterprise_business_agent`
版本：`0.6.0`
最后更新：2026-10-31

> 本文说明 `/api/v1/metrics` 与 OpenTelemetry 的**实际能力、字段口径和未验证部分**。
> 所有数字来自运行时数据库或进程内计数，不来自估算，除非字段显式标注为估算。

---

## 1. 数据来源

| 数据源 | 内容 | 范围 |
| --- | --- | --- |
| `agent_runs` | 状态、执行时长、恢复次数、run payload（token、模型延迟、工具调用） | 共享数据库 |
| `agent_audit_events` | 模型响应、重试、错误、工具事件 | 共享数据库 |
| `agent_tool_results` | 工具调用状态（按 run 聚合） | 共享数据库 |
| `agent_evidence` / `agent_findings` | 业务域、证据与发现 | 共享数据库 |
| `agent_reports` | Numeric Validator 校验结果 | 共享数据库 |
| `agent_report_approvals` | 审批状态与等待时长 | 共享数据库 |
| 进程内 tracing 计数器 | 导出/丢弃 span 数 | 单进程 |

`scope` 字段固定为 `shared_database`，表示指标跨 worker 聚合；`exported_spans` 例外，它是进程内计数。

---

## 2. 端点与认证

```http
GET /api/v1/metrics
X-API-Key: <admin-key>
```

需要 `admin` 角色。返回结构：

```text
generated_at
scope
runs        { total, by_status, completed, failed, terminal,
              success_rate, failure_rate, avg_execution_seconds,
              p95_execution_seconds, resumed_runs, resume_count,
              resume_success_rate }
tools       { total_calls, avg_calls_per_run, by_status, failure_rate }
model       { calls, calls_per_run, retries, errors,
              avg_latency_ms, p95_latency_ms,
              input_tokens, output_tokens, total_tokens }
cost        { currency, price_source,
              input_price_per_million_tokens, output_price_per_million_tokens,
              estimated, estimated_total, estimated_per_completed_run,
              basis, note }
performance { model_calls_per_run { total, avg, p95, max, scope },
              avg_model_latency_ms, p95_model_latency_ms, model_latency_scope,
              avg_tool_duration_ms, p95_tool_duration_ms, tool_duration_scope,
              avg_tool_calls_per_run,
              timeouts { runs, llm, tool_calls },
              retries { llm, tool, tool_note },
              by_domain { <domain>: { runs, completed, success_rate,
                                      avg_execution_seconds,
                                      p95_execution_seconds, tokens } },
              domain_scope }
checkpoints { count, storage_bytes, avg_bytes }
validation  { reports, validated, pass_rate, numeric_interceptions,
              unsourced_numbers, evidence_runs, evidence_coverage, findings }
approvals   { total, by_state, pending,
              avg_pending_wait_seconds, avg_decision_wait_seconds }
database    { backend, connection_errors, connection_errors_scope }
observability { ... }   # 见第 5 节
```

---

## 3. 运行质量指标

| 字段 | 口径 |
| --- | --- |
| `runs.success_rate` | `completed + completed_with_warnings` / 终态 runs |
| `runs.failure_rate` | 失败终态 / 终态 runs |
| `runs.avg_execution_seconds` | 终态且时长 > 0 的 runs 均值；缺时长时回退到 payload 时间戳 |
| `runs.p95_execution_seconds` | 同上集合的 P95 |
| `runs.resume_success_rate` | 恢复过的 runs 中最终完成的比例 |
| `tools.failure_rate` | 非 `ok` 工具结果 / 全部工具结果 |
| `validation.pass_rate` | `status=validated` 报告 / 全部报告 |
| `validation.numeric_interceptions` | 校验问题中数字类问题数量 |
| `validation.unsourced_numbers` | 无来源数字数量 |
| `validation.evidence_coverage` | 有 Evidence 的 completed runs / completed runs |
| `approvals.avg_pending_wait_seconds` | 当前 pending 审批已等待时长均值 |
| `approvals.avg_decision_wait_seconds` | 已决策审批从请求到决策的时长均值 |

---

## 4. 性能与成本

### 4.1 模型调用与延迟

- `performance.model_calls_per_run`：按 `run_id` 统计 `model_response` 审计事件，输出总数、均值、P95、最大值。
- `performance.avg_model_latency_ms` / `p95_model_latency_ms`：每个 run payload 中 `llm_latency_ms` 的均值/P95，口径是**单 run 内模型调用延迟之和**，不是单次调用延迟。
- `model.avg_latency_ms` / `model.p95_latency_ms` 与上面同源。

### 4.2 工具耗时

- 来源：run payload 中每个 `tool_calls[].duration_ms`。
- 输出所有工具调用的均值与 P95。
- 工具内部重试次数**没有单独持久化**，因此 `performance.retries.tool = null`，并附 `tool_note`。LLM 重试来自 `llm_retry` 事件。

### 4.3 超时

| 字段 | 含义 |
| --- | --- |
| `timeouts.runs` | 状态为 `timeout` 的 runs |
| `timeouts.llm` | 状态为 `llm_timeout` 的 runs |
| `timeouts.tool_calls` | 工具结果状态为 `timeout` 的调用数 |

### 4.4 分业务域

- 通过 `agent_evidence.domain` 关联到 run，再按域统计 runs、完成数、成功率、平均/P95 执行时长和 token。
- 一个 run 涉及多个域时会在每个域各计一次；`domain_scope=evidence_domain_joined_to_runs` 明确该口径。
- 无 domain 的证据归入 `unspecified`。

### 4.5 成本估算

```text
estimated_total =
  input_tokens  / 1_000_000 * EBA_MODEL_INPUT_PRICE_PER_MTOKENS
+ output_tokens / 1_000_000 * EBA_MODEL_OUTPUT_PRICE_PER_MTOKENS
```

- 只有两个单价都配置时才计算；否则 `estimated=false`、`estimated_total=null`。
- `price_source` 原样显示配置值（默认 `unconfigured`），避免把默认值误认为合同价。
- `basis=run_store_token_count_x_configured_price`，`note` 明确"不是供应商账单"。
- 单价环境变量：

```bash
EBA_MODEL_INPUT_PRICE_PER_MTOKENS=<number>
EBA_MODEL_OUTPUT_PRICE_PER_MTOKENS=<number>
EBA_MODEL_PRICE_SOURCE='contract-2026-q4'
EBA_MODEL_PRICE_CURRENCY='CNY'
```

---

## 5. OpenTelemetry

### 5.1 开启方式

```bash
pip install -e '.[otel]'
export EBA_OTEL_ENABLED=true
export EBA_OTEL_EXPORTER_ENDPOINT='http://<collector>:4318/v1/traces'
export EBA_OTEL_SERVICE_NAME='enterprise-business-agent'
```

未安装 SDK 时应用照常运行，`observability.otel_sdk_available=false`，不创建 span。

### 5.2 Span 结构

| Span | 关键属性 |
| --- | --- |
| `agent.run`（根） | `agent.run_id`、provider、prompt_version、最终状态、stop_reason、token、latency |
| `agent.decision` | 轮次、剩余预算、已用 token |
| `llm.complete` | 模型、延迟、输入/输出 token、工具调用数 |
| `tool.call` | 工具名、`tool.call_id`、参数、状态、耗时、Evidence 数、错误码 |
| `db.query` | dialect、表、SQL hash、行数、是否截断、耗时；携带 `agent.run_id` 与 `tool.call_id` |
| `checkpoint.restore` | thread_id、是否找到、checkpoint_id、pending writes、恢复耗时；未找到标记错误 |

关联方式：

- 根 span 携带 `run_id`。
- 工具执行线程复制 contextvars，`tool.call` 期间的 `db.query` 落在同一 trace。
- 事件与 span 都使用 `tool_call_id`，可从运行记录反查工具调用。

### 5.3 诚实状态

`/api/v1/metrics` 的 `observability` 字段：

| 字段 | 含义 |
| --- | --- |
| `otel_enabled` | 配置是否开启 |
| `otel_sdk_available` | SDK 是否可导入 |
| `exporter_configured` / `exporter_kind` | 是否配置 OTLP endpoint |
| `initialized` / `init_error` | 初始化结果与错误 |
| `exported_spans` / `dropped_spans` | 仅当 `SpanExporter.export` 返回 `SpanExportResult.SUCCESS` 时计成功 |
| `exported_spans_scope` | 固定 `process_local` |
| `note` | 当前状态说明 |

**当前验证状态：**

- 已用本地 provider 验证 span 生成、父子关系、属性与成功/失败计数。
- **没有接入真实 OTLP Collector 或厂商后端**，因此"外部链路已打通"未验证。
- 只有 `exported_spans` 实际增长，才能声称导出成功；进程内计数不能证明后端收到数据。

---

## 6. 测试覆盖

| 测试 | 覆盖 |
| --- | --- |
| `tests/test_phase6_metrics.py` | runs/tools/model/validation/approvals、成本估算、延迟、分域、超时、重试口径 |
| `tests/test_observability_spans.py` | span 生成、属性、错误标记、导出计数；LLM 延迟/token 属性；工具耗时缺失时**不写入** `tool.duration_ms`（不写 `null`，也不推断） |
| `tests/test_otel_setup.py` | 未安装/未配置/初始化失败的诚实状态 |
| `tests/test_phase7_1_preflight.py` | 归档成本块：未配置单价时 `estimated=false`、`estimated_total=null`、`estimated_per_case=null`，而**已实测的 token 仍照常统计** |

最近一次离线执行（2026-10-31，Phase 7.2）：`pytest -q` → `544 passed, 25 skipped`；
`EBA_OTEL_ENABLED=1` 与 `=0` 时全量均为 `544 passed, 25 skipped`，观测专项
`tests/test_observability_spans.py tests/test_otel_setup.py -q` 为 `14 passed`。

### 6.1 未知数据的 null 口径（Phase 7.2 复核）

| 数据 | 未测得时 | 说明 |
| --- | --- | --- |
| 工具耗时 | 属性不存在 | 不写 `null`、不推断；只有事件确实带 `duration_ms` 才写入 |
| 模型延迟 / token | 字段缺失或 `null` | 只统计真实事件；未执行用例不进汇总 |
| 成本 | `estimated_total = null` | 需同时配置两个单价；token 数仍按实测输出 |
| 工具重试次数 | `performance.retries.tool = null` | 未持久化，附 `tool_note` |

trace 关联（`agent.run` → `agent.decision` → `llm.complete` / `tool.call`，以及
`db.query`、`checkpoint.restore`）与本轮改动无关，未修改；仅复核了 span 属性写入逻辑。

### 6.2 首轮真实运行的计量数据（2026-10-31）

来源是评测归档 `data/exports/phase7_eval.first_round.json`（评测器直接计量），
**不是** OTel 导出数据；本机仍没有 OTLP 后端。

| 数据 | 实测值 |
| --- | --- |
| 模型调用 | 28（预算 200，硬上限拒绝 0） |
| 输入 / 输出 token | 170234 / 5585 |
| LLM 平均延迟 | 26109.3 ms |
| 单例 P50 / P95 | 19851.2 / 60002.7 ms |
| wall time | 292.1 s |
| 计划外 / 重复工具调用 | 6 / 0 |
| 成本 | `null`（未配置单价） |

可核对的口径：28 次调用等于 11 条用例的 `model_calls` 之和
（2+2+2+3+2+2+4+2+3+2+4）；token 与延迟只统计已执行用例，9 条未执行不计入。
工具重试次数仍为 `null`（未持久化）。

---

## 7. 已知限制

1. `exported_spans` 是进程内计数；多实例需要逐实例采集或由 OTLP 后端聚合。
2. 工具重试次数未持久化，只能显示 `null` 并说明原因。
3. 模型延迟是 run 内求和，不是单次调用直方图；需要单次分布时应解析 `model_response` 事件。
4. 分域统计允许一个 run 跨多个域重复计数，适合定位域成本，不等同于互斥分组。
5. 成本是配置单价 × token 数，不包含缓存折扣、阶梯价、失败重试的额外费用，也不等于账单。
6. 没有真实 OTLP 后端时，trace 只在本地生成，不能声称外部可观测平台已接入。

### 6.3 Phase 7.3：评测归档新增的调用轨迹（2026-10-31）

评测 JSON 的每条记录现在额外包含三个块，全部来自评测器的真实计量，
不读取 OTel 后端，也不做推断：

| 字段 | 内容 | 缺失时 |
| --- | --- | --- |
| `decisions` | 每次决策的动作（`call_tool`/`finish`）、工具名、决策理由、决策序号 | 未执行用例为空列表 |
| `llm_calls` | 每次模型调用的序号、状态（`ok` 或异常类型）、`duration_ms`、输入/输出 token、候选工具、是否带文本 | 未调用模型时为空列表 |
| `tool_call_trace` | 每次工具调用的工具名、状态、`duration_ms`、`evidence_ids`、错误码 | 未调用工具时为空列表 |

口径说明：

- `llm_calls` 只统计真正发出的调用；被预算拒绝的调用不计入（与 `model_calls` 一致）。
- `tool_call_trace[].duration_ms` 来自工具返回事件；没有该事件时不写。
- `evidence_ids` 是本次调用产生的 Evidence 主键列表，供离线核对数字来源。
- 这些字段**不替代**第 3–4 节的聚合指标，也不进入通过判定。

最近一次离线回归（Phase 7.3）：`pytest -q` 与 `EBA_OTEL_ENABLED=1 pytest -q`
均为 `559 passed, 25 skipped`；本轮未执行真实模型评测。
