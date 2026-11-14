# 04 — 安全与治理

> 本文档定义 SQL Guard、数据库权限、RBAC、审计、Prompt Injection 防护和人工审批。对应 Phase 3 与 Phase 6。

---

## 1. 威胁模型

| 威胁 | 入口 | 后果 | 主要防线 |
| --- | --- | --- | --- |
| LLM 生成危险 SQL | Text-to-SQL / `run_sql` | 删表、改数据、读系统表 | AST Guard + 只读账号 + 白名单 |
| Prompt Injection | 用户问题、工具输出、RAG 文档 | 越权、泄露、执行非预期动作 | 指令层级 + 数据分隔 + 工具白名单 |
| 越权访问 | 用户角色 / 工具参数 | 看到 HR 薪资、其他区域数据 | RBAC + 行/列过滤 + DB RLS |
| 数据外泄 | 大量查询、导出 | 敏感数据泄露 | 行数上限、限流、审计、脱敏 |
| 报告幻觉 | LLM 叙述 | 错误经营建议 | Evidence Ledger + 数字校验 |
| 高危建议被自动执行 | 建议生成 | 业务损失 | 不自动执行 + 人工审批 |
| 密钥泄露 | 配置 / 日志 | 数据库被访问 | 环境变量 + 日志脱敏 + 轮换 |
| 审计缺失 | 无日志 | 无法追责 | 全链路审计 + 不可篡改存储 |

**核心原则：拒绝优先（deny by default），纵深防御（defense in depth），LLM 不持有权限。**

---

## 1.1 Phase 4 当前最小授权实现

本节描述**已经落地**的最小控制，不是第 3 节的目标 RBAC：

- `EBA_API_KEYS` / `EBA_API_KEY` 配置后，业务端点必须携带 `X-API-Key`；principal ID 使用 key 的 SHA-256 前缀，不保存原始 key；
- 角色 scope：`analyst` / `restricted_analyst` = `metrics:read` + `agent:run`；`viewer` = `metrics:read`；`admin` 额外有 `admin`；
- `EBA_AUTHZ_POLICY` 支持角色级指标、维度和区域白名单；区域限制只允许 `eq` / `in` 且值在允许集合内；配置了区域白名单的角色**必须**在每次查询中显式过滤区域，不能省略过滤读取全国汇总，也不能仅按区域分组；
- 结果存储按 principal 隔离，知道 `request_id` / `run_id` 不能读取他人结果；
- Agent 工具只能通过 Tool Registry 调用；未注册工具、原始 SQL 参数、无 scope、未授权指标/维度/区域均在执行前拒绝；
- 工具 gateway 每次调用打开短生命周期只读会话，不向模型或工具暴露 Session/Connection；
- Agent 的 6 个数据工具不把 `QueryCompiler` 生成的 SQL 文本交给模型或 SQL Guard，而是执行受信任的结构化契约：`CompiledQuery` → `SemanticQueryGuard` → `TrustedSelect` → `SafeQueryExecutor.execute_select`。Guard 会重新解析指标定义、校验维度/过滤/表白名单/选中列，并拒绝 `TextClause`、非 `Select` 语句、非标量绑定参数和篡改的指标定义。SQL Guard 继续负责 Phase 3 的 LLM 候选 SQL 路径；
- 工具最大次数、总时限、单工具超时、有限重试、连续失败终止由代码强制；
- 工具超时是协作式的 `ThreadPoolExecutor` 超时，**不是进程隔离**：调用方会在超时后返回结构化 `timeout`，但底层查询线程可能继续运行到数据库层超时或自然结束。SQLite 使用 `busy_timeout` + progress handler，PostgreSQL 使用 `statement_timeout` / `lock_timeout`；数据库层超时与线程超时是两道独立限制；
- LLM 不可用、超时、非法工具输出、预算耗尽、重复调用、工具失败均返回结构化状态，不伪造成功；
- 持久化审计：`agent_audit_events` 记录 run 创建/结束、模型响应与错误、工具调用、权限拒绝、失败和取消，可关联 `principal_id` / `run_id` / `tool_call_id`；`GET /api/v1/agent/runs/{run_id}/events` 和 SSE 端点读取同一份记录；
- 未配置真实 LLM 时，Agent 端点返回 `llm_unavailable`，测试和演示使用明确标注的假客户端。
- Phase 5 证据与报告沿用同一授权边界：证据/发现/报告端点先校验 run 归属，再要求 `metrics:read`；他人 run 统一返回 404，`viewer` 可读报告但不能触发分析；证据只保存经过授权工具返回的结果，不额外扩大数据范围。
- 报告中的业务数字以 `ValueReference` 指向本次 run 的证据原值；Validator 拒绝本次 run 之外的 `evidence_id`、被篡改的数字、单位/时间范围/筛选口径不一致，以及把假设写成事实或把统计相关写成因果。
- 高风险建议只生成文本并标记 `requires_approval`，不写回业务库、不调用采购/定价/预算/编制系统；Phase 5 没有实现审批状态机，审批仍属 Phase 6。

