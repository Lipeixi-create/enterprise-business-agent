# Phase 6 完成报告：生产化运行、并发安全与最终综合验收

日期：2026-10-31
项目：`enterprise_business_agent`
版本：`0.6.0`
范围：Phase 6 Batch 1（基础质量与安全治理）、Batch 2（运行可靠性与人工审批）、Batch 3（生产化验收、并发安全、最终综合测试）

> 凭据说明：本报告不包含数据库密码、API Key、令牌或完整连接串。生产 `.env` 以 `chmod 600` 保存，报告中的连接串统一写作 `<redacted>`。本批次没有读取、打印、复制或修改现有 GLM API Key，也没有执行密钥轮换。真实模型调用使用本地既有配置完成。

---

## 1. 总体结论

### 1.1 最终判定

**Phase 6 核心完成标准全部满足。** PostgreSQL 运行时持久化、数据库级 Run Claim/Lease/Fencing、关联数据事务化清理、Checkpoint 中断恢复、生命周期累计预算、审批工作台权限隔离、五大业务场景可追溯验收、全量回归和真实 GLM 结果记录均已完成并有真实测试或真实运行证据。

但按保守口径，本报告将总体状态标记为 **“Phase 6 部分完成（核心交付完成）”**，原因不是核心能力缺失，而是以下请求项未能在不改变生产配置的前提下完整执行：

1. 真实 GLM 的“非法工具名 / 非法参数”受控纠正与“模型不可用”场景没有用真实模型强制触发；这些路径由脚本化模型和故障注入测试覆盖，已明确区分，不冒充真实模型结果。
2. 供应链与跨域两次真实运行中，模型正文最终文本被截断，报告的模型叙述回退为确定性文本；结构化报告仍通过全部校验，但叙述质量仍可改进。
3. 智谱 API Key 轮换状态仍未确认。
4. OpenTelemetry SDK/exporter 未部署，没有导出任何 trace。
5. 未执行真实生产服务器部署、TLS、密钥托管、监控告警和容量压测；PostgreSQL 切换是本地 PG17 容器上的生产演练。

### 1.2 工作流结果

| 工作流 | 状态 | 关键证据 |
| --- | --- | --- |
| 生产 PostgreSQL 运行时切换演练 | 完成 | `eba_runtime` 仅 DML；10 张 `agent_*` 表；应用重启后仍能读取历史 Run |
| Run Claim / Lease / Fencing | 完成 | 真实 PostgreSQL 多进程竞争仅 1 个成功；活租约返回 409；过期接管；旧 worker 写入被拒绝 |
| Run 及关联数据事务化清理 | 完成 | 10 张表单事务删除；状态区分 TTL；活动租约保护；dry-run；孤儿检测；回滚测试 |
| Run 生命周期预算 | 完成 | 累计执行时间 + 创建至今墙钟时间；多次恢复不重置；耗尽后终态 |
| 审批工作台基础 API | 完成 | 跨 Run 列表、状态/人员/时间过滤、分页排序、`approval:read` / `approval:read_any` 隔离 |
| 运行质量指标 | 完成 | `GET /api/v1/metrics` 从共享运行时数据库聚合；仅 admin 可读 |
| 真实 GLM 五场景验收 | 完成（有警告） | 5 个场景均有真实 Run / Evidence / Finding / Report；2 个场景带警告 |
| 全量回归 | 通过 | SQLite **472 passed, 25 skipped**；Phase 6 SQLite **48 passed**；PostgreSQL **25 passed** |
| 文档与 README | 完成 | README、docs/README、本报告与当前代码一致 |

---

## 2. Phase 6 总体目标与最终架构

### 2.1 目标

Phase 6 把 Phase 5 的 Evidence / Finding / Report 能力从“本地可演示”推进到“可持久化、可恢复、可并发、可治理、可审计”的运行态：

- 运行时状态不再依赖进程内存或单机 SQLite；
- 多个 API 进程不能同时恢复同一个 Run；
- 崩溃后可以从 Checkpoint 继续，且已完成工具不重复执行；
- Run 的关联数据可以按策略安全清理，不误删活动任务；
- 高风报告必须人工审批，审批绑定报告版本和指纹；
- 运行质量可以从共享数据库观测；
- 真实模型、真实 PostgreSQL、真实多进程并发都有可复核的证据。

### 2.2 最终架构

```text
Client
  │  X-API-Key / anonymous dev mode
  ▼
FastAPI (api/app.py, api/routes.py)
  │  authentication → scope → resource ownership → authorization policy
  ▼
RunManager (api/run_manager.py)
  │  submit / resume / cancel / heartbeat / lifecycle budget
  ├──────────────► AgentRuntime (单 Agent + LangGraph decide → act → decide)
  │                   │  Function Calling
  │                   ▼
  │                ToolRegistry (只读工具, Pydantic 参数契约)
  │                   │
  │                   ▼
  │                AnalysisGateway → SemanticQueryGuard → SafeQueryExecutor
  │                   │
  │                   ▼
  │                PostgreSQL 业务库（eba_readonly，仅 SELECT）
  │
  ├──────────────► PersistentRunStore (api/run_store.py)
  │                   │  Run / Audit / Tool Result / Checkpoint / Evidence /
  │                   │  Finding / Report / Approval / Approval Event
  │                   ▼
  │                PostgreSQL 运行时库（eba_runtime，仅 agent_* 表 DML）
  │
  └──────────────► SqlAlchemyCheckpointSaver（与 Run Store 同库）
```

关键边界：

