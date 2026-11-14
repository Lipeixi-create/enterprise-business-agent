# 生产部署手册

项目：`enterprise_business_agent`
适用版本：`0.6.0`
最后更新：2026-10-31

> 本文是**部署手册**，不是部署完成证明。当前仓库的状态是：PostgreSQL 运行时角色、Checkpoint、Claim/Lease、审批 API、指标和只读 Preflight 均已在本地 PostgreSQL 演练环境和真实测试库上验证；**未连接、未修改任何真实生产服务器，未切换生产 `.env`，未创建生产角色，未执行密钥轮换，也未验证真实 OTLP 后端链路**。这些事项列在文末清单中，由人工按变更流程执行。
>
> 本文不包含任何密码、API Key、Token 或完整连接串。示例中的 `<redacted>` 必须由部署人员从密钥管理系统注入，禁止写入仓库、日志或工单。

---

## 1. 部署形态与约束

保持现有架构不变：

- 单 Agent + LangGraph（`decide → act → decide`）+ Function Calling + Tool Registry。
- 模型只提出工具调用；代码负责参数校验、权限校验、Guard 拦截和确定性执行。
- 业务查询只走 `CompiledQuery → SemanticQueryGuard → TrustedSelect → SafeQueryExecutor`，只读。
- 人工审批只控制"报告发布状态"，不会让模型获得写业务库的能力。
- 不引入 Kafka、Kubernetes、微服务或新的 Agent 编排层。

生产需要两个独立数据库连接池：

| 连接 | 环境变量 | 角色 | 用途 |
| --- | --- | --- | --- |
| 业务只读连接 | `READONLY_DATABASE_URL` | `eba_readonly` | 语义/SQL 查询，SELECT only |
| 运行时连接 | `RUN_STORE_URL` | `eba_runtime` | Run、Event、Tool Result、Checkpoint、Evidence、Finding、Report、Approval |
| 初始化连接（仅运维进程） | `EBA_RUNTIME_SETUP_URL` | `eba_admin` | 建表、授权、迁移；**不得出现在 API 进程环境** |

API 进程只需要前两个连接。`EBA_RUNTIME_SETUP_URL` 和 `EBA_RUNTIME_PASSWORD` 只用于 `scripts/init_runtime_store.py` 的一次性执行。

---

## 2. 部署前检查：只读 Preflight

```bash
# 只读；不执行任何 DDL/DML，不打印密码
.venv/bin/python scripts/preflight.py --env-file /etc/eba/production.env

# 同时探测已启动的 API
.venv/bin/python scripts/preflight.py --api-base-url https://eba.internal --json
```

检查项与失败含义：

| 检查项 | 失败含义 |
| --- | --- |
| `env.file_permissions` | `.env` 可被同组/其他用户读取；应 `chmod 600` |
| `env.anonymous_api` | `EBA_ALLOW_ANONYMOUS_API` 仍为 true |
| `env.api_keys` | 未配置任何调用方密钥 |
| `env.llm_key` | 未配置模型密钥，Agent 会直接返回 `llm_unavailable` |
| `env.llm_endpoint` | Provider 协议与 Base URL 不匹配（Anthropic 需 `/v1/messages`，GLM 需 `/api/paas/v4`） |
| `database.connection` | 业务只读库连不上 |
| `database.role_attributes` | 只读角色是 superuser/createdb/createrole/bypassrls |
| `database.no_ddl` | 只读角色能在 `public` schema 建对象 |
| `database.readonly_grants` | 只读角色对业务表有 INSERT/UPDATE/DELETE/TRUNCATE |
| `run_store.backend` | Run Store 未配置或为内存库，重启即丢数据 |
| `run_store.role_attributes` / `no_ddl` | 运行时角色权限过高 |
| `run_store.schema` / `claim_columns` / `checkpoint` | 运行时表结构不完整，Claim/Lease 或恢复不可用 |
| `run_store.cleanup_policy` / `multi_process` | 清理策略或并发后端不适合多进程 |
| `security.tls` | 数据库连接未要求 TLS 校验 |
| `observability.tracing` | 已开启 OTel 但缺 SDK/Exporter/Endpoint |