尚未实现：数据库 RLS、列级权限、人工审批流。角色到业务域/指标/维度/区域/数据资源的授权是**基于配置白名单的基础版本**（`EBA_AUTHZ_POLICY` + 代码强制），不是完整的企业 RBAC，也不是生产级多租户隔离。

---

## 2. SQL Guard

### 2.1 执行管线

```text
候选 SQL
  ↓
1. 单语句检查
  ↓
2. 解析 AST（sqlglot, dialect=postgres）
  ↓
3. 语句类型检查（只允许 SELECT / UNION of SELECT）
  ↓
4. 系统表/系统 schema 检查
  ↓
5. 表白名单检查
  ↓
6. 列白名单检查
  ↓
7. 危险函数/语法检查
  ↓
8. 行级权限注入
  ↓
9. LIMIT 注入 / 行数上限
  ↓
10. 只读事务 + statement_timeout
  ↓
11. 执行 + 审计
```

### 2.2 允许与禁止

**允许：**

- `SELECT`
- `WITH ... SELECT`（CTE 只读）
- `UNION` / `UNION ALL`（各分支都必须是只读 SELECT）
- 聚合、窗口函数、`CASE`、`COALESCE`
- 参数绑定（bind params）

**禁止：**

| 类别 | 示例 |
| --- | --- |
| 写操作 | `INSERT`, `UPDATE`, `DELETE`, `MERGE` |
| DDL | `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, `COMMENT` |
| 权限 | `GRANT`, `REVOKE` |
| 事务/会话 | `BEGIN`, `COMMIT`, `ROLLBACK`, `SET`, `RESET` |
| 导出/文件 | `COPY`, `\copy`, `lo_import`, `lo_export` |
| 锁 | `FOR UPDATE`, `FOR SHARE`, `LOCK` |
| 系统 | `VACUUM`, `ANALYZE`, `REINDEX`, `CLUSTER`, `CHECKPOINT` |
| 调用 | `CALL`, `DO`, `EXECUTE` |
| 系统表 | `pg_catalog.*`, `information_schema.*`, `pg_*`, `sqlite_*` |
| 危险函数 | `pg_sleep`, `pg_read_file`, `pg_ls_dir`, `dblink`, `query_to_xml`, `current_setting`（按需） |
| 数据修改 CTE | `WITH x AS (DELETE ...) SELECT ...` |

### 2.3 白名单

白名单来源：

- `semantic_layer/metrics.json` 的 `source.table`；
- `semantic_layer/relationships.json` 的关系表；
- 显式批准的只读视图（如 `v_sales_enriched`）；
- 系统运行表不在业务查询白名单内（`agent_run` 等只由应用内部访问）。

列白名单：

- 从 SQLAlchemy 模型 / `dimensions.json` 生成；
- 禁止 `SELECT *`，或将其展开为白名单列；
- 禁止访问 `created_at` 等无业务意义列（可配置）；
- 禁止访问敏感列（如未来新增的 `employee_salary`）。

### 2.4 行级权限注入

两种实现，优先第一种：

1. **视图 + RLS：** 为每个角色/业务域建立只读视图，视图内已过滤；数据库启用 Row-Level Security。
2. **查询包装：** 把候选 SQL 包成子查询，在外层加 `WHERE region_id IN (...)` / `department_id IN (...)`；必须确认列存在且语义正确。

注意：

- 不能只在前端/工具层过滤；数据库层也要有防线；
- 注入过滤器后要重新解析，确保没有被注释/别名绕过；
- 对聚合结果也要过滤，避免“先聚合全公司再过滤”泄露总量。

### 2.5 参数化

- 指标查询由 Query Compiler 生成，使用 bind params；
- LLM 生成的 SQL 中，用户输入值必须是参数，不允许字符串拼接；
- 若无法参数化，至少做类型/长度/枚举校验；
- 禁止动态表名/列名拼接，必须来自白名单。

### 2.6 资源限制

| 限制 | 建议值 | 说明 |
| --- | --- | --- |
| `statement_timeout` | 15s | 防止慢查询 |
| `lock_timeout` | 3s | 防止锁等待 |
| `idle_in_transaction_session_timeout` | 30s | 防止悬挂事务 |
| 最大返回行数 | 1000（默认 200） | 防止大结果 |
| 最大扫描行数 | 按表配置 | 可选 |
| 查询并发 | 每用户 2–5 | 防止滥用 |
| 单 run 查询数 | 与 `max_tool_calls` 一致 | 预算 |
| 查询成本 | `EXPLAIN` 估算，超阈值拒绝 | Phase 6 可选 |

### 2.7 只读数据库账号

```sql
-- 业务只读角色
CREATE ROLE eba_readonly LOGIN PASSWORD '***';
GRANT CONNECT ON DATABASE enterprise_business_agent TO eba_readonly;
GRANT USAGE ON SCHEMA public TO eba_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO eba_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO eba_readonly;
ALTER ROLE eba_readonly SET default_transaction_read_only = on;
ALTER ROLE eba_readonly SET statement_timeout = '15s';
ALTER ROLE eba_readonly SET lock_timeout = '3s';