- 模型只提出工具调用，不持有数据库连接，不生成可执行 SQL；
- Agent 不自动执行采购、调价、预算、人事等业务动作；
- Guard、Numeric Validator、权限体系和 Evidence Chain 均未被绕过；
- 运行时 Store 在 PostgreSQL 模式启动时检查 `agent_runs` 是否存在，缺失直接报错，不回退 SQLite；
- 单 Agent + LangGraph + Function Calling + Tool Registry 架构保持不变，没有引入 Kafka、Kubernetes 或微服务。

---

## 3. Batch 1 / Batch 2 / Batch 3 完成情况

### 3.1 Batch 1：基础质量与安全治理

- 创建并验证 PostgreSQL 最小权限角色：`eba_admin`（初始化，非超级用户）与 `eba_readonly`（业务只读）。
- API 业务查询切换到 `eba_readonly`；验证 `sales_fact` 可读 3072 行，写入和 DDL 均被拒绝。
- Numeric Validator 修复误报：区分 `business_result`、`tool_parameter`、`date_component`、`unsupported_number`。
- 建立后端业务权限：部门域、指标、维度、区域、工具、Run / Evidence / Finding / Report 均在后端校验。
- 结果：全量 `434 passed, 11 skipped`；PostgreSQL 专项 `11 passed`。

### 3.2 Batch 2：运行可靠性与人工审批

- 新增持久化 Checkpoint（`agent_checkpoints`、`agent_checkpoint_writes`），SQLite 与 PostgreSQL 均支持保存、重开、恢复。
- 恢复时重新认证、重新授权；已完成工具从结果缓存重放，不重复执行。
- 时间、工具次数、Token、连续失败、单工具超时、模型/工具重试均有上限。
- 实现人工审批状态机：`pending / approved / rejected / cancelled`；报告按 `(run_id, report_version)` 版本化，审批绑定 `report_id`、`report_version`、`report_fingerprint`；禁止报告所有者审批自己的报告。
- 结果：全量 `456 passed, 16 skipped`；Phase 6 定向 `22 passed`；PostgreSQL 专项 `16 passed`。

### 3.3 Batch 3：生产化验收、并发安全与最终综合测试

- 生产运行时切换：创建 `eba_runtime`，只授予 `agent_*` 表 DML，不授予 DDL、超级用户、CREATEDB、CREATEROLE；业务表不可读。
- 数据库级 Run Claim/Lease/Fencing：条件 UPDATE 原子抢占、租约续期、fencing token、409 冲突、过期接管、旧 worker 写入拒绝。
- 事务化清理：覆盖 10 张运行时表，按状态区分 TTL，保护活动租约，支持 dry-run 和孤儿检测。
- 生命周期预算：累计执行时间与创建至今墙钟时间共同限制，多次 Resume 不会延长总预算。
- 审批工作台：跨 Run 列表、过滤、分页、排序、权限隔离。
- 运行指标：从共享数据库计算 Run / Tool / Model / Checkpoint / Validation / Approval / DB 指标。
- 真实 GLM 五场景验收与全量回归。
- 结果：SQLite 全量 `472 passed, 25 skipped`；Phase 6 SQLite `48 passed`；PostgreSQL `25 passed`；真实 GLM 10 次运行尝试（9 个 Run + 1 次最小连通性调用）。

---

## 4. 最终 PostgreSQL 与角色架构

### 4.1 角色分层

| 角色 | 用途 | 权限边界 | 验证 |
| --- | --- | --- | --- |
| `eba_admin` | 初始化、建表、生成数据 | LOGIN；NOSUPERUSER / NOCREATEDB / NOCREATEROLE；数据库所有者 | 只用于 setup；API 不使用 |
| `eba_readonly` | API 业务查询 | LOGIN；仅 CONNECT / USAGE / SELECT；`default_transaction_read_only=on`；`statement_timeout=15s`；`lock_timeout=3s` | 可读 `sales_fact`；写和 DDL 被 `42501` 拒绝 |
| `eba_runtime` | Agent 运行时状态 | LOGIN；NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOINHERIT；仅 `agent_*` 表 SELECT/INSERT/UPDATE/DELETE；无 schema CREATE；业务表不可读 | 角色属性 `(False, False, False)`；DML 通过；`CREATE TABLE` 被拒绝；`orders/order_items/customers` 被拒绝 |
| `mark`（容器超级用户） | 仅在本地安装/应急时使用 | 不作为应用账号 | 未写入任何应用配置 |

### 4.2 运行时表

PostgreSQL 运行时库共 10 张 `agent_*` 表：

```text
agent_runs
agent_audit_events
agent_tool_results
agent_checkpoints
agent_checkpoint_writes
agent_evidence
agent_findings
agent_reports
agent_report_approvals
agent_report_approval_events
```

### 4.3 运行时连接加固

`PersistentRunStore` 的 PostgreSQL 引擎设置：

- `application_name=eba_run_store`
- `connect_timeout=5`
- `statement_timeout=30000`
- `lock_timeout=10000`
- `idle_in_transaction_session_timeout=30000`
- `pool_pre_ping=True`

### 4.4 切换演练结果

- `.env` 中 `RUN_STORE_URL` 已切换为 `postgresql+psycopg://eba_runtime:<redacted>@localhost:5432/enterprise_business_agent`。
- 真实 uvicorn 使用 `--env-file .env` 启动，`/health` 返回 `dialect=postgresql`、`readonly=true`、`run_store=postgresql`。
- 应用重启后仍能读取切换前创建的 Run，证明 Run / Checkpoint 不依赖进程内存。
- 生产运行时库中 10 张表均有真实数据：`agent_runs=9`、`agent_audit_events=129`、`agent_tool_results=17`、`agent_checkpoints=63`、`agent_checkpoint_writes=702`、`agent_evidence=17`、`agent_findings=17`、`agent_reports=8`、`agent_report_approvals=8`、`agent_report_approval_events=8`（截至本报告生成前）。
- 缺失 schema 或不存在的 PostgreSQL 端口直接抛错；测试断言错误信息包含 `scripts/init_runtime_store.py` 且不包含 `sqlite`。

