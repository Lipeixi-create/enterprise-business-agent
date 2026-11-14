# Phase 8.2 完成报告：生产安全加固与 MCP 外部工具集成

项目：`enterprise_business_agent`
阶段：Phase 8.2
日期：2026-11-03
状态：**核心交付完成；离线安全测试与回归通过，未重新执行真实模型评测**

> 本报告只记录实际执行过的内容。真实模型调用、离线测试和未执行项分开标注。
> 报告中不包含任何 API Key、数据库密码或完整连接串。

---

## 1. 阶段目标与结论

Phase 8.2 的目标是在现有 FastAPI、LangGraph 单 Agent、`AnalysisGateway`、SQL Guard、
Evidence 和人工审批架构上，补齐生产安全边界，并新增 MCP 标准协议工具传输层：

```text
MCP client
  → POST /mcp（JSON-RPC 2.0）
  → API Key → Principal
  → ToolRegistry（scope / 指标 / 维度 / 区域策略）
  → AnalysisGateway
  → SemanticQueryGuard → SafeQueryExecutor
  → 只读数据库
```

本阶段完成后：

- 自然语言分析与结构化指标端点显式要求 `metrics:read`；`approver` 等非数据角色不能再读取业务指标。
- 有指标/区域/域限制的角色启用 `allow_sql_fallback` 时会被前置拒绝，SQL fallback 不再成为数据权限旁路。
- API Key 使用 `hmac.compare_digest` 常量时间比较，空 Key 不再可能匹配空配置。
- 401/403 与 MCP 操作写入 run-independent 安全审计事件，新增 admin-only 审计查询端点。
- 新增 `POST /mcp` JSON-RPC 2.0 传输层，支持 `initialize`、`ping`、`tools/list`、`tools/call` 和 notification；
  MCP 只暴露现有 8 个只读工具，调用仍经过 `ToolRegistry`、权限检查、`AnalysisGateway` 和 SQL 安全链路。
- 新增 13 个定向测试；全量回归 `705 passed, 25 skipped`。

**没有完成或没有验证的部分：**

- 本阶段没有重新执行真实 GLM 业务评测；Phase 7.9 的真实运行失败记录保持不变。
- 当前只实现 MCP **服务端 HTTP 传输**，没有实现 MCP 客户端、SSE/Streamable HTTP 会话、OAuth/mTLS。
- 没有引入数据库 RLS、列级权限、外部身份提供商或独立审计系统。
- MCP 没有单独的限流器；请求仍受现有 API 超时与工具超时约束。
- 安全审计写入是 best-effort：写入失败会记录 warning，但不会阻断业务请求。

---

## 2. 执行前基线与审查发现

### 2.1 已有能力

| 能力 | 位置 | 状态 |
| --- | --- | --- |
| API Key → Principal | `security/auth.py`、`api/auth.py` | 已有 |
| 角色 scope 与指标/域/区域/资源策略 | `security/auth.py` | 已有 |
| 工具级权限与参数校验 | `agent/tools.py` | 已有 |
| 只读 SQL Guard 与执行器 | `security/sql_guard.py`、`security/executor.py` | 已有 |
| Run 归属隔离、审批隔离 | `api/routes.py`、`evidence/approvals.py` | 已有 Phase 6 测试 |
| Run 级审计事件 | `api/run_store.py` | 已有 |
| Evidence / Finding / Report / 审批引用链 | `evidence/`、`report/` | 已有 |

### 2.2 审查发现的实际风险

1. **指标端点只认证、不检查 `metrics:read`**
   - `approver`、部分管理角色可以调用 `/api/v1/metrics/query` 和自然语言分析端点读取业务指标。
   - 修复：两个端点在进入分析前调用 `_require_metrics_scope(principal)`，缺少 scope 返回 403。

2. **SQL fallback 可以绕过指标/域/区域权限**
   - `allow_sql_fallback=true` 时，受限角色可能在语义解析失败后进入 SQL fallback；SQL Guard 只证明
     SQL 只读、表列安全，不能证明用户有权查看该指标或区域。
   - 修复：`AuthorizationPolicy.allows_sql_fallback()` 对任何配置了 `metrics`、`domains`、
     `resources`、`dimensions`、`regions` 或 `tools` 限制的角色返回 false；`_run_sql_fallback`
     在调用 runner 前直接返回 `FORBIDDEN`，并在 API 错误 detail 中保留 `sql_fallback_forbidden`。

3. **空 API Key 与空配置匹配**
   - 旧实现按字典查找，理论上可能出现空 presented key 命中空配置项；也没有常量时间比较。
   - 修复：拒绝空 Key，遍历所有配置项用 `hmac.compare_digest` 比较，不提前返回。

4. **认证/授权失败没有安全审计**
   - 之前只有 run 级审计，401/403 和 MCP 操作没有独立记录。
   - 修复：新增 `security/audit.py` 与 `get_run_store.list_security_events()`；
     middleware 记录 `auth_failed` / `authorization_denied`，MCP 记录
     `mcp_auth_failed`、`mcp_tool_call`、`mcp_tool_denied`、`mcp_protocol_error`。

