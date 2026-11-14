# Phase 9.3 — Docker 构建部署、外部 OTLP 链路与浏览器端到端验收

日期：2026-11-06
状态：🟡 核心完成（Docker 构建与 Compose 部署通过；外部 OTLP 自建 Collector 链路通过；浏览器 E2E 在主机与 Compose 容器两条链路上均通过；容器到 provider 的网络波动与 canonical 重建阻塞仍保留）

## 1. 结论摘要

- **Docker 构建**：`python:3.12-slim` 首次拉取连续失败（`TLS handshake timeout`），网络恢复后拉取成功；仓库 Dockerfile 两次完整构建成功，镜像 410 MB，包含 OTLP HTTP exporter。第三次全量重建在 `pip install ".[otel]"` 下载 `psycopg-binary` 时因 PyPI 读取超时失败（约 80 分钟）。
- **部署**：`docker compose` 使用真实 PostgreSQL 16，init 容器执行 Alembic 迁移、初始化、只读/运行时角色、运行时 schema、Phase 5 迁移与数据种子，退出码 0；应用 `/health` 返回 200（`database=ok`、`llm_configured=true`），两个角色鉴权通过。
- **真实缺陷（部署阻塞）**：第一次 `docker compose up` 的 init 失败。根因是 `enterprise_business_agent/api/__init__.py` 在包导入时急切执行 `from .app import create_app`，而 `scripts/init_runtime_store.py` 只是导入 `api.runtime_schema` 就会触发 `create_app()`，在运行时表尚未创建时抛出 “run store schema is missing”。已改为惰性导出，并在真实 Compose PostgreSQL 上验证全部 init 步骤通过。
- **外部 OTLP**：使用官方 `otelcol-contrib 0.164.0`（SHA-256 校验通过）作为独立进程接收 OTLP/HTTP，应用以 `http.request` 及 `agent.*`/`llm.complete`/`tool.call`/`db.query`/`evidence.collect`/`finding.build`/`report.generate`/`approval.ensure` 等 span 导出；Collector 共写入 **257 个 span**，服务名均为 `enterprise-business-agent`。容器内应用也成功向宿主机 Collector 导出 span。
- **浏览器 E2E（主机后端）**：通过 CDP 驱动真实 Chrome，覆盖提问、调查、报告、Evidence、Findings、审批、历史/刷新和取消恢复，结果 `ok=true`。运行 `feee4134…` 完成，答案 952 字、4 步时间线、3 次工具调用；报告 3146 字、2 个指标、8 条结论、数字/引用校验通过；3 条 Evidence 全部可追溯；3 条 Finding；审批 `pending → approved`；历史 24 条，刷新后仍 24 条；取消状态 `running → cancelling → cancelled`。
- **浏览器 E2E 发现的真实缺陷**：首次 E2E 中点击取消后，服务端已把运行置为 `cancelled`，但工作台停留在 `cancelling`，因为取消后没有再轮询终态。已加入有界（最多 30 s）终态轮询，复测后 UI 正确显示 `cancelled`。
- **浏览器 E2E（Compose 容器）**：镜像更新为最终源码后，直接对 `http://127.0.0.1:8000` 重跑浏览器 E2E，`ok=true`。运行 `fdee9647…` 完成，答案 1014 字、4 步时间线、3 次工具调用；报告 2879 字、2 个指标、8 条结论、校验通过；3 条 Evidence 全部可追溯；3 条 Finding；审批 `pending → approved`；历史 3 条，刷新后 3 条；取消 `running → cancelling → cancelled`。该轮约 5.5 分钟。
- **模型调用超时缺陷**：容器内一次运行的首个模型调用耗时 **32 分 12 秒**后才返回（配置为单请求 120 s、连接 30 s、Run 预算 300 s）；同一次运行在重试后正常完成。证据表明调用线程被阻塞在超时清理路径上。已将客户端关闭移到独立守护线程，避免清理阻塞调用线程，并新增单测；该修复未在真实网络停顿中再次复现验证。
- **容器内真实模型链路**：存在明显的网络波动。一次容器内运行 `ed6c3f56…` 在首个决策上停顿 32 分钟后被取消；稍后容器内运行 `473ca49f…` 在 73 秒内完成。不能据此得出容器网络或 provider 的稳定性结论。
- **未验证/阻塞**：包含最终两处源码修复的单一 canonical 镜像构建（被 PyPI 读超时阻塞，部署镜像由“已构建镜像 + COPY src”的派生层生成）；容器内浏览器 E2E 的完整真实模型结果；OTLP 托管后端/采样比例/长期存储；密钥轮换的技术验证（此前“确认安全”是用户声明）。

