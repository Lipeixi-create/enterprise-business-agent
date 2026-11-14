# Phase 6 Batch 2 完成报告：运行可靠性与人工审批

日期：2026-10-31
范围：PostgreSQL 运行时持久化、LangGraph Checkpoint 与中断恢复、异常/预算控制、报告人工审批
项目：`enterprise_business_agent`
前置批次：`PHASE6_BATCH1_REPORT.md`（基础质量与安全治理）

> 凭据说明：本报告不包含数据库密码、API Key 或完整连接串。测试连接均由进程环境变量传入，报告中以 `<redacted>` 代替。未修改、读取或打印任何现有 API Key。生产 `.env` 未切换，未创建生产 `eba_runtime` 角色，详见第 10 节。

---

## 1. 结论摘要

| 工作流 | 状态 | 结果 |
| --- | --- | --- |
| PostgreSQL 运行时表结构 | 完成（代码与测试库验证） | 10 张 `agent_*` 运行时表由 setup 角色创建；runtime 角色仅 DML |
| 运行时角色隔离 | 完成（测试库验证） | runtime 角色 `rolsuper/rolcreatedb/rolcreaterole=false`；无 schema CREATE；`CREATE/DROP TABLE` 被拒绝 |
| Run / Checkpoint / Evidence / Finding / Report 关联 | 完成（逻辑关联） | 统一 `run_id`；报告按 `(run_id, report_version)` 唯一；审批按 `(report_id, report_version)` + 内容指纹绑定 |
| LangGraph Checkpoint | 完成 | 自定义 `SqlAlchemyCheckpointSaver`；SQLite 与 PostgreSQL 均保存、重开、恢复 |
| 中断恢复与工具幂等 | 完成 | 进程内崩溃后可恢复；已完成工具从持久缓存重放，不重复执行 |
| 恢复权限复查 | 完成 | 恢复路由重新认证/授权；待执行工具和缓存结果均按当前 principal 重新校验 |
| 异常与预算控制 | 完成 | 时间、工具次数、Token、连续失败、单工具超时、工具/模型重试、重复调用均有上限 |
| 人工审批 | 完成（仅审批与审计） | 状态机、版本/指纹绑定、所有者隔离、幂等与非法流转拒绝、事件审计 |
| 全量 SQLite 测试 | 通过 | **456 passed, 16 skipped** |
| Phase 6 定向测试 | 通过 | **22 passed**（运行时 13 + 审批 9） |
| PostgreSQL 专项测试 | 通过 | **16 passed**（真实 PostgreSQL 测试库） |
| 生产切换 | **未完成** | `.env` 仍为 SQLite Run Store；生产 `eba_runtime` 角色和切换步骤未执行 |

---

## 2. 检查与保护

- 当前目录不是 Git 仓库（`git status` 返回 fatal），因此没有提交、回滚或 diff 操作；所有修改保留在原文件。
- 实施前检查了现有 Run Store、Phase 5 Evidence/Finding/Report Store、LangGraph 图构建、工具注册、权限体系和 Batch 1 报告。
- 未重写 Agent；保留单 Agent + LangGraph + Function Calling + Tool Registry 架构。
- 未引入 Kafka、Kubernetes、微服务或新的任务队列。
- 未允许模型直接操作数据库；工具仍只通过 `AnalysisGateway` / `SafeQueryExecutor` 执行只读查询。
- 未删除或覆盖用户已有数据库文件。
- PostgreSQL 测试使用一次性数据库 `eba_b2_test` 与一次性角色 `eba_b2_admin` / `eba_b2_runtime` / `eba_b2_readonly`，测试结束后已删除；不是生产库，也不是 Mock。

---

## 3. PostgreSQL 运行时持久化

### 3.1 角色分层

| 角色 | 用途 | 权限边界 |
| --- | --- | --- |
| `eba_admin` | setup / 建表 / 初始化 | Batch 1 已存在的非超级用户数据库所有者；只用于 `scripts/init_runtime_store.py`，API 不使用 |
| `eba_runtime` | **生产 runtime 角色（设计完成，未部署）** | LOGIN、NOINHERIT、NOSUPERUSER、NOCREATEDB、NOCREATEROLE；仅 `agent_*` 表的 SELECT/INSERT/UPDATE/DELETE；无 DDL |
| `eba_readonly` | 业务数据只读查询 | Batch 1 已存在；API 分析查询使用；本批次未改动 |

`grant_runtime_role()` 的实际授权顺序：