### 4.5 备份与回滚

切换前备份位于 `backups/20261031_batch3/`，权限 `600`：

| 文件 | SHA-256 |
| --- | --- |
| `env.backup` | `f74b707fc19ee5b2416868b6e5db290b47589b363144d02eb99db6e6b1121f18` |
| `enterprise_business_agent_runs.db.backup` | `0d9c43d7216d4aac43c74263f735d1657e533075e7837e2a1f9641ab15dda215` |
| `enterprise_business_agent.db.backup` | `2ae371249643eb877567a2bada23cc85a3584336fa8c77ff7245d2f2be09c6bf` |

原始 SQLite 数据库文件未删除、未覆盖。回滚步骤见第 15 节。

---

## 5. Run Store / Checkpoint / Evidence / Finding / Report / Approval 数据关系

所有运行时数据以 `run_id` 为主关联键：

| 表 | 主键 / 唯一约束 | 关联 | 说明 |
| --- | --- | --- | --- |
| `agent_runs` | `run_id` | 根对象 | 状态、问题、owner、payload、claim、生命周期字段 |
| `agent_audit_events` | `event_id` | `run_id` 可空 | 模型、工具、审批、清理审计；系统清理事件 `run_id=NULL` |
| `agent_tool_results` | `(run_id, call_key)` | `run_id` | 工具幂等缓存，恢复时重放 |
| `agent_checkpoints` | `(thread_id, checkpoint_ns, checkpoint_id)` | `thread_id = run_id` | LangGraph 状态快照 |
| `agent_checkpoint_writes` | `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)` | `thread_id = run_id` | Checkpoint 中间写入 |
| `agent_evidence` | `evidence_id`；`(run_id, fingerprint)` 去重 | `run_id`、`tool_call_id` | 可追溯证据，记录指标口径、筛选、结果、来源表 |
| `agent_findings` | `finding_id` | `run_id`、`evidence_ids` | 七类发现，必须引用证据 |
| `agent_reports` | `report_id`；`(run_id, report_version)` 唯一 | `run_id` | 结构化报告与校验结果；版本只追加 |
| `agent_report_approvals` | `approval_id`；`(report_id, report_version)` 绑定 | `run_id`、`report_id` | 审批状态、指纹、申请人/审批人 |
| `agent_report_approval_events` | `event_id` | `approval_id` | 状态流转审计 |

一致性规则：

- 报告版本只追加，不覆盖历史版本；新版本不会继承旧审批。
- 审批决定必须同时匹配 `report_id`、`report_version`、`report_fingerprint`；指纹不一致返回冲突。
- 报告所有者不能审批自己的报告；审批人与申请人分离。
- 数字必须通过 `ValueReference` / `evidence_id` / `finding_id` 指向证据；Numeric Validator 不因报告版本更新而关闭。
- 清理一个 Run 时，上述 10 张表中与该 Run 相关的行在同一事务内删除；系统级审计事件不随 Run 删除。

---

## 6. Run Claim / Lease / Fencing 设计

### 6.1 字段

`agent_runs` 新增：

- `claim_owner`：worker ID（`host:pid:random`）
- `claim_version`：单调递增的 fencing token
- `claimed_at`：最近一次成功 Claim 时间
- `lease_expires_at`：租约到期时间
- `execution_seconds`：累计执行时间
- `last_resumed_at`：最近恢复时间
- `resume_count`：恢复次数

### 6.2 抢占

`PersistentRunStore.acquire_claim()` 使用一条条件 `UPDATE` 作为数据库锁：

```sql
UPDATE agent_runs
SET claim_owner = :worker_id,
    claimed_at = :now,
    lease_expires_at = :expires,
    claim_version = claim_version + 1,
    updated_at = :now
WHERE run_id = :run_id
  AND status IN (:allowed_statuses)
  AND (lease_expires_at IS NULL OR lease_expires_at < :now)
```

- 两个 worker 同时执行时，只有一个能匹配 `lease_expires_at IS NULL OR < now`。
- 成功后返回 `RunClaim(run_id, worker_id, claim_version, lease_expires_at)`；`claim_version` 是后续所有写入的 fencing token。
- 如果租约仍有效，返回 `lease_held`，API 映射为 **HTTP 409**，响应包含 `run_claim_conflict` 与 `held_by`。
- 如果 Run 状态不可恢复，返回 `not_resumable`，同样 409。
- Claim 成功后才进入 Resume；Claim 失败不会执行任何恢复逻辑。

### 6.3 续约与释放

- 后台心跳线程每 `lease_seconds / 3` 续约一次；默认租约 60 秒，即约 20 秒续约一次。
- 续约条件同时匹配 `run_id + claim_owner + claim_version`；任一项不匹配即视为租约丢失。
- 完成、失败、取消、恢复异常结束时释放 Claim（`claim_owner=NULL, lease_expires_at=NULL`）。
- 进程崩溃后不主动释放，租约自然过期；其他 worker 可接管并递增 `claim_version`。

### 6.4 Fencing

所有 Run 状态写入都带 Claim 条件：

```sql
WHERE run_id = :run_id
  AND claim_owner = :worker_id
  AND claim_version = :claim_version
```

