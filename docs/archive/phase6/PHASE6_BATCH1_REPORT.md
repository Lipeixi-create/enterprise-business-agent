# Phase 6 Batch 1 完成报告：基础质量与安全治理

日期：2026-10-31
范围：合并原 Phase 6.0（PostgreSQL / Numeric Validator）与 Phase 6.1（业务权限体系）
项目：`enterprise_business_agent`

> 凭据说明：本报告不包含数据库密码、API Key 或完整连接串。`.env` 已被 `.gitignore` 排除；本批次未打印、复制或提交任何密钥。GLM API Key 轮换状态仍未确认，本批次没有把轮换作为前置条件，也没有执行真实 LLM 验收调用。

---

## 1. 结论摘要

| 工作流 | 状态 | 结果 |
| --- | --- | --- |
| PostgreSQL 连接与最小权限 | 完成 | `eba_admin`（非超级用户、仅初始化）与 `eba_readonly`（仅 SELECT）均已创建并验证；API 使用只读账号连接真实 PostgreSQL |
| Phase 5 结构在 PostgreSQL 下可用 | 完成 | Run / Evidence / Finding / Report 在 PG 测试库完成持久化、去重、版本化验证 |
| Numeric Validator 误报修复 | 完成 | 区分 `business_result` / `tool_parameter` / `date_component` / `unsupported_number`；无硬编码数字豁免、无整句排除 |
| 后端业务权限体系 | 完成 | 部门域、指标、维度、区域、工具、Run/Evidence/Finding/Report 均在后端校验 |
| 测试与交付 | 完成 | 全量 `434 passed, 11 skipped`；PostgreSQL 专项 `11 passed`；Phase 6 定向测试 `46 passed` |
| 未完成项 | 见第 8 节 | 主要为 GLM 密钥轮换未确认、默认 Run Store 仍为 SQLite、未引入 RLS |

---

## 2. 检查与保护

- 项目目录不是 Git 仓库（`git status` 返回 fatal），因此没有提交、回滚或 diff 操作；所有修改均保留在原文件。
- 实施前已检查现有测试、Phase 5 完成报告、数据库配置、Docker 容器和 `.env` 键名。
- 未覆盖或删除用户已有数据库文件：`enterprise_business_agent.db`、`enterprise_business_agent_runs.db`、`phase5_acceptance_runs.db` 均保留。
- 没有重构项目整体结构；只在现有认证、工具注册、证据校验和 API 路由上做增量修改。

---

## 3. PostgreSQL 修复

### 3.1 环境事实

- 本机 `localhost:5432` 运行的是容器 `mark-postgres`（PostgreSQL 17.11，pgvector 镜像），不是 `docker-compose.yml` 中的 PG16 容器。
- 原 `.env` 的 `DATABASE_URL` 指向 `eba_admin`，但该角色和 `enterprise_business_agent` 数据库此前都不存在，所以 Phase 5 验收时连接失败。
- 本批次没有启动第二个 PostgreSQL 容器，避免与现有 5432 端口冲突；直接在当前 PG17 实例中补齐角色和数据库。

### 3.2 角色与权限

| 角色 | 用途 | 权限 |
| --- | --- | --- |
| `eba_admin` | 初始化、建表、生成数据（setup 连接） | LOGIN；NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOREPLICATION / NOBYPASSRLS；拥有 `enterprise_business_agent` 数据库 |
| `eba_readonly` | API 与分析进程的只读连接 | LOGIN；NOSUPERUSER / NOCREATEDB / NOCREATEROLE / NOREPLICATION / NOBYPASSRLS；CONNECT / USAGE / SELECT；`default_transaction_read_only=on`；`statement_timeout=15s`；`lock_timeout=3s` |

验证结果：

- `eba_readonly` 可读取 `sales_fact`（3072 行）。
- 在关闭会话级只读设置后，`INSERT` 与 `CREATE TABLE` 仍被数据库以 `42501 insufficient_privilege` 拒绝，证明拒绝来自授权本身，而不只是会话参数。
- API 不再使用 `eba_admin`：`.env` 新增 `READONLY_DATABASE_URL`，指向 `eba_readonly`；其密码由脚本生成并仅写入 `.env`，未打印、未写入本报告。
- `DATABASE_URL` 保持原有账号，仅作为初始化连接使用。