1. 创建或调整 runtime 角色：`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`；
2. `GRANT CONNECT ON DATABASE`；
3. `ALTER ROLE ... SET default_transaction_read_only = off`（Run Store 需要写）；
4. `statement_timeout=30s`、`lock_timeout=10s`、`idle_in_transaction_session_timeout=30s`、`search_path=public`；
5. `GRANT USAGE ON SCHEMA public`；
6. `REVOKE CREATE ON SCHEMA public`；
7. `REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public`；
8. 仅对 10 张 `agent_*` 表 `GRANT SELECT, INSERT, UPDATE, DELETE`。

运行时角色不会获得业务表的 SELECT，也不会获得 DDL 权限。API 的业务查询仍走独立 `eba_readonly` 连接。

### 3.2 运行时表结构（10 张）

| 表 | 用途 | 主键 / 唯一约束 |
| --- | --- | --- |
| `agent_runs` | Run 文档、状态、owner、问题、payload | `run_id` PK |
| `agent_audit_events` | Run 审计/跟踪事件 | `event_id` PK；`run_id` 索引 |
| `agent_tool_results` | 已完成工具调用结果缓存 | `(run_id, call_key)` PK |
| `agent_evidence` | Phase 5 证据账本 | `evidence_id` PK；`UNIQUE(run_id, fingerprint)` |
| `agent_findings` | Phase 5 发现 | `finding_id` PK；`run_id` 索引 |
| `agent_reports` | Phase 5 报告版本 | `report_id` PK；`UNIQUE(run_id, report_version)` |
| `agent_checkpoints` | LangGraph Checkpoint 主记录 | `(thread_id, checkpoint_ns, checkpoint_id)` PK |
| `agent_checkpoint_writes` | LangGraph pending writes | `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)` PK |
| `agent_report_approvals` | 报告审批当前状态 | `approval_id` PK；`UNIQUE(report_id, report_version)` |
| `agent_report_approval_events` | 审批状态流转事件 | `event_id` PK；`approval_id` 索引 |

`scripts/init_runtime_store.py` 在 PostgreSQL 上创建上述全部表并输出表数量；在 SQLite 上直接建表并标注为本地开发。脚本只输出角色名和表名，不输出密码。

### 3.3 关联关系

- Run 是聚合根：`agent_audit_events`、`agent_tool_results`、`agent_evidence`、`agent_findings`、`agent_reports`、`agent_report_approvals` 均带 `run_id`。
- Checkpoint 的 `thread_id` 直接使用 `run_id`；恢复时以同一 `thread_id` 读取最新 checkpoint。
- Report 以 `(run_id, report_version)` 唯一；每个版本有独立 `report_id`。
- Approval 以 `(report_id, report_version)` 唯一，并保存该版本报告的 SHA-256 内容指纹。
- Approval 事件通过 `approval_id` 关联，事件中同时冗余 `run_id/report_id/report_version`，便于审计查询。

> 当前这些关联是**逻辑关联 + 索引/唯一约束**，没有数据库级外键。原因是四个 Store 独立建表、SQLite 与 PostgreSQL 共用同一实现，且 Phase 5 表结构在前一批次已固定。应用层在写入前保证关联字段来自同一 Run/Report 对象；未做跨表级联删除。该限制列入第 10 节。

### 3.4 连接硬化与禁止静默降级

- PostgreSQL 连接参数：`application_name=eba_run_store`、`connect_timeout=5`、`statement_timeout=30000`、`lock_timeout=10000`、`idle_in_transaction_session_timeout=30000`。
- `PersistentRunStore` 对 PostgreSQL 不执行 DDL：
  - 表缺失时抛出 `RuntimeError`，提示先用 setup 角色运行 `scripts/init_runtime_store.py`；
  - 连接不可达时直接抛出数据库连接异常；
  - 任何情况下都不会在 PostgreSQL 失败后创建 SQLite 文件。
- SQLite 只在 `RUN_STORE_URL` 未设置或明确为 `sqlite://...` 时使用，属于显式本地开发配置，不是故障回退。
- `SqlAlchemyCheckpointSaver`、`ApprovalStore` 同样在 PostgreSQL 上检查表是否存在，缺表即失败。

### 3.5 初始化与启动命令

```bash
# 1) setup 角色创建全部运行时表并授权（密码仅从环境变量读取）
EBA_RUNTIME_SETUP_URL='postgresql+psycopg://eba_admin:<redacted>@localhost:5432/enterprise_business_agent' \
EBA_RUNTIME_ROLE=eba_runtime \
EBA_RUNTIME_PASSWORD='<redacted>' \
PYTHONPATH=src .venv/bin/python scripts/init_runtime_store.py

# 2) API 使用最小权限 runtime 角色（生产切换时才设置）
RUN_STORE_URL='postgresql+psycopg://eba_runtime:<redacted>@localhost:5432/enterprise_business_agent' \
PYTHONPATH=src .venv/bin/uvicorn enterprise_business_agent.api.app:app \
  --host 127.0.0.1 --port 8000
```

