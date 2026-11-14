# Phase 8 完成报告：生产化能力建设

项目：`enterprise_business_agent`
日期：2026-11-03
基线：Phase 7.9 完成状态
最终状态：**Phase 8 核心交付完成；真实模型、Docker 实跑、浏览器 E2E、外部 OTLP 与 PostgreSQL 集成未在本环境验证**

---

## 1. 总体结论

Phase 8 在既有 FastAPI + LangGraph 单 Agent + AnalysisGateway + Evidence/Approval 架构上，
按 8.1 → 8.2 → 8.3 → 8.4 顺序完成了四部分增量建设：

| 阶段 | 交付 | 状态 |
| --- | --- | --- |
| 8.1 可观测性 | HTTP → Agent → LLM → 工具 → SQL → Evidence → Finding → Report → 审批全链路 span、导出器、日志脱敏、告警 | ✅ 核心完成；本地文件导出实测，OTLP 未实测 |
| 8.2 安全与集成 | scope 校验、SQL fallback 阻断、常量时间 Key 比较、安全审计、MCP 服务端工具传输 | ✅ 核心完成；MCP 客户端未做 |
| 8.3 工作台与部署 | `/workbench/`、身份/历史、报告/Evidence/审批页面、Docker/初始化/健康检查/启动文档 | ✅ 核心完成；镜像未构建、浏览器 E2E 未做 |
| 8.4 生产验收 | 五域场景全链路、数字/证据一致性、并发/幂等、超时/故障/重启、权限隔离 | ✅ 离线套件完成；真实模型未执行 |

最终全量回归：**`722 passed, 25 skipped in 66.48s`**。

本报告严格区分：

- **已实际执行**：pytest、SQLite 只读链路、本地 uvicorn HTTP 冒烟、本地文件 span 导出；
- **已实现但未在本环境执行**：Docker 镜像、浏览器 UI、外部 OTLP、PostgreSQL 集成、真实 LLM；
- **历史结果**：Phase 7.9 真实 GLM 评测，未在 Phase 8 重跑，也未修改。

---

## 2. 架构变化

### 2.1 总体架构未变

没有引入多 Agent、消息队列、微服务或第二套执行路径。核心链路仍为：

```text
FastAPI
  → AgentRuntime / LangGraph（decide → act → decide）
  → ToolRegistry（Pydantic 参数、scope、权限）
  → AnalysisGateway
  → SemanticQueryGuard
  → SafeQueryExecutor / SQL Guard
  → 只读业务数据库
  → Evidence Ledger
  → Finding Engine
  → Report Generator + Numeric Validator
  → 人工审批
```

MCP 只是外部工具传输层，调用仍回到 `ToolRegistry.execute`，没有绕过权限、Gateway 或 SQL 安全链。

### 2.2 Phase 8.1：可观测性

新增/增强：

- `http.request` span，从 W3C `traceparent` 提取上下文，并在响应回写 `X-Trace-ID` / `traceparent`；
- Agent、决策、LLM、工具、SQL、Evidence、Finding、Report、审批决策 span；
- 异步 Run 在提交时捕获 OTel context，在后台线程继续同一 trace；
- 导出器：`otlp`、`file`（JSONL）、`console`、`none`；
- `ParentBased(TraceIdRatioBased(...))` 采样；
- JSON 日志增加 `trace_id` / `span_id`，对 API Key、Authorization、密码、连接串等字段递归脱敏；
- `GET /api/v1/alerts`（仅 admin）：失败率、P95 延迟、审批积压、LLM 超时、数据库连接错误、tracing 未初始化；
- `scripts/view_traces.py` 查看本地 span 调用树。

### 2.3 Phase 8.2：安全与外部集成

新增/增强：

- 指标端点显式要求 `metrics:read`；
- 有指标/区域/指标域限制的角色不能通过 `allow_sql_fallback=true` 绕过数据权限；
- API Key 使用 `hmac.compare_digest` 常量时间比较，空 Key 不再可能匹配空配置；
- 安全审计事件：`auth_failed`、`authorization_denied`、`mcp_auth_failed`、`mcp_tool_call`、
  `mcp_tool_denied`、`mcp_protocol_error`；
- `GET /api/v1/security/audit`（仅 admin）；
- `POST /mcp` JSON-RPC 2.0：`initialize`、`ping`、`tools/list`、`tools/call`、notification、batch；
- 支持 MCP 协议版本 `2025-06-18`、`2025-03-26`、`2024-11-05`；
- MCP 工具注解：只读、非破坏、幂等、非开放世界；
- 默认关闭（`EBA_MCP_ENABLED=false`），开启后仍要求 API Key。