### 3.3 数据与结构

- 数据库：`enterprise_business_agent`
- 业务表：12 张
- 主要数据量：`sales_fact=3072`、`purchase_fact=768`、`inventory_snapshot=768`、`marketing_fact=376`、`finance_monthly=96`、`hr_monthly=120` 等。
- Phase 5 结构：`agent_runs`、`agent_audit_events`、`agent_evidence`、`agent_findings`、`agent_reports` 在 PostgreSQL 测试库 `eba_phase6_test` 中创建并验证。

### 3.4 真实连接验收

| 检查 | 结果 |
| --- | --- |
| `/health` | `200`，`database=ok`，`dialect=postgresql`，`readonly=true` |
| 结构化指标查询 | `200`，返回 2025-12 四个区域的销售额，来源表 `sales_fact` |
| 权限矩阵实库验证 | 6/6 符合预期（本域允许、跨域拒绝、区域过滤缺失/跨区拒绝、本区域允许） |
| Phase 5 PostgreSQL 专项 | `11 passed`：Run Store 往返、Evidence 去重、Finding/Report 持久化、只读执行、超时映射等 |
| PostgreSQL 不可用 | `engine` 直接抛出 `DBAPIError`；`/health` 返回 `503`、`status=degraded`、`database=unavailable`；没有静默降级为 SQLite |

> Run Store 仍使用 SQLite（默认 `RUN_STORE_URL`）。因此 API 启动日志显示 `database dialect=postgresql`、`run_store=sqlite`。Phase 5 的 PG 可用性是在独立 PG 测试库中验证的，不是把生产 Run Store 切换到了 PG。该差异见第 8 节。

---

## 4. Numeric Validator 误报修复

### 4.1 新的来源分类

| 分类 | 来源 | 是否可作为业务结果 |
| --- | --- | --- |
| `business_result` | 工具 `result` / `result_summary` 中的业务值；带 rate 语义的字段单独进入 `business_rates` | 是 |
| `tool_parameter` | `evidence.query_parameters`，以及工具调用参数 | 仅在与参数语境一致时允许；被写成比率/业务结果时拒绝 |
| `date_component` | 日期、月份、季度、年份表达式 | 仅作为日期组成部分，不计为业务数字 |
| `unsupported_number` | 证据中找不到的数字 | 否，产生 `unsourced_number` |

实现要点：

- 扫描散文前先遮蔽日期表达式，保留原始位置用于上下文判断。
- 业务值只从 `result` 和 `result_summary` 收集；`query_parameters` 单独进入 `tool_parameters`，不再自动成为业务证据。
- `tool_parameter_as_result`：参数值被写成“贡献率 20%”“贡献率 0.2”等业务结果时拦截。
- 无硬编码数字豁免；没有通过排除整句话来绕过校验。
- 年份类数字只有在明确的日期语境（如 `2025年`、`2025-06`）下才作为日期组件；`新增成本 2025 元` 仍会被判为无来源金额。

### 4.2 误报修复前后对比

| 输入 | 修复前 | 修复后 |
| --- | --- | --- |
| `观测期为 2025-06、2025-06-30 和 2025年6月` | 被拆成 2025、6、30 等业务数字 | 识别为 `date_component`，不报业务数字 |
| 贡献工具阈值 `0.2`，写成“使用阈值 0.2” | 可能被判为无来源业务数字 | `tool_parameter`，允许 |
| 同一阈值写成“贡献率为 20%” | 可能因参数进入允许集而通过 | `tool_parameter_as_result`，拒绝 |
| 同一阈值写成“贡献率 0.2” | 可能通过 | `tool_parameter_as_result`，拒绝 |
| `新增成本 987654.32 元，毛利率 77.7%` | 拒绝 | 仍拒绝，`unsourced_number` 同时包含两个值 |
| `新增成本 2025 元`，证据中恰有日期 2025 | 可能被年份豁免 | 拒绝，`unsourced_number` |
| `销售额由 1250.00 变为 1000.00，变化率 -0.2` | 允许 | 仍允许，`business_result` |