`EBA_RUNTIME_SETUP_URL` 与 `EBA_RUNTIME_PASSWORD` 只用于初始化进程，不应出现在 API 进程环境。

---

## 4. LangGraph Checkpoint 与任务恢复

### 4.1 Checkpoint 存储实现

官方 LangGraph PostgreSQL/SQLite checkpoint 包与 `psycopg_pool` 在当前环境不可用，因此实现了 `SqlAlchemyCheckpointSaver`，直接复用应用已有的 SQLAlchemy Engine：

- 实现 `put`、`put_writes`、`get_tuple`、`list`、`delete_thread` 与 `latest_checkpoint_id`；
- 使用 `JsonPlusSerializer`，显式允许 `AgentStep`、`ToolCallRecord`、`EvidenceDraft` 三个 Pydantic 类型；
- `agent_checkpoints` 保存 checkpoint 主记录与 metadata；`agent_checkpoint_writes` 保存 pending writes；
- SQLite 和 PostgreSQL 共用同一实现，PostgreSQL 上不依赖 `psycopg_pool`。

### 4.2 状态与进程内上下文分离

可进入 checkpoint 的状态包括：

- `run_id`、`question`、`principal_id`、`role`、`scopes`；
- `messages`、`steps`、`observations`、`evidence`、`errors`、`warnings`；
- `pending_action`、`seen_calls`、`tool_calls_used`、`consecutive_failures`；
- `max_tool_calls`、`max_total_tokens`、`llm_input_tokens`、`llm_output_tokens`、`invalid_action_retries`。

不进入 checkpoint 的进程内对象放在 `_RunContext`，按 `run_id` 注册：

- 当前 principal 对象；
- `deadline`（本次执行的单调时钟截止时间）；
- `cancel_event`；
- trace sink 与 OpenTelemetry span sink。

恢复时创建新的 `_RunContext`，因此不会把旧进程的线程、连接或取消信号带入新进程。

### 4.3 中断恢复流程

1. `AgentRuntime.run()` 为 `run_id` 创建/清理旧 checkpoint，构建 `thread_id=run_id` 的图配置。
2. `decide` 节点完成模型调用后，把 `pending_action` 写入 checkpoint，然后才进入 `act`。
3. `act` 先按工具名和规范化参数计算 `call_key`（SHA-256），查询 `agent_tool_results`：
   - 命中且当前 principal 仍有权限：重放结果，不执行工具；
   - 命中但当前 principal 无权：返回 `permission_denied`，不泄露缓存内容；
   - 未命中：执行工具，成功后写入缓存；`permission_denied` / `invalid_action` 不缓存。
4. 进程在任意节点崩溃后，Run Store 中可能仍是 `running`；API 下次启动调用 `mark_interrupted()`，把 `queued/running/cancelling` 改为 `interrupted`，并写入 `stop_reason=process_restart`。
5. 调用 `POST /api/v1/agent/runs/{run_id}/resume`：
   - 路由重新认证当前 principal，并做 `run:read` Scope 与资源所有权检查；
   - `RunManager.resume()` 重新读取持久状态，检查是否可恢复；
   - 将状态置为 `running`，写入 `run_resumed` 审计事件；
   - `AgentRuntime.resume()` 读取同一 `thread_id` 的最新 checkpoint，注入新的 `_RunContext`；
   - `graph.invoke(None)` 从 checkpoint 继续，不重新调用已完成的工具；
   - 结束后持久化最终状态并写入 `run_finished` 审计事件。
6. 无 checkpoint 时返回结构化失败：`status=failed`、`stop_reason=checkpoint_not_found`，不会假装继续。
7. 恢复异常（例如图执行抛错）也会落盘为 `failed/graph_error`，不会留下永久 `running`。

### 4.4 可恢复与不可恢复边界

可恢复：

- `interrupted`（进程重启）；
- `failed`、`llm_unavailable`、`llm_timeout`、`tool_failure`，且 stop reason 不属于明确终止决策。

不可恢复：

- `permission_denied`；
- `tool_budget_exhausted`、`token_budget_exhausted`、`time_budget_exhausted`；
- `repeated_tool_call`；
- `cancelled_by_user`、`cancelled_before_start`；
- 任何已 `completed` / `completed_with_warnings` / `no_evidence` / `no_data` / `cancelled` 的运行；
- 本进程内仍活跃的运行（`run_already_active`）。

恢复策略集中在 `_is_resumable()`，并有单元测试覆盖矩阵。

### 4.5 恢复时权限复查

权限复查分三层，而不是依赖“之前已经授权”：