### 2.4 Phase 8.3：工作台与部署

新增：

- `/workbench/` 同源单页工作台，`/` 跳转；
- `GET /api/v1/me`；
- `GET /api/v1/agent/runs`：按账号隔离，`resource:read_any` 可看全部，支持状态过滤和分页；
- 工作台功能：连接身份、同步/异步提问、SSE 进度、取消、历史、运行详情、报告、Evidence、
  Finding、审批操作、结构化错误；
- API Key 只存 `sessionStorage`；业务数据用 `textContent` 渲染，不使用 `innerHTML`；
- `Dockerfile`、`.dockerignore`、`docker-compose.yml`（`postgres` / `init` / `app`）、
  `.env.docker.example`、`docs/PHASE8_DEPLOYMENT.md`；
- `pyproject.toml` 打包 `web/*` 静态资源。

### 2.5 Phase 8.4：生产验收

新增 `tests/test_phase8_production.py`，不改业务实现。覆盖：

- 五域业务场景全链路；
- 报告数字与 Evidence 引用一致性；
- 跨域证据合并；
- 8 个并发异步 Run 的唯一性与账号隔离；
- 4 线程并发幂等；
- 超时终态；
- 数据库故障；
- 模型服务故障；
- 重启中断标记；
- Evidence/Finding/Report/Approval 跨账号隔离。

---

## 3. 文件变更汇总

### 3.1 新增

| 文件 | 作用 |
| --- | --- |
| `src/enterprise_business_agent/observability/alerts.py` | 告警规则与评估 |
| `src/enterprise_business_agent/security/audit.py` | 安全审计事件 |
| `src/enterprise_business_agent/mcp/__init__.py` | MCP 包 |
| `src/enterprise_business_agent/mcp/server.py` | MCP JSON-RPC 服务端 |
| `src/enterprise_business_agent/web/index.html` | 工作台页面 |
| `src/enterprise_business_agent/web/styles.css` | 工作台样式 |
| `src/enterprise_business_agent/web/app.js` | 工作台交互与 SSE |
| `tests/test_phase8_observability.py` | Phase 8.1 测试 |
| `tests/test_phase8_security_mcp.py` | Phase 8.2 测试 |
| `tests/test_phase8_workbench.py` | Phase 8.3 测试 |
| `tests/test_phase8_production.py` | Phase 8.4 测试 |
| `Dockerfile` | 应用镜像 |
| `.dockerignore` | 镜像构建排除 |
| `.env.docker.example` | Docker 环境占位 |
| `docs/PHASE8_DEPLOYMENT.md` | 部署与启动指南 |
| `docs/phase8/PHASE8_1_REPORT.md` | 8.1 报告 |
| `docs/phase8/PHASE8_2_REPORT.md` | 8.2 报告 |
| `docs/phase8/PHASE8_3_REPORT.md` | 8.3 报告 |
| `docs/phase8/PHASE8_4_REPORT.md` | 8.4 报告 |
| `docs/PHASE8_COMPLETION_REPORT.md` | 本报告 |

### 3.2 主要修改

| 文件 | 变更 |
| --- | --- |
| `observability/setup.py` | 导出器、采样、context、flush/shutdown |
| `observability/spans.py` | 根 span context、tool span、清理 |
| `agent/graph.py` | trace context、span 顺序 |
| `api/middleware.py` | HTTP span、traceparent、日志脱敏 |
| `api/run_manager.py` | 异步 trace context |
| `api/routes.py` | alerts、me、history、metrics scope、MCP、审计 |
| `api/app.py` | MCP server、工作台静态资源、根跳转 |
| `api/run_store.py` | 历史查询、安全审计、归属过滤 |
| `security/auth.py` | 策略、常量时间比较、fallback 限制 |
| `nl/service.py` | 受限角色 fallback 阻断 |
| `config.py` | OTel、告警、MCP 配置 |
| `evidence/pipeline.py` | Evidence/Finding/Report span |
| `evidence/approval.py` | 审批 decision span |
| `docker-compose.yml` | `postgres` / `init` / `app` 编排 |
| `pyproject.toml` | 工作台静态资源打包 |
| `.env.example` | 新增配置项 |
| `.gitignore` | 忽略 trace 文件、保留 Docker 示例 |
| `README.md` | Phase 8 状态、启动、测试与限制 |
| `docs/README.md` | 能力矩阵、阶段状态、文档索引 |