- 旧 worker 在租约丢失后继续写入时，`claim_version` 已变化，写入影响 0 行，抛出 `RunClaimLost`（`code=run_claim_lost`）。
- 新 worker 的结果不会被旧 worker 覆盖。
- 测试覆盖：活租约 409、过期接管、旧 fence 写入失败、正常完成释放、崩溃接管。

### 6.5 与权限体系的关系

Resume 前仍执行完整认证与授权：

- 当前 principal 必须有 `agent:run`；
- 必须有该 Run 的读取权限（owner 或 `resource:read_any`）；
- 恢复后的工具调用按当前角色重新校验，不能因为缓存了旧结果就跳过权限；
- 低权限角色即使命中了高权限角色的缓存结果，也不能复用。

---

## 7. Run 生命周期预算与恢复

### 7.1 持久化字段

复用 `agent_runs` 既有字段，不新增重复字段：

- `created_at`：Run 创建时间
- `execution_seconds`：累计执行时间（跨启动、跨恢复累加）
- `last_resumed_at`：最近恢复时间
- `resume_count`：恢复次数

### 7.2 生命周期计算

每次执行/恢复时：

```text
consumed = max(execution_seconds, now - created_at)
remaining = max(0, agent_run_max_lifecycle_seconds - consumed)
effective_seconds = min(requested_per_attempt, agent_max_seconds, remaining)
```

- **单次尝试预算**：`requested_per_attempt` 与 `agent_max_seconds` 的较小值。
- **Run 生命周期总预算**：`agent_run_max_lifecycle_seconds`，默认 3600 秒。
- 恢复不会重置 `created_at`；之前消耗的执行时间和墙钟时间都会计入。
- 生命周期耗尽时返回 `budget_exhausted`，写入 `run_lifecycle_exhausted` 审计事件，不继续调用模型。
- Token、工具调用次数、连续失败预算仍由 Agent Runtime 统一执行，Checkpoint 与 Run Store 使用同一份计数。

### 7.3 恢复流程

1. 重新认证、重新授权；
2. 数据库 Claim（条件 UPDATE，失败 409）；
3. 启动租约心跳；
4. 读取 Checkpoint；没有 Checkpoint 返回结构化失败；
5. 已完成工具从 `agent_tool_results` 重放，不重复执行；
6. 继续执行剩余步骤；
7. 写入最终状态、释放 Claim。

测试覆盖多次恢复、预算耗尽、进程崩溃接管、无 Checkpoint 恢复失败、权限变化后的缓存拒绝。

---

## 8. Run 及关联数据事务化清理

### 8.1 清理范围

一次清理覆盖 10 张表：

```text
agent_runs
agent_audit_events
agent_tool_results
agent_checkpoints
agent_checkpoint_writes
agent_evidence
agent_findings
agent_reports
agent_report_approvals
agent_report_approval_events
```

### 8.2 策略

| 状态 | 默认保留期 | 选择条件 |
| --- | --- | --- |
| `completed` / `completed_with_warnings` | 30 天 | `status` + `updated_at` 早于截止时间 |
| `failed` 及失败类终态 | 14 天 | `status` + `updated_at` 早于截止时间 |
| `cancelled` | 7 天 | `status` + `updated_at` 早于截止时间 |
| 长时间未完成 | 10 天 | `created_at` 早于截止时间，且租约不存在或已过期 |

- 有有效租约的 Run 永不被选中，无论其状态或时间。
- 每个 Run 及其所有关联行在同一数据库事务内删除；任一语句失败整体回滚。
- 删除成功后写入 `run_cleanup` 审计事件，记录清理时间、Run 数量、各表删除数量、截止时间。
- 删除失败写入失败审计事件，记录异常类型，不伪造成功。
- `dry_run=True` 执行相同的选择和计数，但事务回滚，不删除任何行。
- 孤儿检测覆盖：审计事件、工具结果、Checkpoint、Checkpoint Writes、Evidence、Finding、Report、Approval、Approval Event 缺少有效 Run / Report / Approval 的情况。
- 若事务或顺序存在风险，优先保证数据安全，不做强制级联删除。

### 8.3 启动行为

API 启动时：

1. 将遗留的 `running` Run 标记为 `interrupted`；
2. 按配置执行一次清理（默认非 dry-run，可在 `.env` 中设置 `RUN_CLEANUP_DRY_RUN=true`）；
3. 记录 `api_started` 日志，包含清理状态、Run 数量、是否 dry-run。

### 8.4 测试

- SQLite：删除 10 张表关联数据、dry-run 不删除、活动租约保护、状态区分 TTL、单语句失败整体回滚、孤儿检测。
- PostgreSQL：真实数据库上的 10 表清理、dry-run、活动租约、回滚触发器。
- 结果：SQLite `test_phase6_cleanup.py` 6 passed；PostgreSQL Batch 3 清理测试通过。

---

## 9. 审批工作台基础 API 与权限

### 9.1 端点

```text
GET /api/v1/agent/approvals
```

查询参数：

- `state`：可重复，支持 `not_required / pending / approved / rejected / cancelled`
- `approver`：按审批人过滤
- `requester`：按申请人过滤
- `created_from` / `created_to`：按创建时间过滤
- `page` / `page_size`：分页，`page_size` 最大 100
- `sort_by`：`requested_at / created_at / updated_at / decided_at / state / report_version`
- `sort_dir`：`asc / desc`

响应：

- `items`：审批记录，附带 `wait_seconds`
- `total` / `page` / `page_size` / `pages`
- `sort_by` / `sort_dir`
- `scope`：`all` 或 `self`

### 9.2 权限