5. **没有 MCP 标准协议入口**
   - 修复：新增 `src/enterprise_business_agent/mcp/server.py` 与 `POST /mcp`，作为现有工具的传输层，
     不是第二套执行路径。

---

## 3. 实现内容

### 3.1 后端权限与认证

- `src/enterprise_business_agent/api/routes.py`
  - `_require_metrics_scope(principal)`：NL/结构化指标端点要求 `metrics:read`。
  - `GET /api/v1/security/audit`：仅 `admin`，支持 `event_type` 过滤和 `limit`（1–1000）。
  - NL/结构化查询的 403 响应在 `error.detail.reason` 中保留 `sql_fallback_forbidden` 等具体原因，
    同时保持历史 `error.code="forbidden"` 兼容。
- `src/enterprise_business_agent/security/auth.py`
  - `hmac.compare_digest` 常量时间 API Key 比较；空 Key 直接拒绝。
  - `AuthorizationPolicy.allows_sql_fallback(principal)`：存在任何数据范围限制时禁止 SQL fallback。
- `src/enterprise_business_agent/nl/service.py`
  - 在 SQL fallback runner 前执行数据权限检查，返回 `AnalysisStatus.FORBIDDEN`。
- `src/enterprise_business_agent/api/auth.py`
  - 将 Principal 写入 `request.state.principal`，供 middleware 审计使用。
- `src/enterprise_business_agent/api/middleware.py`
  - 401/403 记录安全事件和结构化日志；不记录请求体、API Key 或连接串。

### 3.2 安全审计

- `src/enterprise_business_agent/security/audit.py`
  - 固定事件集合：
    `auth_failed`、`authorization_denied`、`mcp_auth_failed`、
    `mcp_tool_call`、`mcp_tool_denied`、`mcp_protocol_error`。
  - 通过 `run_store.append_event(..., run_id=None, ...)` 存储，与 run 解耦。
  - 写入失败只记录 warning，不把审计失败伪装成业务失败。
- `src/enterprise_business_agent/api/run_store.py`
  - `list_security_events(event_type=None, limit=100)`：只返回 `run_id IS NULL` 且事件类型在白名单内的记录。

### 3.3 MCP 外部工具层

新增：

- `src/enterprise_business_agent/mcp/__init__.py`
- `src/enterprise_business_agent/mcp/server.py`
- `src/enterprise_business_agent/api/app.py`：构造 `app.state.mcp_server`
- `src/enterprise_business_agent/api/routes.py`：`POST /mcp`

协议与行为：

| 项目 | 行为 |
| --- | --- |
| 协议版本 | `2025-06-18`、`2025-03-26`、`2024-11-05` |
| 方法 | `initialize`、`ping`、`tools/list`、`tools/call`、`notifications/*` |
| 批量 | 支持 JSON-RPC batch；全部为 notification 时返回 202 无 body |
| 工具发现 | `registry.specs(principal)`，按角色 scope 与策略过滤 |
| 工具调用 | `registry.execute(name, arguments, principal, call_id="mcp-...")` |
| 返回 | `content[text]` + `structuredContent` + `isError` |
| 注解 | `readOnlyHint=true`、`destructiveHint=false`、`idempotentHint=true`、`openWorldHint=false` |
| 禁用 | `EBA_MCP_ENABLED=false` 时返回 404，不暴露端点 |
| 认证 | `EBA_MCP_REQUIRE_API_KEY=true` 时必须提供有效 `X-API-Key`；匿名请求 401 并审计 |
| 协议错误 | JSON-RPC `-32700` / `-32600` / `-32601` / `-32602` / `-32603`，不再裸抛异常 |

安全边界：

- MCP 不接收 SQL 文本，不接收数据库连接，不直接调用执行器。
- `tools/list` 只返回当前角色允许的工具；`approver` 等无 `metrics:read` 的角色得到空列表。
- `tools/call` 的权限拒绝保留为工具结果 `permission_denied`，`isError=true`，不是 HTTP 成功伪装。
- 审计只记录工具名、状态、call_id、角色和参数名，不记录参数值。
- MCP 路由在线程池执行，避免同步数据库查询阻塞事件循环。

### 3.4 配置

`.env.example` 新增：

```text
EBA_MCP_ENABLED=false
EBA_MCP_REQUIRE_API_KEY=true
EBA_MCP_SERVER_NAME=enterprise-business-agent
```

MCP 默认关闭，升级不会自动暴露新端点。

---

## 4. 定向测试与回归

新增 `tests/test_phase8_security_mcp.py`，共 13 项：