退出码：`0` 无失败（可有警告），`1` 存在失败项，`2` 参数或配置错误。**存在失败项时不要上线。**

---

## 3. 数据库初始化

### 3.1 角色准备（人工、一次性）

三个角色都必须是最小权限登录角色，`rolsuper/rolcreatedb/rolcreaterole/rolbypassrls` 全为 false：

- `eba_admin`：数据库所有者，建表与授权；只在迁移窗口使用。
- `eba_readonly`：仅业务表 `SELECT`。
- `eba_runtime`：仅 10 张 `agent_*` 运行时表的 `SELECT/INSERT/UPDATE/DELETE`，无 DDL。

角色创建属于生产变更，按组织流程人工执行；仓库不提供自动化创建脚本以外的权限提升路径。密码由密钥管理系统生成并注入，禁止出现在命令行历史、报告和聊天记录中。

### 3.2 业务库

```bash
# 使用 eba_admin 或迁移账号执行 Alembic / SQL 脚本
alembic upgrade head
python scripts/validate_semantic.py
python scripts/quality_check.py --database-url "$READONLY_DATABASE_URL"
```

### 3.3 运行时库

```bash
# 只在此命令中临时注入初始化连接与运行时角色密码
EBA_RUNTIME_SETUP_URL='postgresql+psycopg://eba_admin:<redacted>@<db-host>:5432/enterprise_business_agent' \
EBA_RUNTIME_ROLE=eba_runtime \
EBA_RUNTIME_PASSWORD='<redacted>' \
.venv/bin/python scripts/init_runtime_store.py
```

脚本会创建 10 张运行时表并授权给 `eba_runtime`，权限仅为 `agent_*` 表的 DML。任何 `CREATE TABLE`、`ALTER`、`DROP` 都必须在 API 进程之外的迁移窗口执行。

### 3.4 备份与恢复

- 备份对象：业务库 + 运行时库（Run、Event、Tool Result、Checkpoint、Evidence、Finding、Report、Approval 全部在其中）。
- 建议：每日全量 + 持续 WAL 归档；备份文件加密并在独立账号下保存。
- 恢复演练（至少每季度一次，在隔离环境执行）：

```bash
pg_restore --clean --if-exists -d <restore-db> backup.dump
READONLY_DATABASE_URL='...' RUN_STORE_URL='...' \
  .venv/bin/python scripts/preflight.py
```

- 恢复后必须确认：`agent_runs` 的 `claim_owner` 为空的行不会被视为活动租约；Checkpoint 能按 `thread_id` 读取；审批状态与报告版本指纹一致。
- 生产恢复属于高风险操作，必须在变更单和人工确认下执行，本文不提供自动恢复命令。

---

## 4. 环境变量与安全

