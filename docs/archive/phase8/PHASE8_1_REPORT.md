# Phase 8.1 完成报告：可观测性与运行监控

项目：`enterprise_business_agent`
阶段：Phase 8.1
日期：2026-11-03
状态：**核心交付完成；本地真实导出已验证，外部 OTLP 后端未验证**

> 本报告只记录实际执行过的内容。真实模型调用、离线测试和未执行项分开标注；
> 不把本地文件导出等同于已接入生产可观测平台，也不把进程内计数等同于后端已收到数据。

---

## 1. 阶段目标与结论

Phase 8.1 的目标是把现有“可选 OTel 接线”补成一条可实际查看的运行链路：

```text
用户请求
  → HTTP 中间件
  → Agent Run
  → Agent 决策
  → LLM 调用
  → 工具调用
  → SQL 查询
  → Evidence
  → Finding
  → Report
  → 人工审批
```

本阶段完成后：

- 上述环节都会在 tracing 初始化时生成 span，并尽量落在同一个 trace 下。
- HTTP 请求支持 W3C `traceparent` 提取与回写，异步 Run 线程通过显式 context 继续同一 trace。
- 新增 `file` 导出器，可以不依赖外部 Collector 在本地真实写出 JSONL span，并用
  `scripts/view_traces.py` 查看调用树。
- 结构化日志增加 `trace_id` / `span_id`，并对 API Key、密码、连接串、Authorization 等字段做递归脱敏。
- 新增 `GET /api/v1/alerts`（仅 admin），基于现有运行质量指标评估失败率、P95 延迟、
  审批积压、LLM 超时、数据库连接错误和 tracing 未初始化。
- 新增定向测试 8 项；全量回归 `692 passed, 25 skipped`。

**没有完成或没有验证的部分：**

- 本机没有真实 OTLP Collector / Jaeger / 厂商后端，因此“外部链路已打通”仍未验证。
- 告警只提供轮询接口，不包含 PagerDuty、Alertmanager、Webhook 等外部通知。
- 本阶段没有重新执行真实 GLM 业务评测；全链路 span 的完整性由测试证明，真实模型运行数据仍以
  Phase 7.9 归档为准。
- `/api/v1/metrics` 中 `exported_spans` 仍是进程内计数，多实例部署需要逐实例采集或由后端聚合。

---

## 2. 执行前基线

### 2.1 已有能力

执行前项目已具备：

| 能力 | 位置 | 状态 |
| --- | --- | --- |
| SpanSink 事件到 span 的映射 | `observability/spans.py` | 已有 `agent.run`、`agent.decision`、`llm.complete`、`tool.call`、`db.query`、`checkpoint.restore` |
| OTel SDK 初始化 | `observability/setup.py` | 仅支持 OTLP HTTP；无 endpoint 时如实报未配置 |
| 导出计数 | `observability/setup.py` | 只有 `SpanExporter.export` 返回 `SUCCESS` 才计数 |
| 请求 ID 与 JSON 日志 | `api/middleware.py` | 有 request_id，无 trace_id、无脱敏 |
| 运行质量指标 | `GET /api/v1/metrics` | runs/tools/model/cost/performance/validation/approvals/database/observability |
| HTTP 异常映射 | `api/app.py` | 404/422/504 与未处理异常的结构化错误响应 |

### 2.2 缺口

对照 Phase 8.1 要求，执行前缺少：

1. HTTP 请求本身没有 span，请求与 Agent trace 无法从入口关联。
2. Evidence、Finding、Report、审批决策没有 span。
3. 异步 Run 线程不继承请求线程的 context，trace 会断开。
4. 只有 OTLP 导出器，无法在没有 Collector 的环境中验证真实导出。
5. 日志没有 trace 关联，也没有统一的敏感字段脱敏。
6. 没有基于指标的告警规则和查询接口。
7. 没有本地 trace 查看工具。

---

## 3. 实现内容

### 3.1 Tracing 配置与导出器

文件：`src/enterprise_business_agent/observability/setup.py`

新增/调整：

- `TracingStatus` 增加 `exporter_target`、`sample_ratio`、`propagation`。
- 新增导出器类型：
  - `otlp`：OTLP HTTP，endpoint 自动补 `/v1/traces`。
  - `file`：`_JsonlFileSpanExporter`，每个完成 span 追加一行 JSON。
  - `console`：直接输出到 stdout。
  - `none`：初始化 SDK 但不导出，用于显式关闭。
- `TracerProvider` 增加 resource：
  - `service.name`
  - `service.version`
  - `deployment.environment`（来自 `EBA_ENV`，默认 `local`）
- 支持 `EBA_OTEL_SAMPLE_RATIO`，使用 `ParentBased(TraceIdRatioBased(...))`，保证子 span 跟随父采样决定。
- 新增上下文工具：
  - `current_otel_context()`
  - `extract_trace_context(carrier)`
  - `inject_trace_context(carrier)`
  - `current_trace_ids()`