## 2. 环境与前置

| 项目 | 值 |
| --- | --- |
| 主机 | macOS Darwin 24.6.0（arm64） |
| Docker | 29.7.2 |
| Docker Compose | v5.5.1 |
| 基础镜像 | `python:3.12-slim`（digest `sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e`） |
| 数据库 | PostgreSQL 16（`postgres:16` 容器） |
| 应用镜像 | `enterprise-business-agent:phase93`，410 MB |
| OTLP Collector | `otelcol-contrib 0.164.0`，SHA-256 `af6a0c56886e3cc36ccebe849fa167bad2bc47a97a48fd8497a0d6f93d0cf73a` |
| 浏览器驱动 | Chrome DevTools Protocol + `websockets 17.3` |

- 宿主 5432 已被既有 `mark-postgres` 占用，本阶段**未停止**该容器；Compose 的 PostgreSQL 映射到 `55432`，应用映射到 `8000`。
- API Key 与数据库密码为本阶段生成，保存在权限 `0600` 的环境文件中，未在任何日志或输出中打印。**密钥轮换未做技术验证**；此前用户回复“确认安全”属于用户声明，不构成验证结果。
- 真实模型：`glm` / `glm-5.3-flash`，`LLM_TIMEOUT_SECONDS=120`、`LLM_CONNECT_TIMEOUT_SECONDS=30`、`LLM_READ_TIMEOUT_SECONDS=120`、`LLM_MAX_RETRIES=1`、`AGENT_MAX_SECONDS=300`、`AGENT_MAX_TOOL_CALLS=6`。

## 3. Docker 镜像构建

### 3.1 首次拉取失败与恢复

`docker pull python:3.12-slim` 连续 6 次失败：

```
net/http: TLS handshake timeout  (auth.docker.io / registry-1.docker.io)
```

镜像加速地址 `docker.m.daocloud.io` 同样 TLS 超时。期间尝试以本地 `postgres:16` 为基础、用 apt 安装 Python 的备选 Dockerfile，两次构建均被 OOM 杀死（退出码 137），一次在 BuildKit 阶段 “cannot allocate memory”，一次在 legacy builder 的 apt 安装阶段被杀。该备选路径最终**未使用**。

网络恢复后，`python:3.12-slim` 拉取成功（digest 见上表），正式构建得以进行。

### 3.2 正式构建

```bash
docker build -t enterprise-business-agent:phase93 .
```

- 第一次构建成功，镜像 410 MB，`pip install ".[otel]"` 安装了 OTel SDK 与 OTLP/HTTP exporter。
- 镜像内验证：

```bash
docker run --rm enterprise-business-agent:phase93 sh -c \
  'python -c "import opentelemetry.exporter.otlp.proto.http; print(\"otlp-http ok\")"'
# otlp-http ok
```

- 第二次构建（包含工作台取消刷新修复）成功；第三次全量重建（包含 import 修复与 transport 修复）在 `pip install` 阶段失败：

```
TimeoutError: The read operation timed out   # psycopg-binary 下载，176 kB/s
ERROR: process "/bin/sh -c pip install \".[otel]\"" did not complete successfully: exit code: 2
```

### 3.3 部署镜像的构建路径（重要偏差）

由于 PyPI 下载超时，最终部署镜像不是单次 canonical `docker build` 的产物，而是：

```dockerfile
FROM enterprise-business-agent:phase93
COPY src /app/src
USER eba
```

即“已成功构建的镜像 + 覆盖最终源码”。`PYTHONPATH=/app/src` 使运行时使用覆盖后的源码。仓库 Dockerfile 本身未被修改为绕过依赖安装；`docker build -t enterprise-business-agent:phase93 .`（不含最后两处源码修复）已真实成功。**这一点不能表述为“最终版本镜像已通过一次完整 canonical 构建”。**