| Scope | 角色（默认） | 可见范围 |
| --- | --- | --- |
| `approval:read` | 所有含读取 scope 的角色 | 自己申请或自己审批的记录 |
| `approval:read_any` | `admin`、`business_manager` | 全部记录 |

- 没有 `approval:read` 返回 403。
- 普通审批人不能通过查询参数越权查看他人的审批。
- 审批决定仍绑定 `report_id`、`report_version`、`report_fingerprint`。
- 报告所有者不能审批自己的报告。
- 没有自动审批；没有采购、调价、预算、人事执行接口。
- 未增加邮件、IM 或消息队列基础设施。

### 9.3 决策端点

审批决定继续使用原有端点，新增工作台只负责查询，不改变状态机：

```text
POST /api/v1/agent/runs/{run_id}/report/approval/decision
```

测试覆盖：跨 Run 列表、状态/人员/时间过滤、分页排序、普通审批人 scope 隔离、admin/business manager 全量可见、403、版本/指纹绑定、所有者隔离。

---

## 10. 五大业务场景真实验收

所有场景均使用：

- 真实 GLM `glm-5.3-flash`；
- 真实 PostgreSQL 业务库（`eba_readonly`）与 PostgreSQL 运行时库（`eba_runtime`）；
- 真实 uvicorn API、异步 Run、Checkpoint、Evidence、Finding、Report、Approval。

### 10.1 验收结果

| 场景 | Run ID | 状态 | 工具调用 | Evidence / Finding | 报告 | 审批 |
| --- | --- | --- | --- | --- | --- | --- |
| 销售变化与下滑归因 | `9561445a-0f6a-43b3-88cf-73f693ee9220` | `completed` | `compare_periods`、`contribution`（2） | 2 / 2 | `validated`，10 项校验通过，2 条建议 | `pending`（高风险建议需人工审批） |
| 营销 ROI | `211ee427-367b-4afa-a3c8-40108bd203c6` | `completed` | `top_n`×2、`query_metric`（3） | 3 / 3 | `validated`，0 条建议 | `not_required` |
| 供应链库存与周转 | `5500beea-3584-482e-af99-872464ca9fce` | `completed_with_warnings` | `query_metric`、`top_n`×2（3） | 3 / 3 | `validated`，0 条建议 | `not_required` |
| 跨域五业务领域 | `532a3060-d388-40f8-9adb-4189d801de7d` | `completed_with_warnings` | `query_metric`×6（6） | 6 / 6 | `validated`，5 个 domain_analysis | `not_required` |
| 不支持预测 | `c8f5619f-aaec-4cea-ad69-e6a02625bf25` | `completed` | `query_metric`（1） | 1 / 1 | `validated`，明确拒绝预测 | `not_required` |

### 10.2 场景说明

**销售变化与下滑归因**

- 先用 `compare_periods` 确认 2025-12 相对 2025-11 销售额变化：`-44,670,744.42`，变化率 `-0.358549`。
- 再用 `contribution` 按区域拆解：NC、SC、EC 为主要贡献区域。
- 报告所有业务数字均通过 `ValueReference` 指向证据；10 项校验全部通过。
- 2 条建议均标记 `requires_approval=true`，审批状态 `pending`，证明高风险建议不会自动执行。

**营销 ROI**

- 通过降序和升序 `top_n` 交叉验证最高/最低渠道。
- 模型明确说明：工具只返回了部分渠道，其余渠道的 ROI “未验证”，没有编造缺失数据。
- 报告 `validated`，无高风险建议，因此审批 `not_required`。

**供应链库存与周转**

- 查询库存周转率，并用 `top_n` 定位最低品类：P004、P005。
- 模型多次调用工具时一轮返回 2 个调用，系统只执行第一个并记录警告，其余被忽略；这是既有的受控多调用策略。
- 报告 `validated`；最终正文被截断（104 字符），但结构化报告完整并通过校验。

**跨域五业务领域**

- 使用销售、营销、供应链、财务、人力资源（HR）五个项目内实际存在的业务域。
- 6 次 `query_metric` 调用，6 条 Evidence、6 条 Finding，报告包含 5 个 `domain_analysis`（Finance、HR、Marketing、Sales、Supply Chain）。
- 模型一轮返回 3 个工具调用，系统只执行第一个并记录警告。
- 报告叙述生成失败后回退到确定性文本；`validated` 通过，但模型正文被截断（1169 字符）。这是一次真实的质量警告，不是伪造成功。
- 早先一次使用“客户”作为第五域的尝试只得到 4 个域；客户不是本项目建模的五域之一，因此未计入最终验收。

**不支持预测**

- 模型正确拒绝预测 2026 年销售额，明确说明外推会构成编造。
- 仅返回已验证的 2025 年历史销售额，并将 2026 年目标、渠道规划等列为无法验证的输入假设。
- 报告 `validated`，没有给出预测数字。

### 10.3 可追溯性

以上每个 Run 均可通过以下链路复核：

```text
Run → Tool Calls → Tool Results → Evidence → Findings → Report → Report Approval → Audit Events
```

Run、工具结果、证据、发现、报告、审批和审计事件均持久化在 PostgreSQL 运行时库；服务重启后仍可读取。

---

## 11. 真实 GLM 调用统计

### 11.1 最终五场景

| Run | 状态 | 执行时间 | 输入 tokens | 输出 tokens | 工具数 |
| --- | --- | --- | --- | --- | --- |
| 销售 | `completed` | 62.87s | 19,068 | 1,354 | 2 |
| 营销 | `completed` | 85.49s | 21,332 | 2,222 | 3 |
| 供应链 | `completed_with_warnings` | 137.33s | 21,589 | 4,530 | 3 |
| 跨域 | `completed_with_warnings` | 104.98s | 42,566 | 3,827 | 6 |
| 不支持预测 | `completed` | 54.15s | 12,471 | 981 | 1 |
| **合计** | 4 成功 / 2 带警告 | **444.81s** | **117,026** | **12,914** | **15** |