| 变量 | 生产要求 |
| --- | --- |
| `DATABASE_URL` | 仅用于初始化/迁移，可留空 |
| `READONLY_DATABASE_URL` | 必填，`eba_readonly`，建议带 `sslmode=verify-full` |
| `RUN_STORE_URL` | 必填，`eba_runtime` 的 PostgreSQL 连接 |
| `EBA_RUNTIME_SETUP_URL` / `EBA_RUNTIME_PASSWORD` | 仅初始化进程临时注入 |
| `EBA_API_KEYS` | 必填，JSON 映射 `{"<key>":"<role>"}`；密钥不得写入仓库 |
| `EBA_ALLOW_ANONYMOUS_API` | 必须为 `false` |
| `EBA_AUTHZ_POLICY` | 如需按角色限制指标/域，配置 JSON 策略 |
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_BASE_URL` | 与 Provider 协议一致，`preflight` 会校验 |
| `LLM_API_KEY` | 由密钥管理注入；轮换是独立人工事项 |
| `EBA_LOAD_DOTENV` | 生产建议保持 `false`，由进程管理器注入环境变量 |
| `EBA_OTEL_ENABLED` / `EBA_OTEL_EXPORTER_ENDPOINT` | 见第 8 节 |

安全要求：

1. `.env` 权限 `0600`，属主为运行账号；更好的做法是不落盘，由 systemd/K8s Secret 注入。
2. 禁止把密码、API Key、Token 写入日志、报告、CI 变量明文或命令行参数。`preflight` 与所有脚本都会对连接串脱敏。
3. 数据库连接启用 TLS：`sslmode=verify-full` 并配置 CA；`require` 只加密不校验主机名。
4. LLM API Key 轮换、API Key 轮换和数据库密码轮换分别作为独立上线事项，需要人工确认后执行；代码不会自动轮换。
5. API 只暴露在受控网络或 TLS 反向代理之后；`X-API-Key` 必须经 TLS 传输。

---

## 5. 进程与多实例配置

```bash
# 单进程（本地/小规模）
EBA_LOAD_DOTENV=true uvicorn enterprise_business_agent.api.app:app --host 127.0.0.1 --port 8000