## 4. Docker Compose 部署

### 4.1 命令

```bash
docker compose -p eba-phase93 --env-file /tmp/eba-phase93.env up -d --no-build
```

使用独立项目名 `eba-phase93`，避免覆盖既有 `enterprise_business_agent_eba_postgres_data` 卷。环境文件含生成的 API Key、数据库密码和模型配置，权限 `0600`，内容未输出。

### 4.2 init 首次失败与修复

第一次执行时 init 容器退出码 1：

```
RuntimeError: run store schema is missing; run scripts/init_runtime_store.py
with the setup role before starting the API with the runtime role
```

根因：`scripts/init_runtime_store.py` 导入 `enterprise_business_agent.api.runtime_schema`，而 `api/__init__.py` 中的 `from .app import create_app` 会在包导入时立即执行 `create_app()`，`create_app()` 又构造 `PersistentRunStore`，此时运行时表尚未创建。

修复：`api/__init__.py` 改为惰性导出（`__getattr__`），导入 `api.runtime_schema` 不再加载 `api.app`。本地验证：

```
app module imported: False
lazy exports ok True ResultStore
```

修复后在真实 Compose PostgreSQL 上重跑全部 init 步骤，均返回 0：

```
alembic upgrade head        -> 0
scripts/init_db.py          -> 0
scripts/create_readonly_role.py -> 0
scripts/init_runtime_store.py   -> 0
scripts/migrate_phase5.py       -> 0
```

### 4.3 部署结果

- `docker compose ps`：`postgres` healthy；`init` 退出码 `0`；`app` 运行。
- `public` schema 共 **23 张表**，包括业务表与 `agent_runs`、`agent_checkpoints`、`agent_evidence`、`agent_findings`、`agent_reports`、`agent_report_approvals`、`agent_audit_events` 等运行时表。
- 种子数据（`EBA_SEED_DATA=true`，`DATA_START=2025-01-01`，`DATA_MONTHS=12`）：

```
sales_fact          3072
purchase_fact        768
marketing_fact       376
inventory_snapshot   768
finance_monthly       96
hr_monthly           120
campaigns             13
products               8
regions                4
channels               4
departments            5
suppliers              4
```

- `/health`：

```json
{"status":"ok","database":"ok","dialect":"postgresql","readonly":true,
 "llm_configured":true,"agent_available":true,"auth_enabled":true,
 "anonymous_allowed":false,"version":"0.6.0"}
```

- `/api/v1/me`：`analyst` 与 `business_manager` 均返回对应角色且 `authenticated=true`。

## 5. 外部 OTLP 链路

### 5.1 Collector

使用官方 `otelcol-contrib 0.164.0` 独立进程（非应用内嵌），配置见 `deploy/otel-collector.yaml`：

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
exporters:
  file:
    path: /data/traces.jsonl
  debug:
    verbosity: normal
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [file, debug]
```

宿主验证时使用同一配置，仅把文件路径改为 `/tmp/eba_otelcol_out/traces.jsonl`。

### 5.2 结果

- 应用配置：`EBA_OTEL_ENABLED=true`、`EBA_OTEL_EXPORTER=otlp`、`EBA_OTEL_EXPORTER_ENDPOINT=http://host.docker.internal:4318/v1/traces`、`EBA_OTEL_SAMPLE_RATIO=1.0`。
- Collector 文件导出共 **257 个 span**，`service.name` 全部为 `enterprise-business-agent`。
- 观察到 span 名称：`http.request`、`agent.run`、`agent.decision`、`llm.complete`、`tool.call`、`db.query`、`evidence.collect`、`finding.build`、`report.generate`、`approval.ensure`。
- 来源区分：
  - 宿主应用（8004）首次链路验证：5 个 `http.request` span。
  - 容器应用启动与决策阶段：`agent.run`、`agent.decision` 与持续健康检查 `http.request` span。
  - 宿主后端完整工作流（17:25–17:49 UTC）：覆盖报告生成与审批的完整 span 集合。