> 已知边界：如果某个工具参数值恰好等于证据中一个真实存在的比率，单靠数值相等无法判断“这句话说的是参数还是真实比率”。此时结构化 `value_refs` 和证据字段仍是强约束；Numeric Validator 不会仅凭语义猜测把真实比率判为参数。

---

## 5. 后端权限体系

### 5.1 角色与 Scope 矩阵

| 角色 | 主要 Scope | 默认数据域 | 资源访问 |
| --- | --- | --- | --- |
| `admin` | `metrics:read`、`agent:run`、`admin`、`resource:read_any`、`run/evidence/finding/report:read` | 无限制 | 所有 Run/Evidence/Finding/Report |
| `business_manager` | `metrics:read`、`agent:run`、`resource:read_any`、读资源 Scope | 无限制 | 所有 Run/Evidence/Finding/Report |
| `sales_analyst` | `metrics:read`、`agent:run`、读资源 Scope | `Sales` | 仅自己的资源 |
| `marketing_analyst` | 同上 | `Marketing` | 仅自己的资源 |
| `finance_analyst` | 同上 | `Finance` | 仅自己的资源 |
| `supply_chain_analyst` | 同上 | `Supply Chain` | 仅自己的资源 |
| `hr_analyst` | 同上 | `HR` | 仅自己的资源 |
| `analyst` | `metrics:read`、`agent:run`、读资源 Scope | 无限制（保持兼容） | 仅自己的资源 |
| `restricted_analyst` | 同上 | 由 `EBA_AUTHZ_POLICY` 指定指标/维度/区域 | 仅自己的资源 |
| `viewer` | `metrics:read` | 无限制 | 无资源读取权限 |

### 5.2 执行位置与覆盖范围

所有权限均在 FastAPI 后端执行，不依赖前端：

- **指标与业务域**：结构化查询、自然语言查询、工具执行三条路径都传递指标定义中的 `domain` 和 `source_table`。受限角色无法验证域/资源时直接拒绝，避免“查不到定义就放行”。
- **工具**：`ToolRegistry.specs()` 过滤不可用工具；`ToolRegistry.execute()` 在参数校验和执行前再次校验 `required_scope` 与角色工具白名单。
- **维度**：`require_dimensions()` 拒绝未授权维度；分组本身不能绕过。
- **区域**：`require_region_scope()` 要求区域受限角色的每次查询必须带区域过滤；`require_filters()` 只允许 `eq`/`in`，且值必须落在允许区域内。删除过滤、改成全区域 `in`、或仅按区域分组都会被拒绝。
- **Run / Evidence / Finding / Report**：每类资源有独立 Scope；默认按 `owner` 隔离，`admin` / `business_manager` 通过 `resource:read_any` 跨所有者读取。缺失资源 Scope 返回 403；资源不存在或不属于当前用户返回 404，避免泄露存在性。
- **取消与事件**：取消 Run、读取事件、SSE 流均先做资源授权，再把 `owner` 传给 Run Manager / Run Store。

### 5.3 RLS 决策

未引入 PostgreSQL RLS。现有业务表没有可信的 tenant 列或租户上下文，强行改造会改变数据模型；本批次按用户要求不把系统改造成多租户架构。当前边界是：

1. 后端角色/Scope/所有者校验；
2. 数据库层独立只读账号与最小授权；
3. SQL Guard 与只读执行器。

---

## 6. 修改文件