- `span()` 现在会把 span 设为当前 context，嵌套调用自动形成父子关系；异常时标记 `ERROR` 后原样抛出。
- 新增 `traced()` 装饰器，只允许显式选择少量关键字参数写入 span 属性，避免把问题文本或凭据带上 span。
- `force_flush_tracing()` 与 `shutdown_tracing()` 支持脚本退出前刷盘。

### 3.2 HTTP 请求链路

文件：`src/enterprise_business_agent/api/middleware.py`

- `RequestContextMiddleware` 创建 `http.request` span。
- 从请求头提取 W3C `traceparent`；响应回写：
  - `X-Request-ID`
  - `X-Trace-ID`
  - `traceparent`
- span 属性包括方法、路径、状态码；异常时标记错误并记录异常类型。
- `JsonFormatter` 增加 `trace_id` / `span_id`。
- 新增递归 `redact()`：
  - 对 `api_key`、`authorization`、`password`、`secret`、`database_url`、`*_token` 等键名脱敏。
  - 对消息中的 `password=...` 形式做内联脱敏。
  - `input_tokens` / `output_tokens` 等计数字段保持可读，避免监控数据被误伤。

### 3.3 Agent 全链路 span

| 文件 | 新增 span / 改动 |
| --- | --- |
| `agent/graph.py` | `run()` / `resume()` 接受 `trace_context`；`SpanSink` 用该 context 作为根 span 父上下文 |
| `agent/graph.py` | `_finish` 调整顺序：先完成 Evidence/Finding/Report/审批，再发 `run_finished`，避免这些 span 落到另一条 trace |
| `api/run_manager.py` | 异步提交和恢复传递 `trace_context` |
| `api/routes.py` | 异步提交/恢复捕获 `current_otel_context()` 并传入后台线程 |
| `evidence/pipeline.py` | 新增 `evidence.collect`、`finding.build`、`report.generate` / `report.reuse`、`approval.ensure` |
| `evidence/approval.py` | `ApprovalStore.decide` 使用 `@traced("approval.decide", ...)`，记录报告 ID、版本、动作、审批人 |
| `analysis/engine.py` | 语义会话直连路径新增 `db.query`，记录 dialect、源表/连接表、行数等 |
| `observability/spans.py` | 根 span 以传入 context 启动；工具执行期间设置 `tool.call_id`；结束时清理 run context |

### 3.4 告警接口

文件：`src/enterprise_business_agent/observability/alerts.py`、`src/enterprise_business_agent/api/routes.py`

新增：

```http
GET /api/v1/alerts
X-API-Key: <admin-key>
```

仅 `admin` scope 可访问。规则：

| 规则 | 条件 | 严重级别 |
| --- | --- | --- |
| `run_failure_rate_high` | 终态 run 数 ≥ `EBA_ALERT_MIN_RUNS` 且失败率 > `EBA_ALERT_FAILURE_RATE` | 超过阈值 2 倍为 critical，否则 warning |
| `run_p95_latency_high` | 终态 run 数达标且 P95 > `EBA_ALERT_P95_SECONDS` | warning |
| `approvals_backlog` | pending 审批 > `EBA_ALERT_PENDING_APPROVALS` | warning |
| `llm_timeout_observed` | `performance.timeouts.llm > 0` | warning |
| `database_connection_errors` | 进程内连接错误计数 > 0 | critical |
| `tracing_not_initialized` | `otel_enabled=true` 且 `initialized=false` | warning |

响应包含每条告警的 `observed`、`threshold`、`scope` 和 `runbook`，并明确
`note="Polling interface only; no external notification was sent."`。本阶段不实现通知发送。

### 3.5 本地 trace 查看

新增 `scripts/view_traces.py`：

```bash
python scripts/view_traces.py data/traces/eba-spans.jsonl
python scripts/view_traces.py data/traces/eba-spans.jsonl --trace <trace-id> --attributes
```

工具读取 JSONL，按 trace id 分组，重建父子树，可选打印属性与事件。

### 3.6 配置与文档

`.env.example` 新增：

- `EBA_OTEL_EXPORTER=otlp|file|console|none`
- `EBA_OTEL_TRACE_FILE`
- `EBA_OTEL_EXPORTER_ENDPOINT`
- `EBA_OTEL_SAMPLE_RATIO`
- `EBA_ENV`
- `EBA_ALERT_*`

---

## 4. 本地真实导出验证

执行方式：本地启动 uvicorn，开启文件导出器，调用健康检查和自然语言分析，停止后读取 JSONL。

实际观测到的 trace 树：

```text
trace fbc5... spans=3
- http.request ... /api/v1/analysis/natural-language
  - db.query ... db.tables=region,sales_fact
  - db.query ... db.tables=region,sales_fact
```

这说明：

- HTTP 请求 span 与数据库 span 真实写出到本地文件。
- HTTP span 是父节点，`db.query` 是子节点。
- 导出不是仅存在于代码中的接线，也不是测试桩。

**限制：** 该次本地验证走的是语义分析路径，没有调用真实 LLM，因此只证明 HTTP → SQL
链路和文件导出；决策、LLM、工具、Evidence、Finding、Report、审批的同一 trace 由自动化测试验证，
见第 5 节。