### 5.3 边界

- Collector 在宿主机运行，**不是** Compose 栈中的服务；容器到宿主 Collector 的导出已验证，但“Compose 内的 Collector 服务”未验证。
- 未验证托管 OTLP 后端（Jaeger/Tempo/云厂商）、长期存储、采样比例控制和 Collector 重启后的数据完整性。
- Collector 只收到应用定义的 span；未验证指标/日志导出链路。

## 6. 浏览器端到端测试

驱动脚本：`scripts/browser_e2e.py`。通过 Chrome DevTools Protocol 打开 `/workbench/`，连接真实后端，提交问题并等待终态，读取答案、时间线、工具调用、报告、Evidence、Findings、审批状态，切换角色审批，刷新历史，再提交并取消一次运行。断言失败会写 `browser_e2e.json`（`ok=false`）并截图。

### 6.1 主机后端（8003）第一轮 — 发现取消 UI 缺陷

- 运行 `e36c630b…`，状态 `completed`，耗时约 170 s。
- 答案 967 字、4 步时间线、3 次工具调用；报告 3278 字、2 个指标、8 条结论、校验徽章通过；3 条 Evidence 全部可追溯；3 条 Finding；审批 `pending → approved`；历史 21 条，刷新后 21 条。
- **失败点**：点击取消后 UI 依次显示 `running → cancelling`，120 s 内未到终态；服务端日志显示同一运行在 0.8 s 内已 `cancelled`。这是真实的 UI 缺陷，不是服务端失败。

修复：`web/app.js` 的 `cancelRun` 在取消后最多轮询 60 次（每次 500 ms）直到终态，并加入 `TERMINAL_STATUSES` 常量。

### 6.2 主机后端（8003）复测 — 全流程通过

夹具：`data/exports/phase9_3_browser/browser_e2e.json` 与 11 张截图。

| 检查项 | 结果 |
| --- | --- |
| 工作台加载 / 分析师连接 | true / true |
| 运行状态 | `completed` |
| 最终答案 | 952 字 |
| 时间线 / 工具调用 | 4 步 / 3 次 |
| 报告 | 3146 字，2 个指标，8 条结论，`数字/引用校验通过` |
| Evidence | 3 条，3 个可追溯徽章 |
| Findings | 3 条 |
| 审批 | `pending` → `approved`（业务经理角色） |
| 历史 | 24 条，刷新后 24 条 |
| 取消 | `running → cancelling → cancelled`，历史 25 条，提交按钮可用 |
| 总体 | `ok=true` |

- 运行 `feee4134…` 的服务端 `execution_seconds=200.775`；但墙钟从提交到完成约 **24 分钟**，其中最后一次模型响应到 `run_finished` 之间有 **23 分 43 秒**的停顿。该停顿与第 7.3 节的超时清理缺陷一致。
- 宿主 E2E 使用 `--max-tool-calls 3`，避免 6 次工具调用在 300 s 预算内触发 `llm_timeout`。

### 6.3 容器后端（8000）— 网络波动下的结果

第一次尝试（镜像修复前）：容器无法稳定访问 provider。直接探测：

```
ERR URLError <urlopen error _ssl.c:994: The handshake operation timed out> secs 15.1
```

运行 `ed6c3f56…` 在 16:36:59 进入首个决策，直到 17:09:11 才返回模型响应（32 分 12 秒），随后完成一次工具调用；17:09:26 被取消接口置为 `cancelled`。取消接口本身在 3 s 内生效。

稍后用同一容器提交运行 `473ca49f…`（`max_tool_calls=1`），73 秒完成，状态 `completed`。这说明网络可达性是波动的，不能据此对容器网络下稳定性结论。

第二次浏览器 E2E 尝试（镜像更新前）：Chrome DevTools 连接在启动阶段断开（浏览器进程崩溃，当时 Docker 构建占用内存），未产生应用层结论。

第三次浏览器 E2E（镜像更新为最终源码后，直接访问 Compose 应用 `http://127.0.0.1:8000`）：**`ok=true`，全流程通过**，归档在 `data/exports/phase9_3_browser_container/`（11 张截图）。