最终五场景平均执行时间 88.96 秒，最短 54.15 秒，最长 137.33 秒。

### 11.2 全部真实调用（含调试与异常观察）

| Run | 状态 | 执行时间 | 输入 / 输出 tokens | 说明 |
| --- | --- | --- | --- | --- |
| `c99facca` | `llm_timeout` | 121.77s | 12,528 / 469 | 早期 60 秒单次预算下的超时观察，含 1 次恢复 |
| `7cc9398c` | `llm_timeout` | 81.01s | 19,092 / 1,454 | 真实模型超时，终态与审计持久化 |
| `9561445a` | `completed` | 62.87s | 19,068 / 1,354 | 销售最终验收 |
| `211ee427` | `completed` | 85.49s | 21,332 / 2,222 | 营销最终验收 |
| `5500beea` | `completed_with_warnings` | 137.33s | 21,589 / 4,530 | 供应链最终验收 |
| `22d5d14c` | `invalid_action` | 86.54s | 0 / 0 | 真实模型连续两次空响应，重试 1 次后终态 |
| `c8f5619f` | `completed` | 54.15s | 12,471 / 981 | 不支持预测验收 |
| `266a0e96` | `completed` | 176.58s | 42,557 / 6,127 | 使用“客户”作为第五域的诊断运行，4 个域，不计入最终五域验收 |
| `532a3060` | `completed_with_warnings` | 104.98s | 42,566 / 3,827 | 最终五域验收 |

**全部真实 Run 合计**：9 个 Run，191,203 输入 tokens，20,964 输出 tokens，共 212,167 tokens；执行时间合计约 911 秒。

**真实 HTTP 模型调用**：

- 成功响应：32 次
- 失败响应：5 次（超时 2 次、空响应 2 次、其他 1 次）
- 重试事件：2 次
- 另有 1 次最小连通性调用成功
- 合计约 38 次真实调用

### 11.3 真实异常观察

| 异常 | 真实模型结果 | 处理 |
| --- | --- | --- |
| 超时 | `7cc9398c` 在 60 秒预算下超时；`c99facca` 也在早期配置下超时 | 有界重试；最终 `llm_timeout`；Run / 审计 / 已完成 Evidence 保留 |
| 空响应 | `22d5d14c` 连续两次返回空内容 | 第 1 次重试，第 2 次终止为 `invalid_action`（`llm_invalid_response`）；没有伪造结果 |
| 重复失败终态 | 同上 | 终态明确，报告与审批记录保留 |
| 多工具调用 | 供应链一轮 2 个、跨域一轮 3 个 | 只执行第一个，其余忽略并写入 warning；不重复执行 |
| 正文截断 | 供应链、跨域最终正文被截断 | 结构化报告仍完整；跨域模型叙述回退为确定性文本，并记录 warning |

### 11.4 未用真实模型执行的异常

以下场景无法在不改变生产提示词、不破坏密钥/网络的前提下用真实 GLM 强制触发，**未冒充真实模型结果**：

- 非法工具名受控纠正；
- 非法参数受控纠正；
- 模型完全不可用（连接失败/认证失败）。

这些路径由脚本化模型和故障注入测试覆盖，见 `tests/test_phase6_runtime.py`；测试明确使用假模型，不产生真实模型调用记录。

---

## 12. 全量测试与专项测试

### 12.1 最终回归

| 测试类型 | 命令 | 结果 |
| --- | --- | --- |
| SQLite 全量单元与回归 | `python -m pytest -q` | **472 passed, 25 skipped in 39.61s** |
| Phase 6 SQLite 定向 | 6 个 Phase 6 SQLite 测试文件 | **48 passed in 12.14s** |
| PostgreSQL 真实集成 | 5 个 PostgreSQL 测试文件 | **25 passed** |

### 12.2 PostgreSQL 专项拆分

| 文件 | 结果 | 类型 |
| --- | --- | --- |
| `tests/test_postgres_integration.py` | 7 passed | 真实 PostgreSQL 集成 |
| `tests/test_phase5_postgres.py` | 4 passed | 真实 PostgreSQL + Phase 5 持久化 |
| `tests/test_phase6_postgres_runtime.py` | 5 passed | 真实 PostgreSQL 运行时 + 角色权限 |
| `tests/test_phase6_postgres_batch3.py` | 7 passed | 真实 PostgreSQL 多进程 Claim、清理、Fencing、运行时角色 |
| `tests/test_phase6_cutover.py` | 2 passed | 真实 PostgreSQL 切换守卫 |

### 12.3 测试类型分离

| 类型 | 覆盖 |
| --- | --- |
| 单元测试 | 语义解析、SQL Guard、Numeric Validator、报告校验、权限策略 |
| 真实 SQLite | Run Store、Checkpoint、清理、审批、工作台、指标 |
| 真实 PostgreSQL 集成 | 事务、版本、角色权限、清理、Checkpoint |
| 多进程并发 | `multiprocessing` + Barrier 同时 Claim，仅 1 个成功 |
| 脚本化 LLM | 工具选择、非法工具名/参数纠正、空响应、超时、连续失败 |
| 故障注入 | PostgreSQL 不可达、schema 缺失、回滚触发器、模型连接失败 |
| 真实 GLM | 五场景、超时、空响应、多调用、正文截断 |
| PostgreSQL + 真实 GLM 联合 | 五场景全部在真实 PG 运行时库上完成 |