1. **API 层**：重新认证 API Key，重新检查 `run:read` / `report:read`；非 owner 默认 404，避免泄露存在性。
2. **RunManager 层**：用当前 principal 的 owner 重新读取持久 Run；owner 不匹配即找不到。
3. **Agent 层**：
   - 待执行工具在 `act` 中通过 `registry.execute()` 重新走 `_prepare()`；
   - 缓存结果在重放前调用 `registry.authorize_call()`；
   - 当前角色被降权后，即使缓存中存在旧结果，也返回 `permission_denied`，不会回放数据。

---

## 5. 异常处理与预算控制

### 5.1 配置项

| 配置 | 默认值 | 范围 | 作用 |
| --- | --- | --- | --- |
| `AGENT_MAX_TOOL_CALLS` | 6 | 1–20 | 单次运行最大工具调用次数 |
| `AGENT_MAX_SECONDS` | 180 | 0–300 | 单次执行最大墙钟时间 |
| `AGENT_TOOL_TIMEOUT_SECONDS` | 20 | 0–120 | 单次工具调用超时上限 |
| `AGENT_TOOL_MAX_RETRIES` | 1 | 0–3 | 单个工具调用的可重试失败重试次数 |
| `AGENT_MAX_CONSECUTIVE_FAILURES` | 2 | 1–5 | 连续工具失败上限 |
| `AGENT_MAX_TOTAL_TOKENS` | 60000 | 1000–1000000 | 单次运行累计 Token 预算 |
| `AGENT_INVALID_ACTION_MAX_RETRIES` | 1 | 0–3 | 非法动作受控纠正次数 |
| `AGENT_LLM_MAX_RETRIES` | 1 | 0–3 | 模型调用超时重试次数 |

工具调用超时实际取 `min(agent_tool_timeout_seconds, 剩余时间 / 尝试次数)`，避免重试把总时间拖过截止时间。

### 5.2 异常分类与处理

| 异常 | 分类 | 处理 |
| --- | --- | --- |
| 工具参数不符合 schema | `invalid_arguments` | 不盲目重试；把 `validation_errors` 反馈给模型调整参数；连续失败受上限约束 |
| 工具/资源权限不足 | `permission_denied` | 立即终止为 `invalid_action`，`stop_reason=permission_denied`；不重试、不缓存 |
| 未知工具 | `invalid_action` / `unknown_tool` | 受控纠正：错误反馈给模型，最多 `AGENT_INVALID_ACTION_MAX_RETRIES` 次；超限终止 |
| 数据库暂时不可用 | `database_unavailable` | 捕获 `OperationalError`；最多重试 `AGENT_TOOL_MAX_RETRIES` 次；错误消息隐藏原始连接细节 |
| 工具执行超时 | `timeout` | 有限重试；重试耗尽后按连续失败计数 |
| 模型调用超时 | `llm_timeout` | 最多重试 `AGENT_LLM_MAX_RETRIES` 次；仍超时则终止为 `llm_timeout` |
| 模型返回非法动作 | `invalid_action` | 受控纠正并反馈错误；超过上限终止为 `invalid_action` |
| 重复相同工具调用 | `repeated_tool_call` | 按规范化 `{tool,args}` 检测；终止，不执行第二次 |
| 工具次数预算耗尽 | `budget_exhausted` / `tool_budget_exhausted` | 终止并保留状态 |
| Token 预算耗尽 | `budget_exhausted` / `token_budget_exhausted` | 终止并保留状态，阻止新工具调用 |
| 总时间耗尽 | `timeout` / `time_budget_exhausted` | 在模型调用前和工具执行前检查截止时间，终止 |

所有终止路径都会返回 `AgentRunResponse`，由 `RunManager` 持久化为最终 Run 状态，并写入审计事件；没有“无限循环”路径。

> 说明：墙钟截止时间是**每次执行尝试**的预算；恢复时会获得新的有界时间预算。Token 和工具调用次数保存在 checkpoint 中，跨恢复累计。若需要“整个 Run 生命周期总墙钟时间”上限，应在 Batch 3 增加持久化的累计时间字段。

---

## 6. 人工审批流程

### 6.1 状态机

```text
not_required                       （低风险 / 未通过校验的报告）
      │
      ├── 高风险且有建议 → pending
      │                        ├── approve → approved
      │                        ├── reject  → rejected
      │                        └── cancel  → cancelled
      │
      └── 不再流转
```

- `approved` / `rejected` / `cancelled` / `not_required` 均为终态。
- 终态后只允许相同 actor 对相同目标状态重复提交（返回 `duplicate=true`）；不同决定或不同 actor 返回 `invalid_approval_transition`。
- `not_required` 不能审批，防止把低风险报告“补批”为已批准。
- 未经批准的 `pending` 永远不会被标记为 `approved`；本批次没有自动批准路径。