| 文件 | 修改内容 |
| --- | --- |
| `src/enterprise_business_agent/security/auth.py` | 扩展角色 Scope；部门默认域策略；工具白名单；资源 Scope 与所有权校验；域/资源不可验证时拒绝 |
| `src/enterprise_business_agent/agent/tools.py` | 工具列表与执行前权限校验；执行时传指标域/来源 |
| `src/enterprise_business_agent/nl/service.py` | 自然语言路径传递指标域/来源 |
| `src/enterprise_business_agent/api/app.py` | 暴露 metric resolver 供 API 授权使用 |
| `src/enterprise_business_agent/api/routes.py` | 资源级授权；Run/Evidence/Finding/Report 端点接入；结构化查询传域/来源 |
| `src/enterprise_business_agent/api/run_store.py` | 新增 `owner_of()` |
| `src/enterprise_business_agent/api/run_manager.py` | `cancel` / `events` 支持按 owner 限定 |
| `src/enterprise_business_agent/evidence/contracts.py` | 新增 `NumberClassification`；`ValidationIssue.category`；结果包含 `numbers` |
| `src/enterprise_business_agent/evidence/validator.py` | 来源分类、日期遮蔽、工具参数语境、年份金额防豁免 |
| `src/enterprise_business_agent/evidence/__init__.py` | 导出新类型 |
| `tests/test_numeric_validator.py` | 新增工具参数、日期、0.2 阈值、年份金额等回归测试 |
| `tests/test_phase6_permissions.py` | 新增权限矩阵、区域绕过、资源隔离测试 |
| `tests/test_security_config.py` | 新增 PostgreSQL 不可用不降级、health 503 测试 |
| `tests/test_phase5_postgres.py` | 新增 PostgreSQL Run Store 往返测试 |
| `scripts/init_db.py` | 输出 URL 时隐藏密码 |
| `scripts/run_phase3_demos.py` | 输出数据库时隐藏密码 |
| `.env` | 新增 `READONLY_DATABASE_URL`（只读账号）；未修改 API Key，未输出密码 |

---

## 7. 测试结果

### 7.1 全量 SQLite 测试

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```

结果：**434 passed, 11 skipped**

跳过项为未设置 `EBA_TEST_POSTGRES_URL` 时自动跳过的 PostgreSQL 专项测试；没有把跳过当作通过。

### 7.2 PostgreSQL 专项

使用独立测试库 `eba_phase6_test`，通过进程环境变量传入连接，未写入仓库、未打印：

```bash
EBA_TEST_POSTGRES_URL=<redacted> \
EBA_TEST_POSTGRES_READONLY_URL=<redacted> \
.venv/bin/python -m pytest -q tests/test_phase5_postgres.py tests/test_postgres_integration.py
```

结果：**11 passed**

覆盖：

- Phase 5 schema 创建与幂等；
- Evidence 往返与 `(run_id, fingerprint)` 去重；
- Finding 持久化；
- Report 版本追加（v1、v2）；
- PostgreSQL Run Store 创建、读取、owner 隔离、事件、状态更新；
- 结构化查询链与 SQLite 数值一致性；
- 只读引擎拒写；
- 超时映射为结构化错误；
- 独立只读角色拒写。

### 7.3 Phase 6 定向测试

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q \
  tests/test_phase6_permissions.py \
  tests/test_numeric_validator.py \
  tests/test_security_config.py
```

结果：**46 passed**

其中：

- 权限测试 10 条；
- Numeric Validator 26 条；
- 安全/连接配置 10 条。

### 7.4 真实 PostgreSQL API 验证

- `/health`：200，`database=ok`，`dialect=postgresql`，`readonly=true`。
- 指标查询：`sales_amount`，2025-12-01，按区域返回 4 个点，来源 `sales_fact`。
- 权限矩阵实库验证：6/6 符合预期。
- 未使用超级用户运行 API；API 使用 `eba_readonly`。

### 7.5 未执行的验证

- 没有执行真实 GLM/LLM 验收调用。本批次的结构化查询、权限和 PostgreSQL 验证不依赖 LLM；GLM 密钥轮换仍未确认，因此不声称已完成真实模型验收。
- 没有前端 UI 验收；本批次权限全部在后端执行，没有新增前端。

---

## 8. 未完成项与后续建议