-- 明确撤销写权限
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM eba_readonly;
```

应用连接使用 `eba_readonly`；迁移/生成数据使用单独的写账号。**分析 Agent 永远不拿写账号。**

### 2.8 Guard 测试

至少覆盖：

- 正常 SELECT / 聚合 / 窗口函数 / CTE；
- `INSERT` / `UPDATE` / `DELETE` / `DROP` / `ALTER` / `TRUNCATE`；
- 多语句 `SELECT 1; DROP TABLE ...`；
- 注释绕过 `SELECT 1 -- ; DROP ...`；
- 系统表 `pg_catalog.pg_user`；
- 危险函数 `pg_sleep(100)`；
- 数据修改 CTE；
- 未授权表 / 列；
- `SELECT *`；
- 超长/超大 OFFSET；
- 行级权限注入后的语义正确性。

**验收：对抗样例 100% 拦截，正常查询 0 误杀。**

### 2.9 Phase 3 实际实现与差异

实际代码：

- Guard：[security/sql_guard.py](../src/enterprise_business_agent/security/sql_guard.py)
- 执行器：[security/executor.py](../src/enterprise_business_agent/security/executor.py)
- 统一入口：`SecureQueryService`（[security/executor.py](../src/enterprise_business_agent/security/executor.py)）
- 只读引擎：[db.py](../src/enterprise_business_agent/db.py)
- 只读角色脚本：[create_readonly_role.py](../scripts/create_readonly_role.py)

已落实：

1. **AST 解析**：使用 SQLGlot；只允许单条只读 `SELECT` 或只读 `WITH ... SELECT`；拒绝 DDL、DML、多语句、`SELECT *`、`SELECT INTO`、`FOR UPDATE`。
2. **白名单**：表、列、函数、操作符全部显式配置；只接受注册表中的表/列和少量聚合函数。
3. **参数化**：外部值必须是命名绑定参数；SQL 中出现字面量过滤值、位置参数、缺失或多余参数都会被拒绝。
4. **绕过防护**：注释（`--`、`/* */`）、系统 schema/表、未知列、未授权函数直接拒绝；无法解析或方言不兼容返回结构化错误。
5. **资源限制**：`LIMIT` 超限收紧到硬上限；`OFFSET` 超限拒绝；执行器限制返回行数并设置查询超时。
6. **数据库层只读**：SQLite 使用 `mode=ro` URI，并在连接上执行 `PRAGMA query_only`；PostgreSQL 使用只读会话，设置 `default_transaction_read_only=on`、`statement_timeout`、`lock_timeout`。
7. **独立账号**：脚本只授予 `CONNECT / USAGE / SELECT`，撤销写表与 DDL 权限，并设置默认只读与超时；密码只从 `EBA_READONLY_PASSWORD` 读取，不打印、不落盘。
8. **审计**：执行器回调只记录 SQL 哈希、表名、行数、耗时、状态和只读标记，不记录 SQL 原文或参数值。
9. **LLM 边界**：LLM 只看到问题、方言、表/列目录；不持有连接、账号或密码；候选 SQL 必须走同一条 Guard + 执行链路。

尚未实现（如实标记）：

- 行级权限注入与数据库 RLS（Phase 6）；
- 按用户/角色的表、列、指标授权（RBAC，Phase 6）；
- 持久化、不可篡改的外部审计存储；
- 查询成本估算与并发限流；
- 正常查询“0 误杀”只在现有测试语料上验证，不代表对所有业务 SQL 的普遍保证；
- 独立只读角色的真实集成验证在本地 PostgreSQL 17 完成，仓库 PG16 compose 配置未单独实测。

---

## 3. RBAC 权限模型

### 3.1 角色

| 角色 | 业务域 | 行级范围 | 敏感字段 | 工具 |
| --- | --- | --- | --- | --- |
| `ceo` | 全部 | 全部 | 全部 | 全部 |
| `sales_manager` | Sales + 相关 Marketing | 本区域 | 不含 HR 成本 | 销售/营销工具 |
| `marketing_manager` | Marketing + Sales 汇总 | 全部区域（聚合） | 不含 HR 成本 | 营销工具 |
| `finance_manager` | Finance + 全部汇总 | 全部 | 成本/费用 | 财务工具 |
| `supply_manager` | Supply Chain + Sales 汇总 | 全部 | 不含 HR 成本 | 供应链工具 |
| `hr_manager` | HR | 本部门/全部 HR | 人力成本 | HR 工具 |
| `analyst` | 只读聚合 | 按授权 | 脱敏 | 查询工具，不含 `run_sql` |
| `admin` | 系统配置 | — | — | 运维工具，不参与业务分析 |

### 3.2 权限矩阵

| 资源 | CEO | Sales | Marketing | Finance | Supply | HR | Analyst |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Sales 指标 | ✅ | ✅ | ✅ 汇总 | ✅ | ✅ 汇总 | ❌ | ✅ |
| Marketing 指标 | ✅ | ✅ 汇总 | ✅ | ✅ | ❌ | ❌ | ✅ |
| Finance 指标 | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ | ✅ 汇总 |
| Supply 指标 | ✅ | ✅ 汇总 | ❌ | ✅ 汇总 | ✅ | ❌ | ✅ |
| HR 指标 | ✅ | ❌ | ❌ | ✅ 汇总 | ❌ | ✅ | ✅ 脱敏 |
| `labor_cost` 明细 | ✅ | ❌ | ❌ | ✅ 汇总 | ❌ | ✅ | ❌ |
| `headcount` 明细 | ✅ | ❌ | ❌ | ✅ 汇总 | ❌ | ✅ | ✅ 脱敏 |
| `run_sql` | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 导出原始行 | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

### 3.3 策略模型

```python
class AccessPolicy(BaseModel):
    role: str
    allowed_domains: list[str]
    allowed_metrics: list[str] | Literal["*"] = "*"
    denied_metrics: list[str] = []
    row_filters: dict[str, list[str]] = {}   # {"region_id": ["EC"]}
    allowed_tools: list[str] | Literal["*"] = "*"
    can_run_sql: bool = False
    can_see_sensitive: bool = False
    max_rows: int = 200