### 6.2 高风险判定与绑定

- 进入 `pending` 的条件：报告 `status == validated`，且至少一条 `Recommendation.requires_approval == true`。
- 其它情况（低风险、校验未通过）直接 `not_required`。
- 每条审批记录绑定：
  - `run_id`；
  - `report_id`；
  - `report_version`；
  - `report_fingerprint`（报告文档 canonical JSON 的 SHA-256）；
  - `requested_by` / `requested_at`；
  - `decided_by` / `decided_at` / `comment`；
  - 当前状态与创建/更新时间。
- 报告内容变化时指纹不同，旧审批不能用于新内容；新报告版本生成新的 `report_id` 与新的 `pending` 审批。
- 审批事件表记录 `from_state`、`to_state`、actor、comment、时间，形成完整审计链。

### 6.3 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/agent/runs/{run_id}/report/approval?version=N` | 读取指定版本审批；省略 `version` 时取该 Run 最新报告版本 |
| POST | `/api/v1/agent/runs/{run_id}/report/approval/decision` | 提交 `approve` / `reject` / `cancel` |
| GET | `/api/v1/agent/runs/{run_id}/report/approval/events?version=N` | 读取审批状态流转事件 |
| POST | `/api/v1/agent/runs/{run_id}/resume` | 恢复中断/可恢复失败的 Run |

审批决定请求必须携带 `report_version` 与 `report_fingerprint`，服务端不会猜测“最新版本”。缺少字段返回 422。

主要状态码：

| 场景 | 状态码 / 错误码 |
| --- | --- |
| 缺少版本或指纹 | 422 `invalid_request` |
| 缺少 `approval:decide` Scope | 403 `forbidden` |
| 报告所有者审批自己的报告 | 403 `approval_separation_violation` |
| 非 owner 访问 Run | 404 `agent_run_not_found` |
| 审批记录不存在 | 404 `approval_not_found` |
| 指纹不匹配 | 409 `approval_stale` |
| 终态后不同决定 / 不同 actor | 409 `invalid_approval_transition` |
| 重复相同决定 | 200，`duplicate=true` |
| Run 不可恢复 | 409 `run_not_resumable` |
| 无 checkpoint | 200，响应体 `status=failed`、`stop_reason=checkpoint_not_found` |

### 6.4 权限隔离

- 读取审批需要 `report:read`，并按 Run owner 做资源隔离。
- 审批决定需要 `approval:decide`；`approver`、`business_manager`、`admin` 具备该 Scope。
- 报告所有者不能 `approve` / `reject` 自己的报告，即使其角色拥有 `approval:decide`（已有 `business_manager` owner 测试）。
- 所有者可以 `cancel` 自己的待审批请求；非 owner 执行 `cancel` 需要 `approval:decide`。
- `viewer` 没有报告资源 Scope，不能读取审批。
- 所有授权在 FastAPI 后端执行，前端无法绕过。

### 6.5 本批次边界

- 本批次只实现审批、状态流转和审计，**没有接入采购、调价、预算、人事等业务执行接口**。
- 审批通过不会触发任何自动业务动作；Agent 仍只有只读分析工具。
- 没有新增“自动批准”“超时自动通过”“按金额自动通过”等路径。

---

## 7. 修改文件