1. **GLM API Key 轮换仍未确认。** 本批次按用户要求继续使用本地现有环境变量，没有读取、打印或修改密钥；在进行真实 LLM 调用前，仍需用户确认轮换完成。
2. **默认 Run Store 仍是 SQLite。** 本批次验证了 Phase 5 结构在 PostgreSQL 下可用，但 `.env` 未把 `RUN_STORE_URL` 切到 PostgreSQL。若要生产化：
   - 新建仅对 `agent_*` 表具有 `INSERT/UPDATE/DELETE`、对业务表仅 `SELECT` 的 runtime 角色；
   - 由初始化账号预创建 Run Store 表；
   - 再把 `RUN_STORE_URL` 指向该角色。
   不建议直接让 `eba_admin` 承担运行时写入。
3. **`eba_admin` 拥有数据库。** 它只用于初始化和建表，不被 API 使用；若要更严格，可拆分为“数据库所有者 / 迁移账号 / 初始化账号”三个角色。
4. **未引入 RLS。** 现有数据没有 tenant 维度，强行加 RLS 会改变架构；当前采用后端授权 + 数据库只读角色 + SQL Guard。
5. **区域策略是白名单等值/集合过滤。** 不支持层级区域（如大区下辖省份）或更复杂的行级策略；如未来需要，应在语义层增加区域层级后再扩展。
6. **Numeric Validator 的语义边界。** 当工具参数值与真实证据比率数值完全相同时，纯数值层无法区分语义；此类情况仍以结构化 `value_refs` 为主要约束。
7. **没有 Git 提交。** 当前目录不是 Git 仓库；本批次没有创建提交，也没有可回滚的 VCS 记录。

---

## 9. 复现命令

### 9.1 初始化 PostgreSQL（管理员连接，仅本地）

```bash
# 1) 创建角色与数据库（使用现有 .env 中的 eba_admin 密码；不打印密码）
# 2) 初始化业务表与数据
PYTHONPATH=src .venv/bin/python - <<'PY'
from enterprise_business_agent.config import load_env_file, get_settings
from enterprise_business_agent.db import Base, make_engine
from enterprise_business_agent import models  # noqa: F401
from enterprise_business_agent.data_generator import generate_data
from sqlalchemy.orm import Session

load_env_file(override=True)
settings = get_settings()
engine = make_engine(settings.database_url)
Base.metadata.create_all(engine)
with Session(engine) as session:
    generate_data(session, settings.data_start, settings.data_months, reset=True)
engine.dispose()
PY

# 3) 授权只读账号（DDL 由 eba_admin 执行；脚本不会打印密码）
#    GRANT CONNECT / USAGE / SELECT；REVOKE 写与 DDL；ALTER ROLE ... default_transaction_read_only=on
```

### 9.2 启动 API（使用只读连接）

```bash
set -a && source .env && set +a
PYTHONPATH=src .venv/bin/uvicorn enterprise_business_agent.api.app:app \
  --host 127.0.0.1 --port 8000
```

### 9.3 运行测试

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q

# PostgreSQL 专项（连接串通过环境变量传入，不写入文件）
EBA_TEST_POSTGRES_URL=<redacted> \
EBA_TEST_POSTGRES_READONLY_URL=<redacted> \
.venv/bin/python -m pytest -q tests/test_phase5_postgres.py tests/test_postgres_integration.py
```

---

## 10. 验收声明

- 本批次完成了真实 PostgreSQL 连接，并且 API 使用独立只读角色。
- 本批次没有使用 PostgreSQL 超级用户作为应用账号。
- PostgreSQL 不可用时不会静默降级为 SQLite，也不会在健康检查中报告成功。
- Numeric Validator 已区分业务结果、工具参数、日期组件和无来源数字；没有硬编码数字豁免或整句排除。
- 权限校验全部在后端执行，覆盖部门域、指标、维度、区域、工具以及 Run/Evidence/Finding/Report 资源。
- 所有测试结果均为实际执行结果；跳过项已明确标注，未混入未执行或脚本伪造的结果。
- 未打印、复制、记录或提交任何数据库密码或 API Key。