---

## 4. 部署与运行

### 4.1 本地 SQLite

```bash
export PYTHONPATH=src
python scripts/init_db.py --database-url sqlite:///./enterprise_business_agent.db
python scripts/generate_data.py --database-url sqlite:///./enterprise_business_agent.db --reset
uvicorn enterprise_business_agent.api.app:app --host 127.0.0.1 --port 8000
```

工作台：`http://127.0.0.1:8000/workbench/`
健康检查：`GET /health`

### 4.2 Docker Compose

```bash
cp .env.docker.example .env.docker
# 替换全部 change-me 值
docker compose --env-file .env.docker up --build
```

初始化顺序：

```text
alembic upgrade head
→ init_db.py
→ create_readonly_role.py
→ init_runtime_store.py
→ migrate_phase5.py
→ 可选 generate_data.py --reset（EBA_SEED_DATA=true）
```

API 进程只使用：

- `eba_readonly`：业务数据只读；
- `eba_runtime`：运行状态读写。

初始化/管理连接只存在于 `init` 服务。

### 4.3 必需环境变量

| 变量 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL 容器密码 |
| `EBA_RUNTIME_PASSWORD` | 运行时角色密码 |
| `EBA_READONLY_PASSWORD` | 只读角色密码 |
| `EBA_API_KEYS` | API Key → 角色 JSON 映射 |
| `READONLY_DATABASE_URL` / `RUN_STORE_URL` | API 进程连接 |
| `EBA_OTEL_*` | 可选 tracing |
| `EBA_MCP_ENABLED` | 可选 MCP |

真实密钥必须通过环境或密钥管理系统注入，不得写入仓库、报告、命令历史或日志。

### 4.4 上线前检查

```bash
python scripts/preflight.py --env-file <env-file>
```

存在 `FAIL` 不上线。生产还缺少：

- TLS 终止与反向代理；
- 密钥管理与轮换；
- 备份与恢复演练；
- 集中日志与外部告警；
- 数据库 RLS；
- 容量与高可用方案。

---

## 5. 测试结果

### 5.1 阶段累计

| 阶段 | 新增测试 | 全量回归 |
| --- | --- | --- |
| Phase 7.9 基线 | — | 684 passed（Phase 8.前基线） |
| 8.1 | 8 | 692 passed, 25 skipped |
| 8.2 | 13 | 705 passed, 25 skipped |
| 8.3 | 4 | 709 passed, 25 skipped |
| 8.4 | 13 | **722 passed, 25 skipped** |

> 684 为 Phase 7.9 报告的基线；8.1 报告记录的回归结果为 692。

### 5.2 最终定向结果

```text
Phase 8.1 + 8.2 + 8.3 + 8.4
37 passed in 9.40s
```

### 5.3 最终全量结果

```text
722 passed, 25 skipped in 66.48s
```

25 个 skip 主要为未配置 PostgreSQL 集成环境；skip 不计为通过。

### 5.4 语料门禁

```text
corpus: 92 cases
corpus check: OK (no model call was made)
```

---

## 6. 真实运行结果

### 6.1 Phase 8 未新增真实模型运行

Phase 8.3/8.4 没有配置真实模型 Key，没有调用真实 LLM。工作台、并发、故障和全链路测试均使用：

- 真实 SQLite 只读数据库；
- 真实 LangGraph / AgentRuntime；
- 真实 FastAPI；
- 真实 Evidence/Finding/Report/Approval；
- **脚本化假 LLM**。

因此不能把 Phase 8.4 的 13 个测试称为“真实模型验收通过”。

### 6.2 Phase 7.9 历史结果保持不变

Phase 7.9 的真实 GLM 归档仍为：

- 26 条 live 用例已执行；
- 最新通过 23/26；
- 2 条提供方超时；
- 1 条内容错误；
- 684 个测试。

已知风险继续保留：

| 风险 | 状态 |
| --- | --- |
| `live-004` / `live-005` 提供方超时 | 未修复，历史记录保留 |
| `live-023` 结论越界 | 未修复，历史记录保留 |
| 模型成本单价未配置 | 未配置，不估算成本 |

Phase 8 没有覆盖、删除或美化这些失败记录。

### 6.3 本地可观测性实测