| 文件 | 修改内容 |
| --- | --- |
| `src/enterprise_business_agent/agent/checkpoint.py` | 新增 SQLAlchemy LangGraph Checkpoint Saver、checkpoint 表、显式序列化白名单、缺表检查 |
| `src/enterprise_business_agent/evidence/approval.py` | 新增审批表、审批事件表、指纹、状态机、所有者隔离、幂等与终态校验 |
| `src/enterprise_business_agent/api/runtime_schema.py` | 新增 10 张运行时表统一建表/删表、runtime 角色授权与权限收紧 |
| `scripts/init_runtime_store.py` | 新增运行时 schema 初始化与角色授权脚本；不打印密码 |
| `src/enterprise_business_agent/api/run_store.py` | 新增工具结果缓存表与读写方法、PostgreSQL 连接硬化、缺表失败、`mark_interrupted()` |
| `src/enterprise_business_agent/agent/graph.py` | 接入 Checkpoint/恢复、进程内上下文、工具幂等缓存、缓存重授权、模型超时重试、非法动作受控纠正 |
| `src/enterprise_business_agent/agent/tools.py` | 新增 `authorize_call()`；`database_unavailable` 分类；有限重试仅覆盖可重试状态 |
| `src/enterprise_business_agent/agent/contracts.py` | 响应增加 `approval_id` / `approval_state` |
| `src/enterprise_business_agent/api/run_manager.py` | 新增 `RunManager.resume()`、可恢复状态矩阵、`RunResumeError`、恢复审计 |
| `src/enterprise_business_agent/api/routes.py` | 新增 Resume 路由、审批读取/决定/事件路由、报告内嵌审批、恢复错误映射 |
| `src/enterprise_business_agent/security/auth.py` | 新增 `approval:read` / `approval:decide` Scope 与 `approver` 角色 |
| `src/enterprise_business_agent/evidence/pipeline.py` | 最终化报告时创建/复用审批记录，返回 `approval_id` / `approval_state` |
| `src/enterprise_business_agent/evidence/__init__.py` | 导出审批类型 |
| `src/enterprise_business_agent/config.py` | 新增 `agent_invalid_action_max_retries`、`agent_llm_max_retries` 与运行时配置读取 |
| `src/enterprise_business_agent/api/app.py` | 接入 Checkpointer、工具缓存、启动时中断标记与清理 |
| `.env.example` | 记录 PostgreSQL runtime 初始化/启动方式、禁止降级、重试配置；未写入真实密码 |
| `tests/test_phase6_runtime.py` | 新增 13 个 SQLite 运行时测试（真实 SQLite/Checkpoint，脚本化 LLM） |
| `tests/test_phase6_approvals.py` | 新增 9 个审批测试（SQLite + API） |
| `tests/test_phase6_postgres_runtime.py` | 新增 5 个真实 PostgreSQL 运行时测试 |
| `tests/test_phase5_postgres.py` | 测试 fixture 自行创建 Run Store schema，消除对 Batch 2 测试执行顺序的依赖 |
| `tests/test_agent_runtime.py`、`tests/test_agent_security_boundaries.py`、`tests/test_governance.py` | 更新既有用例以覆盖新的模型超时重试与非法动作纠正行为；安全用例仍验证未知工具无法触达数据库 |

> `.env` 未修改。生产切换需要用户确认后单独执行，见第 10 节。

---

## 8. 测试结果

### 8.1 全量 SQLite 测试

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

结果：**456 passed, 16 skipped in 33.68s**

- 16 个跳过项全部是未设置 `EBA_TEST_POSTGRES_URL` 时自动跳过的 PostgreSQL 专项测试；没有把跳过计为通过。
- 全量测试中的 Agent 控制流使用仓库自带的 `ScriptedLLM`，它只替代模型决策，**不冒充真实模型质量验收**。
- Run Store、Checkpoint、审批表、工具缓存均为真实 SQLite 表，不是内存 Mock。

### 8.2 Phase 6 定向测试

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_phase6_runtime.py \
  tests/test_phase6_approvals.py
```

结果：**22 passed**

| 测试文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `tests/test_phase6_runtime.py` | 13 | 重启后读取、崩溃恢复、缓存幂等、恢复重授权、无 checkpoint、数据库不可用重试、PostgreSQL 不降级、API Resume、模型超时重试/上限、非法动作纠正/上限、恢复策略矩阵 |
| `tests/test_phase6_approvals.py` | 9 | 高风险进入待审批、审批/拒绝/取消、重复提交、所有者隔离、终态拒绝、版本变化旧审批失效、无审批/非法状态、API Scope/指纹/隔离/审计 |

### 8.3 PostgreSQL 专项测试

使用一次性真实 PostgreSQL 数据库 `eba_b2_test`；连接串只通过进程环境变量传入，未写入仓库、未打印：

```bash
EBA_TEST_POSTGRES_URL='postgresql+psycopg://<redacted>' \
EBA_TEST_POSTGRES_RUNTIME_URL='postgresql+psycopg://<redacted>' \
EBA_TEST_POSTGRES_READONLY_URL='postgresql+psycopg://<redacted>' \
.venv/bin/python -m pytest -q \
  tests/test_phase5_postgres.py \
  tests/test_phase6_postgres_runtime.py \
  tests/test_postgres_integration.py