没有删除测试、弱化断言或把 Mock 当作 PostgreSQL/真实恢复测试。

---

## 13. 可观测性与运行质量指标

### 13.1 指标端点

```text
GET /api/v1/metrics
```

仅 `admin` 可读；数据来自共享运行时数据库，而不是进程内计数器。

覆盖：

- Run：总数、状态分布、成功/失败率、平均/P95 执行时间、恢复次数、恢复成功率；
- Tool：调用总数、平均每 Run 调用数、失败率、状态分布；
- Model：模型响应数、重试数、错误数、输入/输出/总 Token；
- Checkpoint：数量、存储字节、平均大小；
- Validation：报告数、通过数、通过率、Numeric Validator 拦截数、未溯源数字数、Evidence 覆盖率、Finding 数；
- Approval：总数、状态分布、待审批数、平均等待时间、平均决策时间；
- Database：后端类型、进程内连接错误数（明确标注 `process_local`）；
- Observability：是否启用 OTel、SDK 是否可用、是否配置 exporter、是否导出 span。

### 13.2 实际部署状态

- **OpenTelemetry exporter 未部署**：本机没有安装/配置 SDK 与 exporter，`exported_spans=false`，没有 trace 被导出到任何后端。
- 结构化 JSON 日志、审计事件、指标端点已实现并可用。
- 报告、README 和指标端点均明确说明“未导出 trace”，不宣称已接入 Jaeger、Tempo、Langfuse 或其他后端。

---

## 14. 性能、可靠性与安全边界

### 14.1 可靠性

- 数据库级 Claim 避免多进程重复恢复；
- 租约过期允许崩溃接管，fencing 阻止旧 worker 覆盖新结果；
- Checkpoint + 工具结果缓存保证恢复不重复执行已完成工具；
- 清理在单事务内完成，失败整体回滚；
- PostgreSQL 不可达或 schema 缺失时明确失败，不回退 SQLite；
- API 重启将遗留 `running` 标记为 `interrupted`，不假装任务仍在运行。

### 14.2 性能边界

- 本报告没有做生产级容量压测；执行时间来自真实单机运行，不是基准测试。
- PostgreSQL 运行时连接设置 `statement_timeout=30s`、`lock_timeout=10s`、`connect_timeout=5s`。
- Agent 单次尝试默认最大 180 秒；Run 生命周期默认最大 3600 秒；工具调用次数默认 6，最大 20。
- 真实 GLM 五场景平均 89 秒，受模型网络延迟影响明显；跨域 6 次工具调用约 105 秒。
- 未验证多副本 API 的吞吐、连接池上限、数据库连接数与高并发下的 P95。

### 14.3 安全边界

- `eba_runtime` 不是超级用户，不能 DDL，不能读业务表；
- `eba_readonly` 只能 SELECT，写入和 DDL 被数据库拒绝；
- 模型不持有数据库凭据，不直接执行 SQL；
- 工具参数经过 Pydantic 契约与权限检查；
- 报告数字经过 Numeric Validator，未溯源数字被拦截或标记；
- 审批绑定报告版本和指纹，报告所有者不能自批；
- 没有自动执行采购、调价、预算、人事动作；
- 未引入 RLS / 列级权限；当前是应用层角色 + 数据库授权，不是数据库行级安全。

---

## 15. 未完成项、风险、上线前检查与操作命令

### 15.1 未完成项与风险

| 项 | 状态 | 影响 | 建议 |
| --- | --- | --- | --- |
| 真实 GLM 非法工具名/参数、模型不可用 | 未用真实模型执行 | 仅脚本化/故障注入覆盖 | 在隔离测试环境用可替换模型端点做真实故障演练，不改生产提示词 |
| 供应链/跨域正文截断 | 已观察到 | 结构化报告完整，正文不完整 | 调整模型输出上限或分段叙述；对截断增加显式 warning |
| 跨域模型叙述回退 | 已观察到 | 报告回退为确定性文本 | 检查叙述生成的 JSON/长度限制，增加重试与更明确的降级提示 |
| API Key 轮换 | 未确认 | 密钥曾写入本地文件 | 上线前在智谱控制台轮换，更新 `.env` 后重启，不复用旧 key |
| OpenTelemetry exporter | 未部署 | 无外部 trace | 安装 SDK/exporter，配置 OTLP endpoint，验证 span 导出 |
| 数据库 RLS / 列级权限 | 未实现 | 依赖应用层授权 | 如合规要求，增加 RLS 与列级授权 |
| 生产部署 | 未执行 | 仅本地 PG17 演练 | 完成 TLS、密钥托管、备份、监控、告警、容量测试 |
| 容量/并发压测 | 未执行 | 未知高并发上限 | 多副本 + 连接池 + PG 压测，记录 P95 与错误率 |

### 15.2 上线前检查清单

- [ ] 轮换 GLM API Key，确认旧 key 失效。
- [ ] 确认 `.env` 权限为 `600`，且未进入版本库。
- [ ] 确认 `RUN_STORE_URL` 使用 `eba_runtime`，`READONLY_DATABASE_URL` 使用 `eba_readonly`。
- [ ] 运行 `scripts/init_runtime_store.py`，确认 10 张表存在。
- [ ] 验证 `eba_runtime` 不能 DDL、不能读业务表。
- [ ] 验证 `eba_readonly` 只能 SELECT。
- [ ] 先以 `RUN_CLEANUP_DRY_RUN=true` 启动一次，核对清理数量。
- [ ] 检查 `/health`：`dialect=postgresql`、`readonly=true`、`run_store=postgresql`。
- [ ] 检查 `/api/v1/metrics` 仅 admin 可读，且 `exported_spans=false` 与实际情况一致。
- [ ] 跑全量测试、PostgreSQL 专项、多进程 Claim 测试。
- [ ] 准备 SQLite 备份与回滚脚本。
- [ ] 配置外部监控；在 OTel 未部署前不要宣称有分布式追踪。

