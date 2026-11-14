# Phase 6 最终验收报告

日期：2026-10-31
项目：`enterprise_business_agent`
版本：`0.6.0`
范围：Phase 6 收尾与工程可靠性完善（模型截断/叙述失败、异常处理、OpenTelemetry、生产部署文档与只读 Preflight）

> 本报告只记录本轮实际执行的命令与结果。脚本化模型的测试结果与真实 GLM 结果分开陈述；本机完成的是 SQLite 全量回归、真实 PostgreSQL 测试库集成和本地 API 探测，**不是真实生产服务器部署**。本轮未读取、打印或修改任何 API Key、数据库密码或完整连接串，未执行密钥轮换。

---

## 1. 结论摘要

| 收尾项 | 状态 | 结论 |
| --- | --- | --- |
| 真实 GLM 供应链/跨域叙述截断 | 代码已修复 | 两个 Provider 均透出 `finish_reason`；报告叙述按段落生成、逐段校验、失败回退并显式告警 |
| 叙述生成失败 | 代码已修复 | 结构化报告、Evidence、Finding、Numeric Validator 不受影响；`narrative_mode` 标记 `llm` / `partial` / `deterministic` |
| 最终结论被截断 | 代码已修复 | 有界续写一次；续写失败保留部分正文并写入警告，不伪装成完整报告 |
| 异常处理与确定性测试 | 完成 | 截断、空响应、格式错误、超时、连接失败、认证失败、非法工具名、非法参数、多工具调用均有测试 |
| OpenTelemetry | 完成（本机可变现验证） | 可选 SDK/TracerProvider/OTLP Exporter；覆盖 Run/决策/模型/工具/数据库/Checkpoint；只有成功导出才计数；7 项测试通过 |
| 真实 OTLP 后端链路 | **未验证** | 本机没有 Collector/厂商后端，未导出任何真实 trace；报告中不做"已接入"陈述 |
| 生产部署文档 | 完成 | `docs/PRODUCTION_DEPLOYMENT.md` |
| 只读 Preflight | 完成 | `scripts/preflight.py`，默认只读，连接串自动脱敏 |
| 真实 GLM 五场景复测 | **本轮未执行** | 现有 `.env` 存在但本轮未获得 Key 状态人工确认，按约束不调用真实模型 |

**总体判定：Phase 6 遗留的工程质量项已完成并有确定性测试证据；真实模型复测和生产部署仍需人工执行，不能视为已上线。**

---

## 2. 实际修改

### 2.1 模型客户端

| 文件 | 修改 |
| --- | --- |
| `src/enterprise_business_agent/llm/client.py` | `LLMCallResult` / `LLMToolResponse` 增加 `finish_reason`、`truncated`；新增 `LLMTruncated`、`is_truncated`；`complete_json` / `complete_with_tools` 支持单次 `max_tokens` 覆盖 |
| `src/enterprise_business_agent/llm/glm.py` | 读取 `choices[0].finish_reason`；JSON 不完整且被截断时抛 `LLMTruncated`；工具响应标记 `truncated` |
| `src/enterprise_business_agent/llm/__init__.py` | 导出 `LLMTruncated`、`is_truncated` |
| `src/enterprise_business_agent/config.py` | 新增 `phase5_narrative_max_tokens`、`agent_final_continuation_max_tokens`、`otel_exporter_endpoint` 及环境变量绑定 |

### 2.2 报告叙述

| 文件 | 修改 |
| --- | --- |
| `src/enterprise_business_agent/evidence/report.py` | 执行摘要与各域分别生成、分别校验；通过校验的段落合并，失败段落保留确定性文本并生成明确警告；`max_fix_attempts` 对每段生效；异常映射为 `timeout` / `truncated` / `unavailable` / `invalid_json` |
| `src/enterprise_business_agent/evidence/contracts.py` | `narrative_mode: deterministic \| llm \| partial` |
| `src/enterprise_business_agent/evidence/pipeline.py` | 透传 `narrative_max_tokens` |