```

结果：**16 passed in 1.50s**

| 测试文件 | 数量 | 覆盖 |
| --- | --- | --- |
| `tests/test_phase6_postgres_runtime.py` | 5 | 10 张表存在、Run Store 重开读取、Checkpoint 崩溃恢复、审批状态机、runtime 角色 DML/非超级用户/无 DDL/业务表不可读 |
| `tests/test_phase5_postgres.py` | 4 | Phase 5 schema 幂等、Evidence 往返/去重、Finding/Report 持久化与版本、Run Store 往返 |
| `tests/test_postgres_integration.py` | 7 | 语义查询链、SQLite/PostgreSQL 数值一致性、只读执行、只读拒写、超时映射、独立只读角色拒写等 |

PostgreSQL 测试不是 Mock，也不是 SQLite 替代：

- 真实创建 10 张 `agent_*` 表；
- 真实关闭并重开 `PersistentRunStore`；
- 真实使用新的 `SqlAlchemyCheckpointSaver` 与新的图实例恢复；
- 真实 runtime 角色执行 INSERT/UPDATE/SELECT/DELETE，并验证 `CREATE TABLE` / `DROP TABLE` 被 PostgreSQL 拒绝；
- 测试结束后删除测试数据库与测试角色。

### 8.4 测试类型标注

| 类型 | 使用位置 | 说明 |
| --- | --- | --- |
| 真实 SQLite | 全量测试、Phase 6 运行时/审批 | 真实文件或内存 SQLite 表；验证持久化、checkpoint、状态机 |
| 真实 PostgreSQL | PostgreSQL 专项 | 一次性真实 PG 数据库与真实最小权限角色 |
| 脚本化 LLM | 全量测试、Phase 6 运行时/审批 API | 只控制 `decide` 返回；不调用真实模型，不声称模型质量 |
| Mock | 无 | 没有用 Mock 冒充 PostgreSQL、Checkpoint 或恢复流程 |

### 8.5 未执行的验证

- 没有执行真实 GLM/LLM 调用；本批次用脚本化异常验证超时重试路径，但没有验证真实网络超时、真实模型非法动作分布。
- 没有把生产 `.env` 切换到 PostgreSQL；生产 `eba_runtime` 角色未创建。
- 没有多进程并发恢复压力测试。
- 没有前端 UI 验收；审批接口为后端 API。

---

## 9. 需求覆盖矩阵

| 需求 | 状态 | 证据 |
| --- | --- | --- |
| PostgreSQL Run Store 持久化与重启读取 | 完成 | `test_postgres_run_store_survives_reopen`、`test_checkpoint_and_tool_result_survive_process_restart` |
| LangGraph Checkpoint 保存与恢复 | 完成 | `test_postgres_checkpoint_resumes_after_a_crash`、SQLite 崩溃恢复测试 |
| 中断恢复不重复执行已完成工具 | 完成 | 重启后 `second_registry.executions == []`，缓存计数不变 |
| 权限变更后恢复仍检查权限 | 完成 | 待执行工具在 restricted 角色下 `permission_denied`；缓存结果重授权测试 |
| 超时、重试、预算、连续失败限制 | 完成 | 模型超时重试/上限、数据库不可用重试、非法动作纠正/上限、既有预算测试 |
| 审批状态流转与非法状态拒绝 | 完成 | 9 个审批测试；终态、`not_required`、重复提交、版本变化 |
| 审批人与报告所有者权限隔离 | 完成 | owner approve/reject 403；business_manager owner 仍 403；非 owner 404 |
| 报告版本变化后旧审批失效 | 完成 | 新版本独立 `pending`；旧版本 `approved` 保持；跨版本指纹 `ApprovalStale` |
| SQLite 与 PostgreSQL 测试均可运行 | 完成 | SQLite 456 passed；PostgreSQL 16 passed |
| 不得静默降级到 SQLite | 完成 | 不可达 PG 抛错且错误文本不含 `sqlite`；缺表 RuntimeError |
| 不允许 Mock 冒充恢复/PG | 完成 | PG 专项使用真实库和真实角色；恢复测试使用真实 checkpoint |
| 不接入业务自动执行 | 完成 | 审批模块只写审批/事件；无采购/调价/预算/人事执行路径 |

---

## 10. 未完成项与明确限制

1. **生产 `.env` 未切换到 PostgreSQL。**
   当前 API 默认仍是 `sqlite:///./enterprise_business_agent_runs.db`。代码和测试已支持 PostgreSQL 运行时，但生产 `eba_runtime` 角色、生产库表初始化与 `RUN_STORE_URL` 切换均未执行。原因是用户要求不擅自修改密钥/生产配置；切换需要单独确认。

2. **PostgreSQL 运行时验证使用一次性测试库。**
   验证库为 `eba_b2_test`，角色为 `eba_b2_admin` / `eba_b2_runtime` / `eba_b2_readonly`，测试后已删除。它不是生产库；生产 `eba_runtime` 尚未创建。

3. **多进程恢复缺少数据库租约。**
   `RunManager` 的活跃运行集合是进程内锁。两个 API 进程理论上可以同时恢复同一个 Run，后者可能覆盖前者的状态。当前单进程部署安全；多进程部署前应增加数据库条件的 `claim`（如 `UPDATE ... WHERE status='interrupted'` 或 advisory lock）。

4. **保留策略只清理 `agent_runs`。**
   启动时 `purge_older_than()` 只删除过期 Run 行，不联动删除 `agent_audit_events`、`agent_tool_results`、`agent_checkpoints`、`agent_checkpoint_writes`、审批表。长期运行会产生孤儿数据和存储增长，需 Batch 3 增加按 Run 的事务化清理。

