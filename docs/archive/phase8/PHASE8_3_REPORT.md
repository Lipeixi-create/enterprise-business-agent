# Phase 8.3 业务工作台与部署完成报告

日期：2026-11-03
状态：核心功能已完成；Docker 镜像构建受当前网络条件阻塞，未虚报为已实跑
基线：Phase 8.2 `705 passed, 25 skipped`

## 1. 阶段目标与边界

Phase 8.3 要求把现有 API 能力变成业务人员可用的工作台，并补齐 Docker、环境配置、数据库初始化/迁移、健康检查、日志和启动文档。实现继续复用 FastAPI、LangGraph 单 Agent、AnalysisGateway、权限、Evidence 和审批架构，没有新增微服务、消息队列或多 Agent。

本阶段没有修改 SQL 安全、工具注册、Agent 决策循环、Evidence、Finding、报告校验或人工审批规则。

## 2. 实现前现状

已有能力：

- FastAPI 同步/异步 Agent Run、SSE、取消、幂等；
- 按 principal 的 Run 归属检查、`approval:read` / `approval:read_any`、审批版本和报告指纹校验；
- Run、审计事件、Evidence、Finding、Report、Approval 的持久化查询 API；
- PostgreSQL 初始化脚本、运行库脚本、Alembic、`preflight.py`、Docker Compose 的 PostgreSQL 服务；
- Phase 8.1 trace 和 Phase 8.2 安全审计/MCP 服务端。

缺口：

- 没有业务工作台页面，Report/Evidence/Finding 只能通过 curl 查看；
- 没有“当前账号运行历史”查询，业务人员无法从界面找回自己的任务；
- 没有 `/api/v1/me`，前端无法知道当前角色和可用的审批能力；
- Docker 只有 PostgreSQL，没有应用镜像、初始化编排和健康检查；
- 缺少工作台与 Docker 启动文档。

## 3. 新增实现

### 3.1 身份与运行历史 API

`GET /api/v1/me` 返回 `principal_id`、`role`、`authenticated` 和排序后的 `scopes`，不返回 API Key。

`GET /api/v1/agent/runs`：

- 需要 `run:read`；
- 有 `resource:read_any` 的角色查看全部，否则只返回当前 principal 的运行；
- 支持 `status`、`limit`、`offset`；
- 返回 `items`、`count`、`limit`、`offset`、`has_more`、`scope`；
- 历史摘要包含 `question`、`status`、`stop_reason`、`error_count`、`updated_at`，不复制完整 Run 文档。

`PersistentRunStore.list_runs()` 使用参数化 SQL 和列白名单，状态过滤不接受原始 SQL。

### 3.2 业务工作台

静态单页应用挂载在 `/workbench/`，根路径 `/` 307 跳转。页面由 FastAPI 同源提供，API 与静态资源没有跨域拼接，也没有独立前端构建链。

功能：

- API Key 只保存在浏览器 `sessionStorage`，连接后调用 `/api/v1/me` 显示角色和权限；
- 发起同步或异步分析，异步模式使用 `fetch` + `ReadableStream` 读取 SSE（`EventSource` 无法发送 `X-API-Key`）；
- 显示 Run 状态、停止原因、错误、执行步骤、工具调用、耗时、校验状态和最终回答；
- 报告、Evidence、Finding、运行详情分栏查看，Evidence 显示来源表、时间范围、可追溯标记和原始 JSON；
- 历史运行按账号隔离，点击历史即可加载完整结果；
- 审批面板显示状态、审批人、报告版本和报告指纹是否过期；提交审批时携带 `report_version` 和 `current_report_fingerprint`；
- 支持取消运行、刷新历史和结构化错误展示。

前端没有使用 `innerHTML` 渲染业务数据，全部通过 `textContent` 或 JSON `<pre>` 写入，避免把模型/工具输出当 HTML 执行。

### 3.3 Docker 与部署

新增：

- `Dockerfile`：Python 3.12 slim、非 root `eba` 用户、应用健康检查、uvicorn 启动；
- `.dockerignore`：排除 `.env`、数据库、测试、文档和本地缓存；
- `docker-compose.yml`：`postgres`、`init`、`app` 三段式编排；
- `.env.docker.example`：只含占位值；
- `docs/PHASE8_DEPLOYMENT.md`：本地 SQLite、Docker、非 Docker PostgreSQL 启动步骤和上线前检查。

初始化顺序：

```text
alembic upgrade head
→ init_db.py
→ create_readonly_role.py
→ init_runtime_store.py
→ migrate_phase5.py
→ 可选 generate_data.py --reset（仅 EBA_SEED_DATA=true）
```

API 只使用 `eba_readonly` 和 `eba_runtime`；初始化/管理连接只存在于 `init` 服务。`app` 依赖 `postgres` 健康和 `init` 成功，带 healthcheck 和 Docker 日志轮转。

### 3.4 打包

`pyproject.toml` 增加：