Phase 8.1 使用 `file` 导出器实际写出 JSONL span，并用 `scripts/view_traces.py` 查看调用树。
已验证 HTTP 请求和 SQL 链路进入同一 trace。

未验证：

- 真实 OTLP Collector / Jaeger / 厂商后端；
- 多实例 trace 聚合；
- 外部告警通知（PagerDuty、Alertmanager、Webhook）。

---

## 7. 性能数据

本阶段只有功能级耗时，没有容量基准：

| 项目 | 实测 |
| --- | --- |
| Phase 8.4 定向 13 测试 | 4.81s |
| 最慢并发用例 | 约 0.65s |
| Phase 8 全部定向 37 测试 | 9.40s |
| 全量 722 测试 | 66.48s |

没有以下数据：

- QPS / RPS；
- 持续并发下的 P50/P95/P99；
- 数据库连接池耗尽行为；
- 多实例租约竞争；
- 内存与 CPU 曲线；
- 真实模型下的端到端延迟分布。

不能把上述功能测试耗时当作生产容量结论。

---

## 8. 残余风险

### 8.1 环境与验证风险

1. **Docker 镜像未构建**：当前网络无法拉取 `python:3.12-slim`，`docker compose up` 未实跑。
2. **浏览器 E2E 未做**：工作台只验证了静态资源、JS 语法、API 契约和 SSE 数据流，没有真实浏览器视觉/交互验收。
3. **PostgreSQL 集成测试 skip**：缺少测试连接环境变量。
4. **OTLP 未验证**：没有外部 Collector。
5. **真实模型未重跑**：Phase 8 的新链路没有在真实 GLM 下验证。

### 8.2 功能与安全风险

1. **不是完整 RBAC/RLS**：当前是应用层角色 + scope + 区域/指标限制，不是数据库行级安全。
2. **MCP 只有服务端**：没有 MCP 客户端、SSE/Streamable HTTP。
3. **工作台认证依赖 API Key**：没有 OIDC/SSO、会话管理、CSRF 防护（当前为同源静态页 + Header Key）。
4. **审批隔离依赖应用层归属检查**：没有独立审批人组织模型。
5. **安全审计是应用层事件**：没有防篡改存储或外部 SIEM 对接。
6. **密钥轮换未实现**：API Key 轮换需要重新部署配置。
7. **成本未配置**：模型单价缺失，成本指标不完整。

### 8.3 生产运维风险

1. 单机 Docker Compose，不是高可用部署；
2. 没有自动扩缩容；
3. 没有数据库备份/恢复演练；
4. 没有集中日志、指标后端和外部告警；
5. 没有多实例指标聚合；
6. 没有容量压测和故障演练记录；
7. 没有 TLS、WAF、速率限制等边缘防护配置。

---

## 9. 生产部署限制

当前项目适合：

- 本地演示；
- 单机内网试用；
- 受控环境下的功能验收；
- 作为后续生产化的基础工程。

当前项目**不适合**直接承担：

- 公网多租户生产流量；
- 强合规场景（缺少 RLS、SSO、审计防篡改、密钥托管）；
- 高可用/容灾要求；
- 大规模并发分析；
- 无人值守的高风险经营决策执行。

生产上线前至少需要补齐：

1. 可复现的镜像构建与镜像扫描；
2. PostgreSQL 集成测试与生产连接验证；
3. TLS、反向代理、速率限制；
4. 密钥管理系统与轮换流程；
5. 数据库 RLS 或等价的行级隔离；
6. 外部 OTLP/日志/告警后端；
7. 浏览器 E2E 与真实模型回归；
8. 备份、恢复、容量和故障演练。

---

## 10. 结论

Phase 8 已把项目从“具备完整 Agent 核心能力的工程”推进到：

- 有全链路 trace、结构化日志、脱敏和告警接口；
- 有更严格的 scope、SQL fallback 阻断、安全审计和 MCP 工具传输层；
- 有业务人员可用的工作台、身份、历史、报告/Evidence/审批页面；
- 有 Docker/初始化/健康检查/日志/启动文档；
- 有覆盖五域场景、数字一致性、并发、幂等、超时、故障、重启和权限隔离的生产验收套件。

但“可部署、可观测、可安全集成、可供业务人员实际使用”目前是**工程能力完成**，
不是**生产环境验收完成**。Docker 实跑、浏览器 E2E、真实 OTLP、PostgreSQL 集成和真实模型回归
仍未在本环境执行，必须在具备条件后补测，不能标记为已通过。