| 检查项 | 容器 E2E 结果 |
| --- | --- |
| 运行 | `fdee9647…`，`completed`，约 5.5 分钟 |
| 答案 / 时间线 / 工具调用 | 1014 字 / 4 步 / 3 次 |
| 报告 | 2879 字，2 个指标，8 条结论，`数字/引用校验通过` |
| Evidence / Findings | 3 条，3 个可追溯徽章 / 3 条 |
| 审批 | `pending → approved`（业务经理） |
| 历史 | 3 条，刷新后 3 条；取消后 4 条 |
| 取消 | `running → cancelling → cancelled`，提交按钮可用 |
| 总体 | `ok=true` |

该结果证明：Compose 部署、真实 PostgreSQL、真实 GLM 调用、工作台 UI、Evidence/Findings、审批与取消恢复在同一条链路上全部可用。先前 32 分钟停顿与随后 73 秒完成并存，只说明网络波动确实存在，不能推出稳定性结论。

## 7. 发现的问题与修复

### 7.1 init 容器无法创建运行时 schema（阻塞部署）

- 现象：`docker compose up` 的 init 退出码 1，`RuntimeError: run store schema is missing`。
- 根因：`api/__init__.py` 急切导入 `app`，包导入即构造 `PersistentRunStore`。
- 修复：`api/__init__.py` 改为惰性导出；`scripts/init_runtime_store.py` 可在建表前导入 `api.runtime_schema`。
- 验证：本地确认导入 `runtime_schema` 不加载 `api.app`；真实 Compose PostgreSQL 上五步 init 全部返回 0。

### 7.2 取消后工作台不刷新终态

- 现象：服务端已 `cancelled`，UI 停留 `cancelling`。
- 根因：`cancelRun()` 中止进度流后只调用一次 `loadRun()`，若此时服务端仍处于中间态，不再更新。
- 修复：取消后以 500 ms 间隔轮询运行状态，最多 30 s，直到 `TERMINAL_STATUSES`。
- 验证：复测 UI 记录 `running → cancelling → cancelled`。

### 7.3 超时清理阻塞调用线程

- 现象：容器内一次模型调用耗时 32 分 12 秒才返回；宿主 E2E 也出现 23 分 43 秒停顿。配置为单请求 120 s、连接 30 s。
- 证据：运行在停顿期间数据库空闲、进程 CPU ~1%，取消接口可立即生效；停顿结束后重试很快成功。
- 根因判断：`transport._interrupt()` 在调用线程上执行 `client.close()`；httpcore 在 TLS 握手卡住时持有连接锁，`close()` 可能一直等待，导致调用线程被阻塞，总超时无法按时抛出。
- 修复：`_interrupt()` 保留 socket `shutdown`，把 `close()` 放到独立守护线程，调用线程立即继续抛超时。
- 验证：新增单测 `test_interrupt_does_not_wait_for_a_blocked_client_close`，`tests/test_phase9_2_1_reliability.py` 12 passed；完整离线回归 **765 passed, 30 skipped**。**未在真实网络停顿中再次复现 32 分钟挂起**，因此该修复只证明“清理不再阻塞调用线程”，不证明所有网络停顿都会在 120 s 内结束。

## 8. 未验证与阻塞项

1. **包含最终源码的单一 canonical 镜像构建**：第三次全量构建因 PyPI 读取超时失败；部署镜像为派生层（`FROM` + `COPY src`）。仓库 Dockerfile 的标准构建路径本身已验证成功（两次），但未包含最后两处修复。
2. **容器内真实模型链路的重复稳定性**：容器 E2E 单次通过，但同一容器早前出现过 32 分钟停顿；未做多次重复运行，不能据此宣称稳定。
3. **Compose 内运行的 OTLP Collector**：本阶段 Collector 在宿主运行。
4. **托管 OTLP 后端、采样比例、长期存储与告警**：未验证。
5. **托管 PostgreSQL、多节点、TLS、备份/PITR、容量压测**：未验证（沿用 Phase 9.2 边界）。
6. **密钥轮换**：用户声明“确认安全”，本工具未做控制台或凭据轮换验证。
7. **Docker Hub 拉取的长期可用性**：本阶段多次 TLS 超时，最终成功但不可作为网络 SLA。
8. **真实模型质量结论**：本阶段 E2E 只验证单条问题的全流程，不构成模型质量提升或回退结论；Phase 9.1/9.2.1 的通过率数字仍是唯一质量基线。
9. **模型单价与成本**：未配置，成本保持 `null`。