```

### 3.4 执行点

```text
API 鉴权
  → 加载角色策略
  → Tool Registry 检查工具权限
  → Metric Resolver 检查指标权限
  → Query Compiler 注入行过滤
  → SQL Guard 再检查表/列权限
  → PostgreSQL RLS（最后一道）
```

**四层都检查，任何一层拒绝都终止。** 不能因为“上层已经检查过”就跳过下层。

### 3.5 越权行为

- 拒绝时返回 `PERMISSION_DENIED`，说明缺少哪个权限；
- 不返回“换个说法就能查到”的提示；
- 记录审计；
- Agent 不能通过 `run_sql` 绕过指标权限；
- 不能通过多次聚合反推敏感明细（限制行数、查询次数、聚合粒度）。

---

## 4. 审计日志

### 4.1 记录内容

| 字段 | 说明 |
| --- | --- |
| `event_id` | 唯一 ID |
| `timestamp` | UTC |
| `user_id` / `role` | 操作者 |
| `run_id` / `conversation_id` | 关联分析 |
| `action` | `run.create`, `tool.call`, `sql.execute`, `guard.reject`, `permission.deny`, `report.generate`, `approval.*` |
| `resource` | 指标 / 表 / 工具 / 报告 |
| `allowed` | 是否允许 |
| `reason` | 拒绝/允许原因 |
| `query_hash` | SQL 哈希 |
| `sql_redacted` | 脱敏 SQL（可选） |
| `row_count` | 返回行数 |
| `latency_ms` | 耗时 |
| `error_code` | 错误码 |

### 4.2 要求

- **只追加，不修改**；定期归档；
- 敏感值（密码、token、PII）不写入；
- SQL 可记录哈希 + 脱敏文本；完整 SQL 存受控表；
- 审计日志与业务数据分离；
- 支持按 user / run / 时间 / 资源检索；
- 保留期按合规要求（演示环境至少 90 天）。

### 4.3 审计事件示例

```json
{
  "event_id": "audit_01J...",
  "timestamp": "2026-10-28T10:00:03Z",
  "user_id": "u_1001",
  "role": "sales_manager",
  "run_id": "run_01J...",
  "action": "permission.deny",
  "resource": "HR.labor_cost",
  "allowed": false,
  "reason": "role sales_manager has no access to HR domain",
  "query_hash": null,
  "latency_ms": 1
}
```

---

## 5. 人工审批

> **当前状态：** Phase 5 只把高风险建议标记为 `requires_approval`，并在报告中说明依据、预期方向和风险；尚未实现审批 API、状态机或审批审计，因此不能声称“已接入人工审批”。

### 5.1 需要审批的建议

- 调价 / 促销 / 折扣策略；
- 停止或大幅调整营销投放；
- 大额采购 / 供应商变更；
- 人员编制调整 / 裁员；
- 任何涉及资金、合同、合规的 action。

### 5.2 流程

```text
Agent 生成建议
  ↓