5. **关联关系无数据库外键。**
   Run 与 Evidence/Finding/Report/Checkpoint/Approval 的关联为应用层逻辑关联，依赖 `run_id`、`report_id`、`report_version` 与唯一约束，没有 FK 级联。跨表完整性目前由写入顺序和 Store 逻辑保证，缺少数据库级强制。

6. **模型超时/非法动作使用脚本化异常验证。**
   已验证重试次数与终止状态，但未验证真实模型/真实网络在超时、限流、空响应、非法工具名下的分布。真实模型验收仍需单独执行。

7. **没有真实业务执行。**
   审批通过不会触发采购、调价、预算或人事操作；这是本批次的明确边界，不是遗漏。

8. **审批并发只依赖行锁。**
   PostgreSQL 上 `decide` 使用 `SELECT ... FOR UPDATE`；SQLite 不支持该语义（会被忽略）。SQLite 仅用于本地开发/测试；生产 PostgreSQL 才能保证并发审批的一致性。

9. **没有审批队列/通知。**
   当前只能按 Run 查询审批，没有“待我审批”的跨 Run 列表、邮件/IM 通知或超时提醒。若需要审批工作台，应在 Batch 3 增加查询接口与通知机制。

10. **恢复后的墙钟预算重新计算。**
    Token 与工具次数跨恢复累计，但单次墙钟截止时间按新进程重新计算；没有持久化“Run 生命周期累计耗时”。如需严格总时限，应增加持久化累计字段。

---

## 11. Batch 3 验收建议

1. **生产切换演练**
   - 备份现有 SQLite Run Store；
   - 用 `eba_admin` 运行 `scripts/init_runtime_store.py`；
   - 创建生产 `eba_runtime` 并确认只授予 10 张 `agent_*` 表 DML；
   - 设置 `RUN_STORE_URL` 指向 runtime 角色；
   - 重启后验证 `/health`、Run 读取、进程重启恢复、PostgreSQL 断连不降级；
   - 保留回滚到 SQLite 的显式步骤，但不做自动回退。

2. **多进程安全**
   - 为 Resume 增加数据库租约/条件更新；
   - 增加两个进程同时恢复同一 Run 的集成测试；
   - 明确重复 Resume 的 409 语义。

3. **保留与清理**
   - 按 Run 事务化清理 Run/审计/工具缓存/Checkpoint/审批；
   - 增加 TTL 配置与清理审计；
   - 增加孤儿数据检查。

4. **真实模型验收**
   - 先由用户确认 GLM/LLM 密钥轮换状态；
   - 验证真实超时、限流、空响应、非法工具名、非法参数；
   - 记录真实 Token 消耗与重试次数，不伪造结果。

5. **审批工作台**
   - 增加按状态/审批人查询待审批列表；
   - 增加通知与超时提醒；
   - 仍然只做审批，不自动执行高风险业务动作。

6. **数据库完整性**
   - 评估为 `run_id`、`approval_id` 增加外键或定期完整性校验；
   - 若增加外键，需同时处理 SQLite/PostgreSQL 建表顺序和清理顺序。

7. **可观测性**
   - 监控 Run 状态分布、恢复次数、重试次数、审批待办时长、Checkpoint 增长；
   - 为 `database_unavailable`、`llm_timeout`、`invalid_action_limit` 建立告警。

---

## 12. 验收声明

- 本批次在现有单 Agent + LangGraph + Function Calling + Tool Registry 架构上增量实现，没有重写 Agent。
- Run Store、Checkpoint、Evidence、Finding、Report、Approval 均使用真实持久化存储；没有用 Mock 冒充 PostgreSQL 或恢复流程。
- PostgreSQL 测试使用真实数据库与真实最小权限角色；runtime 角色被验证无超级用户、无建库/建角色、无 DDL。
- PostgreSQL 不可用或 schema 缺失时启动失败，不会静默降级到 SQLite。
- 中断恢复会读取真实 checkpoint，已完成工具不会重复执行；恢复时按当前 principal 重新检查权限。
- 模型超时、工具超时、数据库不可用、非法动作、重复调用、Token/工具/时间预算均有上限和明确终止状态。
- 审批状态机、版本/指纹绑定、所有者隔离、重复提交保护与审计事件均有实际测试。
- 本批次没有接入任何采购、调价、预算或人事执行接口；审批通过不会自动执行高风险业务动作。
- 所有测试数字均为实际执行结果；跳过项已明确标注，未混入未执行或脚本伪造的结果。
- 未打印、复制、记录或提交任何数据库密码或 API Key。