### 2.3 Agent 最终结论

| 文件 | 修改 |
| --- | --- |
| `src/enterprise_business_agent/agent/graph.py` | 检测最终结论截断；在剩余预算内续写一次；续写失败保留部分正文并写入警告；新增 trace 事件 |
| `tests/agent_fakes.py` | `ScriptedLLM` 记录 `max_tokens`；新增 `truncated_response` |

### 2.4 OpenTelemetry

| 文件 | 修改 |
| --- | --- |
| `src/enterprise_business_agent/observability/setup.py`（新增） | 可选 SDK/`TracerProvider`/`BatchSpanProcessor`/`OTLPSpanExporter`；`TracingStatus` 记录真实状态；只有 `SpanExportResult.SUCCESS` 才增加 `exported_spans`；初始化/导出异常不影响业务；`span()`、`set_run_context`、`current_tracer`、`reset_tracing` |
| `src/enterprise_business_agent/observability/spans.py` | `tool.call` 执行期间设为当前 span，使数据库查询共享 trace；新增 `checkpoint.restore` span；工具 span 结束时正确 detach |
| `src/enterprise_business_agent/security/executor.py` | 每次受控查询生成 `db.query` span（dialect、表、SQL hash、行数、截断、耗时） |
| `src/enterprise_business_agent/agent/tools.py` | 工具线程继承上下文（`contextvars.copy_context()`），run_id/tool_call_id 不丢失 |
| `src/enterprise_business_agent/api/app.py` | 启动时初始化 tracing，`api_started` 日志记录真实状态 |
| `src/enterprise_business_agent/api/run_metrics.py` | `observability` 改为读取实时 `tracing_status()`，不再硬编码 |
| `pyproject.toml` | 新增可选依赖组 `otel` |

### 2.5 部署与检查

| 文件 | 修改 |
| --- | --- |
| `scripts/preflight.py`（新增） | 只读检查：PG 连接、角色权限边界、业务表、Run Store、Checkpoint、Claim/Lease、清理策略、TLS、密钥管理、日志、OTel、健康/指标/审批端点；连接串自动脱敏 |
| `docs/PRODUCTION_DEPLOYMENT.md`（新增） | 角色、初始化、备份恢复、TLS、密钥、多进程、指标、OTel、上线清单与残留风险 |
| `tests/test_otel_setup.py`、`tests/test_preflight.py`（新增） | 覆盖 OTel 开关/缺 SDK/缺 Endpoint/成功与失败导出/trace 关联/Checkpoint span；Preflight 脱敏与只读 JSON |

---

## 3. 实际测试命令与结果

| 命令 | 结果 | 说明 |
| --- | --- | --- |
| `.venv/bin/python -m pytest -q` | **492 passed, 25 skipped** | 全量 SQLite；含新增 OTel/Preflight 测试 |
| 第一阶段定向套件（10 个文件，见下） | **117 passed** | 模型客户端、报告叙述、数字校验、Agent 运行时、集成、OTel、指标、Preflight |
| Phase 6 SQLite 定向（7 个文件） | **48 passed, 2 skipped** | 运行时、审批、清理、指标、工作台、权限、切换 |
| PG：`test_postgres_integration` + `test_phase5_postgres` + `test_phase6_postgres_runtime` + `test_phase6_postgres_batch3` | **23 passed** | 真实 PostgreSQL 16 一次性容器；非 Mock、非 SQLite 替代 |
| `tests/test_otel_setup.py` | **7 passed** | 0.03s；含成功/失败导出计数与 trace 关联 |
| `tests/test_preflight.py` | **3 passed** | 脱敏、只读 JSON、状态汇总 |

第一阶段定向套件：