1. `approver` 不能读取结构化指标 / 自然语言业务分析。
2. 受限角色 `allow_sql_fallback=true` 返回 403，`error.detail.reason=sql_fallback_forbidden`。
3. 空 API Key 被拒绝。
4. 安全审计仅 admin 可读，并记录 403 `authorization_denied`。
5. 401 记录 `auth_failed`。
6. MCP 关闭时返回 404。
7. MCP 无 Key 返回 401，并记录 `mcp_auth_failed`。
8. MCP `initialize` 返回协议版本、`tools.listChanged=false` 和 `Mcp-Protocol-Version` 响应头。
9. MCP `tools/list` 返回 8 个工具和只读注解。
10. MCP `tools/call` 经 ToolRegistry 执行成功，并记录 `mcp_tool_call`。
11. 受限角色 MCP 调用被策略拒绝，`permission_denied` + `isError=true`，并记录 `mcp_tool_denied`。
12. MCP 工具列表按角色过滤；`approver` 得到空列表。
13. MCP 协议错误结构化并审计；notification 返回 202 无 body。

实际执行结果：

```text
tests/test_phase8_security_mcp.py ............. 13 passed in 3.67s

pytest -q
705 passed, 25 skipped in 59.95s
```

说明：

- 基线为 Phase 8.1 的 `692 passed, 25 skipped`；本阶段新增 13 项，合计 705。
- 全量回归中的 25 个 skip 均为既有的可选集成/真实服务用例，本阶段没有把未执行项计为通过。
- 本阶段没有调用真实 LLM，也没有改写 Phase 7.9 历史失败记录（`live-004`、`live-005` provider timeout，
  `live-023` 结论越界）。

---

## 5. 残余风险与限制

1. **认证方式仍只有 API Key**
   - 没有 OAuth2/OIDC、mTLS、短期令牌或密钥自动轮换。生产环境应在外层网关补充身份认证与速率限制。

2. **权限不是数据库级安全**
   - 当前是应用层角色 scope + 指标/域/区域策略；没有 PostgreSQL RLS 或列级权限。
   - 任何绕过 API 直接访问只读数据库的路径仍依赖数据库账号权限和网络隔离。

3. **MCP 传输范围有限**
   - 只实现 HTTP JSON-RPC 请求/响应；没有 SSE streaming、会话恢复、服务端主动通知。
   - 没有 MCP 客户端侧集成；现有 Agent 主循环不依赖 MCP。

4. **审计可用性**
   - 审计写入失败为 best-effort，只记录 warning；没有独立的审计存储、不可篡改保证或外部 SIEM 投递。
   - 401/403 审计在 middleware 中同步写 run store；高并发拒绝流量可能增加延迟。

5. **SQL fallback 仍存在**
   - 本阶段只阻断“有数据范围限制的角色”使用 fallback；无限制角色仍可使用既有受控 fallback。
   - SQL Guard 继续负责只读、表列白名单与参数绑定，不等同于数据权限。

6. **未做真实模型与生产环境验证**
   - 没有真实 LLM 安全回归、红队测试或生产 PostgreSQL 压测。
   - `EBA_MCP_REQUIRE_API_KEY=false` 仅建议在受控内网调试使用，生产不应关闭。

---

## 6. 部署前置条件

- 生产环境必须设置 `EBA_API_KEYS`（或单 Key 配置），不要把真实 Key 写入仓库或日志。
- 需要 MCP 时显式设置 `EBA_MCP_ENABLED=true`，并保持 `EBA_MCP_REQUIRE_API_KEY=true`。
- 审计查询需要 admin 角色；不要给业务分析角色 `admin` scope。
- 若使用 PostgreSQL run store，安全事件和 run 事件共用 `agent_audit_events` 表，按 `run_id IS NULL` 区分。
- 本阶段没有修改生产配置，也没有输出任何真实凭据。

---

## 7. 文件变更清单

新增：

- `src/enterprise_business_agent/security/audit.py`
- `src/enterprise_business_agent/mcp/__init__.py`
- `src/enterprise_business_agent/mcp/server.py`
- `tests/test_phase8_security_mcp.py`
- `docs/phase8/PHASE8_2_REPORT.md`

修改：

- `src/enterprise_business_agent/config.py`
- `src/enterprise_business_agent/security/__init__.py`
- `src/enterprise_business_agent/security/auth.py`
- `src/enterprise_business_agent/nl/service.py`
- `src/enterprise_business_agent/api/app.py`
- `src/enterprise_business_agent/api/auth.py`
- `src/enterprise_business_agent/api/middleware.py`
- `src/enterprise_business_agent/api/routes.py`
- `src/enterprise_business_agent/api/run_store.py`
- `.env.example`
- `README.md`

---

## 8. 结论

Phase 8.2 的核心安全交付已完成并经过自动化验证：指标端点 scope 校验、SQL fallback 数据权限阻断、
常量时间 API Key 比较、401/403 与 MCP 安全审计、以及不绕过 `ToolRegistry` / `AnalysisGateway` /
SQL Guard 的 MCP 工具传输层。当前仍不等同于完成生产级身份体系、数据库 RLS 或 MCP 全双工传输；
这些限制已在上文明确列出。