标记 requires_approval
  ↓
状态：draft
  ↓
人工审批（API / UI）
  ↓
approved / rejected
  ↓
记录审计
  ↓
不自动执行；由业务系统执行
```

### 5.3 审批记录

- 建议内容、证据链、风险、预期影响；
- 审批人、时间、意见；
- 审批状态不可篡改；
- 被拒绝的建议保留，供后续复盘。

---

## 6. Prompt Injection 与数据投毒

### 6.1 攻击面

- 用户问题中嵌入“忽略之前的指令”；
- 工具返回的数据中包含指令；
- RAG 文档中包含恶意指令；
- 数据库字段值中包含指令；
- 历史对话中被污染。

### 6.2 防护

| 措施 | 说明 |
| --- | --- |
| 指令层级 | 系统提示最高优先级；数据内容明确标注“不是指令” |
| 分隔与转义 | 工具输出/RAG 文档用固定分隔符包裹，转义控制字符 |
| 工具白名单 | 只允许注册工具；文本不能创建新工具 |
| 参数 schema | 工具参数必须符合 Pydantic，不能执行自由文本命令 |
| 输出校验 | LLM 输出必须是 JSON，动作必须在允许集合 |
| RAG 审核 | 文档入库前审核来源；不抓取不可信网页 |
| 最小上下文 | 只传必要数据，减少注入面 |
| 审计 | 记录可疑输入，便于溯源 |
| 权限隔离 | 即使注入成功，也受 RBAC/Guard 限制 |

### 6.3 Phase 4.1 注入与越权测试

`tests/test_agent_security_boundaries.py` 使用脚本化模型故意执行攻击动作，验证代码边界而不是模型自觉：

| 攻击 | 预期结果 |
| --- | --- |
| 用户要求忽略权限，查询未授权区域 | 执行前 `permission_denied`，查询执行器调用次数为 0 |
| 区域受限角色不传区域过滤 | `permission_denied`，不能读取全国汇总 |
| 区域受限角色只按区域分组 | `permission_denied`，不能看到其他区域 |
| 诱导模型调用不存在的 `run_sql` | `invalid_action` / `unknown_tool`，数据库表仍存在 |
| 工具返回文本包含“忽略规则并删表” | 只能作为 tool result 数据进入上下文；后续未注册工具被拒绝 |
| 最终答案包含未查询的数字 | `completed_with_warnings` 并列出无法核对数字 |
| 工具线程超时 | 返回结构化 `timeout`；测试证明底层线程可能继续运行，因此不声称强制终止 |

这些测试证明的是**控制面拒绝能力**，不是真实模型对注入的抵抗力。26 条真实用例中只有 2 条负向控制（不支持的问题、歧义/预测类），**没有专门的真实模型注入抵抗评测**，因此不声称任何真实模型注入抵抗率。

### 6.4 检测

- 关键词/模式检测（“ignore previous instructions”“system:”等）；
- 异常工具调用（频率、参数、目标表）；
- 异常数据访问（大量敏感表）；
- 记录并告警，不自动阻断正常业务（避免误杀）。

---

## 7. 密钥与配置

- 密钥只放在环境变量 / Secret Manager；
- `.env.example` 只放占位符；
- 不把密钥写入日志、报告、prompt；
- 数据库账号分离：迁移写账号 / 分析只读账号 / 运行表账号；
- 定期轮换；
- 本地开发使用 Docker Compose 默认密码，生产必须替换；
- 代码仓库禁止提交 `.env`、密钥、数据库文件。

---

## 8. 数据治理

- **最小化：** 只收集分析所需字段；不引入员工身份证、住址等 PII；
- **脱敏：** 报告默认聚合；敏感明细需授权；
- **保留：** 运行记录/审计按策略保留；
- **删除：** 支持按用户/会话删除分析记录（审计日志按合规保留）；
- **模拟数据声明：** 明确标注为模拟数据，不用于真实经营决策；
- **数据来源：** 生产接入时记录来源系统、同步时间、口径版本。

---

## 9. 安全验收清单

Phase 3 / Phase 6 完成时：

- [x] SQL Guard 对抗样例在测试集中按预期拦截；正常查询在测试语料上未被误杀（Phase 3）；
- [x] 只读账号无法执行 INSERT/UPDATE/DELETE/DDL（SQLite 与本地 PostgreSQL 17 已验证）；
- [x] 多语句、注释绕过、系统表、危险函数、数据修改 CTE 全部拦截（Phase 3 测试集）；
- [x] 未授权表/列被拒绝；指标层未知指标返回 `unsupported`（Phase 3）；
- [ ] 行级权限在应用层和数据库层都生效（Phase 6，尚未实现 RLS）；
- [ ] 每次 run / tool / SQL / guard / permission 都有审计记录（Phase 3 仅记录 SQL 哈希与元数据，无持久化审计）；
- [ ] 审计日志不可篡改、可检索（Phase 6）；
- [x] 未注册工具、原始 SQL 参数、未授权指标/维度/区域和执行前 scope 检查（Phase 4 测试）；
- [x] Agent 数据工具经过结构化查询契约执行；篡改指标、非 SELECT、越权表/列、原始 SQL 文本和非标量绑定参数均被拒绝（Phase 4.1 测试）；
- [x] 区域受限角色不能省略区域过滤读取全国汇总，也不能只按区域分组绕过（Phase 4.1 测试）；
- [x] 工具结果中的 Prompt Injection 不能创建工具、提升权限或诱导未注册动作；注入文本以 tool result 数据传入，未注册工具在执行前被拒绝（Phase 4.1 测试）；
- [x] 结论中无法在证据里核对的数字会被标记为 `completed_with_warnings`（Phase 4 测试）；
- [x] 报告数字必须解析到本次 run 的证据原值；不存在的证据/发现、单位、时间范围和筛选口径不一致会被拒绝（Phase 5 测试）；
- [x] 假设被写成事实、统计相关被写成因果会被拒绝；有限次修正失败后回退确定性报告（Phase 5 测试）；
- [x] 证据/发现/报告端点校验 run 归属与 `metrics:read`，他人 run 返回 404，`viewer` 不能触发分析（Phase 5 测试）；
- [x] 高风险建议只标记 `requires_approval`，不自动执行；没有写业务库的动作（Phase 5 测试）；
- [ ] 高风险建议进入审批流，不自动执行（Phase 6；审批状态机尚未实现）；
- [x] 仓库无密钥、无 `.env`、无数据库文件（`.gitignore` 已覆盖；Phase 4.1 对项目文件做了密钥值扫描，未发现泄漏）；
- [x] 安全测试纳入测试套件（Phase 3 / Phase 4.1 / Phase 5；CI 配置未单独提供）。

---

## 10. 常见错误

| 错误 | 后果 | 正确做法 |
| --- | --- | --- |
| 只靠 Prompt 禁止危险 SQL | 易绕过 | AST + 只读账号 + 白名单 |
| 应用层过滤，数据库层不设防 | 绕过应用即泄露 | 数据库 RLS / 视图 |
| `run_sql` 对所有角色开放 | 越权 | 仅授权角色，默认关闭 |
| 审计只记成功 | 无法追责 | 拒绝也记录 |
| 把完整 SQL 和 PII 写日志 | 二次泄露 | 哈希 + 脱敏 |
| 审批流只是 UI 状态 | 可被绕过 | 后端强制状态机 + 审计 |
| 权限检查只在一处 | 单点失效 | 四层检查 |
| 允许导出原始行 | 数据外泄 | 默认聚合，导出需审批 |