```bash
.venv/bin/python -m pytest \
  tests/test_llm_client.py tests/test_glm_client.py \
  tests/test_report_generator.py tests/test_numeric_validator.py \
  tests/test_agent_runtime.py tests/test_phase5_agent_integration.py \
  tests/test_observability_spans.py tests/test_otel_setup.py \
  tests/test_phase6_metrics.py tests/test_preflight.py -q
```

PostgreSQL 测试使用隔离容器（端口 `55432`，一次性数据库），测试结束后删除；不是生产库。

---

## 4. 五场景回归

### 4.1 确定性演示（本轮实际执行）

```bash
.venv/bin/python -m pytest tests/test_demo_results.py -q   # 6 passed
```

覆盖销售、营销、财务、供应链、人力五个业务域及对比/归因链路；结果来自本地数据与确定性计算，不是模型生成的数字。

### 4.2 真实 GLM 五场景（上一轮 Phase 6 验收记录，本轮未复测）

| 场景 | 状态 | 执行时间 | 工具数 | 警告 |
| --- | --- | --- | --- | --- |
| 销售 | `completed` | 62.87s | 2 | 无 |
| 营销 | `completed` | 85.49s | 3 | 无 |
| 供应链 | `completed_with_warnings` | 137.33s | 3 | 正文截断 |
| 跨域 | `completed_with_warnings` | 104.98s | 6 | 正文截断 |
| 不支持预测 | `completed` | 54.15s | 1 | 无 |

**本轮未重新调用真实 GLM。** 原因是本轮没有获得"当前 Key 状态已人工确认"的明确结论，按安全约束不自动使用真实密钥。截断修复目前有确定性测试证据，但**真实模型下的叙述完整性需要人工确认 Key 后复测**；这是本报告最主要的残留风险。

---

## 5. 异常与截断测试矩阵

| 场景 | 覆盖位置 | 行为 |
| --- | --- | --- |
| Anthropic 输出截断 | `test_llm_client.py::test_truncated_json_is_reported_as_truncation_not_generic_error` | `LLMTruncated`，不混同格式错误 |
| GLM 输出截断 | `test_glm_client.py::test_glm_truncated_json_is_reported_as_truncation` | 同上 |
| 非截断的非法 JSON | `test_glm_client.py::test_glm_non_truncated_invalid_json_stays_a_format_error` | 保持 `LLMInvalidResponse` |
| 工具响应截断 | 两个客户端的 `..._truncated_tool_response_is_flagged` | `truncated=True`，`stop_reason` 保留 |
| 最终结论截断后续写 | `test_agent_runtime.py::test_truncated_final_answer_is_continued_once` | 只续写一次，拼接正文 |
| 续写失败 | `test_agent_runtime.py::test_failed_continuation_keeps_partial_answer_and_warns` | 保留部分正文 + 警告 |
| 报告叙述整体失败 | `test_report_generator.py::test_unavailable_narrative_falls_back_with_accurate_warning` | 确定性文本 + 明确警告 |
| 报告叙述部分失败 | `test_report_generator.py::test_partial_narrative_keeps_valid_sections_and_warns` | 通过校验的段落保留，`partial` |
| 报告叙述截断 | `test_report_generator.py::test_truncated_narrative_is_reported_as_truncation` | 警告包含"截断" |
| 叙述中出现编造数字 | `test_report_generator.py::test_invented_number_in_narrative_is_rejected_and_falls_back` | 该段拒绝，回退确定性文本 |
| 空模型响应 | `test_agent_runtime.py::test_empty_model_response_is_retried_once_then_recovers` / `..._stops_after_one_retry` | 重试一次后终态 |
| 模型超时 | `test_agent_runtime.py::test_llm_timeout_returns_structured_state` | 结构化终态 |
| 模型不可用 | `test_agent_runtime.py::test_llm_unavailable_returns_structured_state` | 结构化终态 |
| 连接失败 | `test_llm_client.py::test_connection_failure_becomes_structured_unavailable_after_retries` | 重试后 `LLMUnavailable` |
| 认证失败 | 两个客户端的 `401` / `403` 测试 | 不重试，密钥脱敏 |
| 非法工具名 | `test_agent_security_boundaries.py`、`test_governance.py::test_unknown_tool_request_cannot_reach_database` | `stop_reason="unknown_tool"`，不触库 |
| 非法参数 | `test_agent_runtime.py::test_invalid_tool_arguments_are_fed_back_and_recovered` | 回灌错误并恢复 |
| 连续非法参数 | `..._stop_at_failure_circuit_breaker` | 熔断 |
| 多工具调用 | `test_agent_runtime.py::test_multiple_tool_calls_in_one_turn_run_only_the_first` | 只执行第一个并告警 |