```toml
[tool.setuptools.package-data]
enterprise_business_agent = ["web/*"]
```

确保安装后的包包含工作台静态文件。

## 4. 测试与验证

### 4.1 自动化测试

新增 `tests/test_phase8_workbench.py`，4 个测试：

1. `/workbench/` 由 FastAPI 实际提供，包含工作台标题和 `app.js`；`/` 跳转正确；
2. `/api/v1/me` 返回角色和 scope，响应体不含 API Key；
3. 运行历史按 principal 隔离，admin 可查看全部，状态过滤、`has_more`、`error_count` 正确；
4. 未认证访问历史返回 401。

结果：`4 passed in 1.45s`。

全量回归：`709 passed, 25 skipped in 64.97s`。

### 4.2 本地 HTTP 冒烟

使用 `uvicorn` + 本地 SQLite + 独立测试 Run Store 实际启动服务：

- `/health` 返回 200；
- `/workbench/` 返回 200，页面包含“经营分析工作台”；
- `/api/v1/me` 返回 `analyst` 角色和 7 个 scope；
- `/api/v1/agent/runs` 未认证返回 401；
- 异步提交返回 `queued`，未配置 LLM 时正确落库为 `llm_unavailable` / `llm_not_configured`；
- 历史接口返回该 Run，`error_count=1`；
- SSE 实际返回 `run_created`、`run_finished`、`status`、`done` 事件。

`node --check src/enterprise_business_agent/web/app.js` 通过。

### 4.3 前端验证边界

当前环境没有浏览器自动化工具，未做真实浏览器视觉/交互验收。已验证静态资源可访问、JS 语法、后端 API 契约和 SSE 数据流；不能据此声称“浏览器端完整验收通过”。

## 5. 阻塞与未完成项

### 5.1 Docker 镜像构建阻塞

`docker compose config` 使用占位 `EBA_API_KEYS` 验证通过。`docker build -t enterprise-business-agent:phase8 .` 失败：

```text
python:3.12-slim 拉取超时（Docker Hub 网络不可达）
```

本机仅有 `postgres:16`、`pgvector/pgvector:pg17`、`neo4j:5.26`、`hello-world:latest`，没有可替代的 Python 基础镜像。因此：

- Dockerfile、compose 编排和配置已完成静态验证；
- **镜像未构建，`docker compose up` 未实跑**；
- 不在本报告中声称 Docker 部署验收通过。

解除条件：可访问 Docker Hub 或企业镜像仓库后执行 `docker compose --env-file .env.docker up --build`，并检查 `init` 退出码、`app` 健康状态和工作台。

### 5.2 未执行真实模型

本阶段没有调用真实 LLM，没有重新执行 Phase 7 评测。工作台异步链路使用真实后端和真实数据库，但在无模型配置时只验证到 `llm_unavailable`，没有验证真实模型驱动的完整报告/审批 UI 流程。Phase 7.9 历史失败记录保持不变。

## 6. 文件变更

新增：

- `src/enterprise_business_agent/web/index.html`
- `src/enterprise_business_agent/web/styles.css`
- `src/enterprise_business_agent/web/app.js`
- `tests/test_phase8_workbench.py`
- `Dockerfile`
- `.dockerignore`
- `.env.docker.example`
- `docs/PHASE8_DEPLOYMENT.md`
- `docs/phase8/PHASE8_3_REPORT.md`

修改：

- `src/enterprise_business_agent/api/app.py`：挂载工作台、根路径跳转；
- `src/enterprise_business_agent/api/routes.py`：`/api/v1/me`、`GET /api/v1/agent/runs`；
- `src/enterprise_business_agent/api/run_store.py`：`list_runs`；
- `pyproject.toml`：静态文件 package data；
- `docker-compose.yml`：应用与初始化编排；
- `.gitignore`：忽略本地 trace 文件并保留 Docker 环境示例；
- `README.md`、`docs/README.md`：状态、启动方式、测试数和工作台入口。

## 7. 部署前置条件

- Docker 可访问基础镜像仓库，或已预加载 `python:3.12-slim`；
- 准备 `POSTGRES_PASSWORD`、`EBA_RUNTIME_PASSWORD`、`EBA_READONLY_PASSWORD`；
- 准备长随机 `EBA_API_KEYS`，并通过环境/密钥管理系统注入；
- 需要真实分析时另行配置 LLM Key 和预算；
- 生产环境需要 TLS、反向代理、备份、日志采集和密钥轮换。

不得把真实密钥写入仓库、报告、命令历史或日志。

## 8. 结论

Phase 8.3 的工作台、身份、历史、报告/Evidence/Finding 展示、审批操作、Docker/初始化编排和启动文档已实现并通过自动化与本地 HTTP 验证。Docker 镜像构建因当前环境无法拉取基础镜像而未完成实跑，前端未做浏览器级验收，这两项必须在 Phase 8.4 或具备条件后补测，不能标记为已验证。