### 15.3 启动与初始化

```bash
# 1. 安装依赖
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 初始化业务库（setup 角色）
.venv/bin/python scripts/init_db.py

# 3. 初始化运行时库（setup 角色；密码只从环境变量读取）
EBA_RUNTIME_SETUP_URL='postgresql+psycopg://eba_admin:<redacted>@localhost:5432/enterprise_business_agent' \
EBA_RUNTIME_ROLE=eba_runtime \
EBA_RUNTIME_PASSWORD='<redacted>' \
.venv/bin/python scripts/init_runtime_store.py

# 4. 启动 API（从 .env 读取运行时与只读连接）
.venv/bin/python -m uvicorn enterprise_business_agent.api.app:create_app \
  --factory --env-file .env --host 0.0.0.0 --port 8000
```

### 15.4 测试命令

```bash
# 全量 SQLite 回归
.venv/bin/python -m pytest -q

# Phase 6 SQLite 定向
.venv/bin/python -m pytest \
  tests/test_phase6_runtime.py \
  tests/test_phase6_cleanup.py \
  tests/test_phase6_approvals.py \
  tests/test_phase6_permissions.py \
  tests/test_phase6_workbench.py \
  tests/test_phase6_metrics.py -q

# PostgreSQL 专项（使用一次性测试库和测试角色）
EBA_TEST_POSTGRES_URL='postgresql+psycopg://<test-admin>@localhost:5432/eba_phase6_test' \
EBA_TEST_POSTGRES_RUNTIME_URL='postgresql+psycopg://<test-runtime>@localhost:5432/eba_phase6_test' \
EBA_TEST_POSTGRES_READONLY_URL='postgresql+psycopg://<test-readonly>@localhost:5432/enterprise_business_agent' \
.venv/bin/python -m pytest \
  tests/test_postgres_integration.py \
  tests/test_phase5_postgres.py \
  tests/test_phase6_postgres_runtime.py \
  tests/test_phase6_postgres_batch3.py \
  tests/test_phase6_cutover.py -q
```

### 15.5 手工清理与 dry-run

```bash
# 以 dry-run 方式查看将被清理的 Run 数量
RUN_CLEANUP_DRY_RUN=true .venv/bin/python - <<'PY'
from enterprise_business_agent.config import load_env_file, get_settings
from enterprise_business_agent.api.run_store import PersistentRunStore

load_env_file(override=False)
settings = get_settings()
store = PersistentRunStore(settings.run_store_url)
result = store.cleanup_runs(
    dry_run=True,
    retention_seconds={
        "completed": settings.run_retention_completed_seconds,
        "failed": settings.run_retention_failed_seconds,
        "cancelled": settings.run_retention_cancelled_seconds,
        "unfinished": settings.run_retention_unfinished_seconds,
    },
    batch_limit=settings.run_cleanup_batch_limit,
)
print(result)
store.close()
PY

# 孤儿检测
.venv/bin/python - <<'PY'
from enterprise_business_agent.config import load_env_file, get_settings
from enterprise_business_agent.api.run_store import PersistentRunStore

load_env_file(override=False)
store = PersistentRunStore(get_settings().run_store_url)
print(store.detect_orphans())
store.close()
PY
```

### 15.6 回滚到 SQLite

回滚前先停止 API，确认没有正在运行的 Run。

```bash
# 1. 停止 API 进程（按实际 PID）
#    kill <api_pid>

# 2. 恢复切换前的 .env
cp backups/20261031_batch3/env.backup .env
chmod 600 .env

# 3. 确认 RUN_STORE_URL 指向原 SQLite 文件
grep '^RUN_STORE_URL=' .env

# 4. 确认 SQLite 文件仍在原位置且未被覆盖
ls -l enterprise_business_agent_runs.db
shasum -a 256 enterprise_business_agent_runs.db

# 5. 启动 API，检查 /health
.venv/bin/python -m uvicorn enterprise_business_agent.api.app:create_app \
  --factory --env-file .env --host 127.0.0.1 --port 8000
curl -s http://127.0.0.1:8000/health
```

回滚原则：

- 不删除 PostgreSQL 运行时表，保留再次切换的数据；
- 不覆盖原始 SQLite 数据库文件；
- 如需从备份恢复，先复制到新文件，再改 `RUN_STORE_URL`，不要直接覆盖现有文件；
- 回滚后运行 `/health` 和一次只读 Run，确认 `run_store=sqlite` 且历史 Run 可读。

---

## 16. 验收声明

- 本报告中的真实 GLM 调用均来自实际运行记录；脚本化模型、故障注入和未执行项已明确标注。
- 本报告没有打印、复制或提交数据库密码、API Key 或完整连接串。
- 没有删除用户已有数据库文件，没有覆盖 SQLite 原文件。
- 没有把 Mock 测试冒充 PostgreSQL 集成测试或多进程并发测试。
- 没有把未部署的 OpenTelemetry exporter 描述为已部署。
- 没有宣称 Agent 会自动执行采购、调价、预算或人事动作。
- 最终判定：**Phase 6 核心完成标准全部满足；按保守口径标记为“Phase 6 部分完成（核心交付完成）”，剩余项见第 15 节。**