## 9. 复现命令

```bash
# 构建（标准路径）
docker build -t enterprise-business-agent:phase93 .

# 启动 Compose（独立项目名，避免覆盖既有卷）
docker compose -p eba-phase93 --env-file /tmp/eba-phase93.env up -d --no-build

# 健康检查
curl -s http://127.0.0.1:8000/health

# 外部 OTLP Collector（宿主）
/tmp/eba-otelcol/otelcol-contrib --config /tmp/eba-otelcol.yaml

# 浏览器 E2E
.venv/bin/python scripts/browser_e2e.py \
  --base-url http://127.0.0.1:8003 \
  --keys-file /tmp/eba-phase93-keys.json \
  --output-dir data/exports/phase9_3_browser \
  --run-timeout 420 --max-tool-calls 3
```

归档：

- 主机 E2E：`data/exports/phase9_3_browser/browser_e2e.json` 与 11 张 PNG。
- 容器 E2E：`data/exports/phase9_3_browser_container/`（见第 6.3 节）。
- Collector 输出：`/tmp/eba_otelcol_out/traces.jsonl`（宿主临时路径，未纳入仓库）。
- 构建/部署日志：`/tmp/eba_build_phase93*.log`、`/tmp/eba_compose_up*.log`（临时路径）。

## 10. 本阶段变更文件

| 文件 | 变更 |
| --- | --- |
| `Dockerfile` | 安装 `.[otel]`，镜像包含 OTel SDK 与 OTLP/HTTP exporter |
| `docker-compose.yml` | PostgreSQL 宿主端口可配置；应用透传模型、超时、熔断、Agent 预算与 OTel 环境；`extra_hosts: host.docker.internal:host-gateway` |
| `deploy/otel-collector.yaml` | 新增外部 Collector 配置（OTLP/HTTP → file + debug） |
| `scripts/browser_e2e.py` | 新增 CDP 驱动的浏览器验收脚本，支持 `--run-timeout`、`--max-tool-calls` |
| `src/enterprise_business_agent/web/app.js` | 取消后轮询终态；新增 `TERMINAL_STATUSES` |
| `src/enterprise_business_agent/api/__init__.py` | 改为惰性导出，避免导入包即构造应用 |
| `src/enterprise_business_agent/llm/transport.py` | 超时清理不再阻塞调用线程（关闭操作移至守护线程） |
| `tests/test_phase9_2_1_reliability.py` | 新增 1 个回归测试（清理不阻塞） |
| `data/exports/phase9_3_browser/` | 主机浏览器 E2E 归档（JSON + 11 张截图） |
| `data/exports/phase9_3_browser_container/` | Compose 容器后端浏览器 E2E 归档（JSON + 11 张截图） |

## 11. 结论

Phase 9.3 完成了本阶段要求的真实验证：Docker 镜像构建、Compose + 真实 PostgreSQL 部署、init/migrations/seeding、健康检查与鉴权、外部 OTLP Collector 链路、以及浏览器端到端全流程（提问 → 调查 → 报告 → Evidence → Findings → 审批 → 历史/刷新 → 取消恢复）。

同时必须明确：

- 部署镜像的最终源码来自派生层，不是一次完整的 canonical 构建；标准构建路径已成功但未覆盖最后两处修复。
- 容器内真实模型链路受网络波动影响，出现过 32 分钟停顿；同一镜像随后 73 秒完成、5.5 分钟浏览器 E2E 全流程通过，但都是单次样本，不能据此宣称容器或 provider 稳定。
- 超时清理阻塞问题已修复并有单测，但没有在真实停顿中再次验证，因此“所有调用都会在 120 s 内结束”仍未证明。
- 外部 OTLP Collector 在宿主运行；托管后端、采样、长期存储未验证。
- 密钥轮换未做技术验证。