---

## 6. OpenTelemetry 状态（如实记录）

本轮在本地虚拟环境安装了可选依赖 `opentelemetry-sdk` 与 `opentelemetry-exporter-otlp-proto-http`，因此：

- 未开启（默认）：`otel_enabled=false`，不创建、不导出任何 span；`/api/v1/metrics` 的 `observability.note` 明确说明原因。
- 开启但缺 Endpoint：`initialized=false`、`init_error="no OTLP endpoint configured"`。
- 缺 SDK：`sdk_available=false`，业务不受影响。
- 注入可控 Exporter 时：只有 `export()` 返回 `SUCCESS` 才增加 `exported_spans`；失败计入 `dropped_spans`。
- trace 关联：`tool.call` 与嵌套的 `db.query` 共享同一 `trace_id`，`db.query` 携带 `agent.run_id` 与 `tool.call_id`。
- `checkpoint.restore`：记录 thread_id、是否找到、checkpoint_id、pending writes、耗时；未找到标记为 ERROR。

**没有部署 Collector 或厂商后端，因此没有验证真实网络导出。** `exported_spans` 是进程内计数，需要真实后端和运行中的服务共同确认。

---

## 7. 生产就绪度

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| PostgreSQL 连接与角色边界 | 已验证（测试库） | `eba_test_readonly` 无 DDL、无业务表 DML；`eba_b2_runtime` 无 DDL，仅 `agent_*` DML |
| 运行时表结构 | 已验证 | 10 张运行时表、Claim/Lease 列、Checkpoint 表 |
| Preflight（SQLite 本地） | 可运行 | 3 个 FAIL 均为"本地未配置 API Key / LLM Key / 允许匿名"，符合本地开发预期 |
| Preflight（PG + API） | **20 ok, 6 warn, 0 fail** | 真实 PG 只读/运行时角色 + 本地 API 探测 |
| 部署文档 | 完成 | `docs/PRODUCTION_DEPLOYMENT.md` |
| 真实生产服务器 | **未执行** | 未连接、未修改 |
| 生产 `.env` / 角色切换 | **未执行** | 保留本地开发环境 |
| 真实 OTLP 链路 | **未验证** | 无后端 |
| 备份恢复演练 | **未执行** | 文档给出步骤，需人工在隔离环境执行 |

---

## 8. 剩余风险与人工事项

1. **真实 GLM 复测**：需人工确认 Key 状态后，重跑供应链与跨域两个场景，确认叙述不再截断或能正确降级为带警告的确定性文本。
2. **密钥轮换**：GLM API Key 轮换仍是独立人工上线事项；本轮未读取、未修改、未轮换。
3. **真实生产部署**：角色创建、`.env` 切换、TLS、反向代理、监听地址、多进程参数均需按变更流程执行。
4. **OTLP 后端**：接入 Collector 后，需观察 `metrics.observability.exported_spans` 是否增长，并核对 trace 中的 `run_id` / `tool_call_id`。
5. **容量与故障演练**：未做压测、未做多机灾备；Lease 参数需要按真实 Run 时长调整。
6. **叙述质量**：分段生成降低了截断概率，但模型叙述仍需人工抽检；Numeric Validator 只保证数字可追溯，不保证文字质量。