---

## 5. 测试结果

### 5.1 定向测试

```text
pytest tests/test_phase8_observability.py \
       tests/test_otel_setup.py \
       tests/test_observability_spans.py \
       tests/test_phase5_agent_integration.py \
       tests/test_phase6_approvals.py \
       tests/test_api.py -q
→ 47 passed
```

新增 `tests/test_phase8_observability.py` 覆盖：

1. 文件导出器真实写出 `http.request` span，并保留传入的 `traceparent` / `X-Trace-ID`。
2. 跨线程 `SpanSink(parent_context=...)` 继续同一 trace。
3. 日志脱敏不隐藏 token 计数。
4. `JsonFormatter` 增加 trace id 并脱敏 extra 字段。
5. 告警规则覆盖失败率、P95、审批积压、LLM 超时、数据库错误、tracing 未初始化。
6. 健康小样本不误报告警。
7. 审批决策路径生成 `approval.decide` span，记录不存在时标记 `ERROR`。

`tests/test_phase5_agent_integration.py::test_one_trace_covers_decision_tool_sql_evidence_and_report`
额外断言以下 span 共享同一 trace：

```text
agent.run
agent.decision
llm.complete
tool.call
db.query
evidence.collect
finding.build
report.generate
approval.ensure
```

### 5.2 全量回归

```text
pytest -q
→ 692 passed, 25 skipped in 56.18s
```

执行前基线为 `684 passed, 25 skipped`；本阶段新增 8 个测试，数量一致。
25 个 skip 与既有环境相关（PostgreSQL 集成、真实模型等），未通过跳过伪装成功。

---

## 6. 异常分类与日志

本阶段没有新增独立的异常分类模块，沿用并补强现有机制：

| 场景 | 表现 |
| --- | --- |
| 请求参数错误 | HTTP 422 + `invalid_request` + 结构化 errors |
| 资源不存在 | HTTP 404 + `not_found` |
| 超时 | HTTP 504 + `timeout` |
| 未处理异常 | HTTP 500 + `execution_error`；日志记录 `error_type`，不返回堆栈和内部细节 |
| span 内异常 | span 状态设为 `ERROR`，异常原样向上抛出，不吞错 |
| 告警 | 将运行指标映射为 `critical` / `warning` / `ok` |

日志中不打印 API Key、数据库密码、完整连接串；错误消息只保留类型和必要的业务字段。

---

## 7. 未验证项与风险

1. **无真实 OTLP 后端。** `otlp` 导出器已实现 endpoint 规范化，但没有对真实 Collector 做过网络导出验证；
   不能声称 Jaeger/Tempo/厂商平台已接入。
2. **告警无通知通道。** 只有 `GET /api/v1/alerts` 轮询接口，生产环境需要外部监控系统转发。
3. **导出计数是进程内。** 多 worker 部署时每个进程独立计数，不能相加当作全局导出量。
4. **采样可能丢弃 span。** 设置 `EBA_OTEL_SAMPLE_RATIO < 1` 时，未采样 trace 不会导出；这是配置预期，不是故障。
5. **本阶段未执行真实 LLM 评测。** 全链路 span 的父子关系由测试验证；真实模型延迟、token 与失败模式仍以
   Phase 7.9 归档和后续 Phase 8.4 测试为准。
6. **本地 trace 文件会增长。** `file` 导出器是开发/排障用途，生产应使用 OTLP 并配置采集端轮转；
   本阶段未实现文件轮转。
7. **敏感数据脱敏是字段名与常见内联模式驱动。** 业务字段中若出现自由文本形式的敏感信息，仍需调用方避免写入日志。

---

## 8. 文件清单

新增：

- `src/enterprise_business_agent/observability/alerts.py`
- `scripts/view_traces.py`
- `tests/test_phase8_observability.py`
- `docs/phase8/PHASE8_1_REPORT.md`

修改：

- `src/enterprise_business_agent/config.py`
- `src/enterprise_business_agent/observability/setup.py`
- `src/enterprise_business_agent/observability/spans.py`
- `src/enterprise_business_agent/agent/graph.py`
- `src/enterprise_business_agent/api/app.py`
- `src/enterprise_business_agent/api/middleware.py`
- `src/enterprise_business_agent/api/routes.py`
- `src/enterprise_business_agent/api/run_manager.py`
- `src/enterprise_business_agent/analysis/engine.py`
- `src/enterprise_business_agent/evidence/pipeline.py`
- `src/enterprise_business_agent/evidence/approval.py`
- `tests/test_phase5_agent_integration.py`
- `.env.example`
- `README.md`

---

## 9. 下一阶段前置条件

Phase 8.2 开始前需要先审查现有 RBAC、数据权限、API 认证、审计日志、高风险审批和 SQL Guard，
再决定 MCP 外部工具层的接入点。MCP 必须复用 `AnalysisGateway`、权限检查与 SQL 安全链路，
不能成为绕过这些控制的旁路。