# 多进程（生产推荐，共享同一个 PostgreSQL Run Store）
uvicorn enterprise_business_agent.api.app:app --host 0.0.0.0 --port 8000 --workers 4
```

多进程前提：

- `RUN_STORE_URL` 必须是 PostgreSQL。SQLite 只能单进程，`preflight` 会给出警告。
- Claim/Lease 由数据库条件更新实现，`claim_version` 单调递增；失去租约的 worker 写入会被拒绝（`RunClaimLost`，HTTP 409 `run_claim_conflict`）。
- `RUN_LEASE_SECONDS` 必须显著大于单次 Run 的心跳间隔，且小于 `RUN_RETENTION_UNFINISHED_SECONDS`。
- 清理任务由 API 启动时的 lifespan 执行，按状态分 TTL；多实例并发安全，但建议只让一个实例或独立定时任务承担大规模清理（`RUN_CLEANUP_BATCH_LIMIT` 控制单批大小）。
- `RUN_CLEANUP_DRY_RUN=true` 只报告不删除，用于上线前观察。

生命周期预算：

- `AGENT_MAX_SECONDS`：单次执行时间上限。
- `AGENT_RUN_MAX_LIFECYCLE_SECONDS`：从创建到现在的累计预算，跨恢复不重置。
- `AGENT_MAX_TOTAL_TOKENS`、`AGENT_MAX_TOOL_CALLS`、`AGENT_MAX_CONSECUTIVE_FAILURES`：单 Run 上限。
- `AGENT_TOOL_TIMEOUT_SECONDS`、`AGENT_TOOL_MAX_RETRIES`、`AGENT_LLM_MAX_RETRIES`：外部依赖上限。

---

## 6. 健康检查、指标与审批

| 端点 | 用途 | 认证 |
| --- | --- | --- |
| `GET /health` | 存活/就绪探针 | 无 |
| `GET /api/v1/metrics` | 运行质量、工具、模型、校验、审批、数据库、可观测性状态 | `admin` |
| `GET /api/v1/agent/approvals` | 审批工作台列表/筛选 | `approval:read`（本人）或 `approval:read_any` |
| `POST /api/v1/agent/runs/{run_id}/report/approval/decision` | 审批决策 | `approval:decide`，按所有者隔离 |

运维建议：

- 存活探针 `/health`；就绪探针同时检查业务库只读连接与 Runtime 库可用性。
- 对 `metrics.runs.failure_rate`、`model.errors`、`tools.failure_rate`、`approvals.pending`、`database.connection_errors` 建告警。
- `metrics.observability.exported_spans` 是**进程内**计数，多实例部署需要逐实例采集或改用 OTLP 后端聚合。

---

## 7. 日志

- 结构化 JSON 行日志，包含 `request_id`、`run_id`、`event_type`，不包含 Prompt、模型正文或密钥。
- 建议集中采集到只读审计存储，保留期与 Run 清理策略一致。
- 审计事件（`sql_execution`、`agent_events`）保留在运行时表中，不依赖日志文件。

---

## 8. OpenTelemetry

默认关闭。开启方式：

```bash
pip install -e '.[otel]'
export EBA_OTEL_ENABLED=true
export EBA_OTEL_EXPORTER_ENDPOINT='http://<collector>:4318/v1/traces'
export EBA_OTEL_SERVICE_NAME='enterprise-business-agent'
```

Span 覆盖：

- `agent.run`（根）：run_id、provider、prompt_version、最终状态、token 与耗时。
- `agent.decision`：轮次、预算、已用 token。
- `llm.complete`：模型、延迟、token、工具调用数。
- `tool.call`：工具名、call_id、参数、状态、耗时、Evidence 数量；执行期间作为当前 span，使数据库查询落在同一 trace。
- `db.query`：dialect、表、SQL hash、行数、是否截断、耗时；自动携带 `agent.run_id` 与 `tool.call_id`。
- `checkpoint.restore`：thread_id、是否找到、checkpoint_id、pending writes、恢复耗时；未找到时标记为错误。

诚实状态规则：

- 未安装 SDK → `observability.otel_sdk_available=false`，不创建 span。
- 开启但未配置 endpoint → `init_error="no OTLP endpoint configured"`，不导出。
- 只有 `SpanExporter.export` 返回 `SpanExportResult.SUCCESS` 才增加 `exported_spans`；失败计入 `dropped_spans`。
- 初始化或导出异常不会影响业务请求。
- **没有 OTLP 后端时，只能确认"span 已生成并按成功/失败计数"，不能声称"外部链路已验证"。** 真实导出验证需要 Collector 或厂商后端，并在 `/api/v1/metrics` 中观察 `exported_spans` 增长。

---

## 9. 上线检查清单

| # | 事项 | 责任 | 状态 |
| --- | --- | --- | --- |
| 1 | 生产 `.env` 由密钥管理注入，权限 0600 | 运维 | 人工确认 |
| 2 | 三个 PostgreSQL 角色按最小权限创建 | DBA | 人工确认 |
| 3 | 业务库迁移完成，语义层校验通过 | 后端 | 人工确认 |
| 4 | 运行时表结构与授权完成 | DBA | 人工确认 |
| 5 | `preflight` 无 FAIL | 运维 | 人工确认 |
| 6 | 多实例配置与 Lease 参数确认 | 后端 | 人工确认 |
| 7 | TLS 与反向代理配置完成 | 运维 | 人工确认 |
| 8 | 指标与告警阈值确认 | SRE | 人工确认 |
| 9 | 备份与恢复演练完成 | DBA | 人工确认 |
| 10 | OTLP 后端接入并验证 `exported_spans` 增长 | SRE | 人工确认 |
| 11 | LLM Key 轮换作为独立变更执行 | 安全 | 人工确认 |
| 12 | API Key 轮换与最小角色分配 | 安全 | 人工确认 |

---

## 10. 已知限制与残留风险

1. 本仓库未在真实生产服务器上执行部署；PostgreSQL 验证使用本地演练/测试库。
2. 未接入真实 OTLP 后端，外部 trace 链路未验证。
3. 未做容量压测与多机灾备演练。
4. 清理任务与审批超时依赖进程生命周期；需要独立定时任务时应在部署层补充。
5. 运行时表位于与业务库相同的数据库实例时，故障域不隔离；生产建议独立实例或独立 database。
6. 模型叙述失败时会回退为确定性文本并在报告中显式标注警告；这不影响结构化报告和数字校验，但会降低叙述可读性。
