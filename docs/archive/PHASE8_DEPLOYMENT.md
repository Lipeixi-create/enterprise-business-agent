# Phase 8 部署与启动指南

本文面向本地验收和单机 Docker 部署。生产环境仍需补充 TLS、反向代理、密钥管理、备份和监控告警，不能把本文件当作完整生产运维手册。

## 1. 本地 SQLite 启动

```bash
cd enterprise_business_agent
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export PYTHONPATH=src

python scripts/init_db.py --database-url sqlite:///./enterprise_business_agent.db
python scripts/generate_data.py --database-url sqlite:///./enterprise_business_agent.db --reset
uvicorn enterprise_business_agent.api.app:app --host 127.0.0.1 --port 8000
```

打开 `http://127.0.0.1:8000/workbench/`。默认匿名访问只适用于本地开发；需要角色隔离时设置 `EBA_API_KEYS` 并在工作台顶部输入对应 Key。

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
```

## 2. Docker Compose 启动

### 2.1 准备环境文件

```bash
cp .env.docker.example .env.docker
# 编辑 .env.docker，替换全部 change-me 值
```

必须设置：

| 变量 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL 本地容器密码 |
| `EBA_RUNTIME_PASSWORD` | `eba_runtime` 运行时角色密码 |
| `EBA_READONLY_PASSWORD` | `eba_readonly` 业务只读角色密码 |
| `EBA_API_KEYS` | API Key 到角色的 JSON 映射 |

`.env.docker` 已被 `.gitignore` 忽略，不要提交真实值。

### 2.2 启动

```bash
docker compose --env-file .env.docker up --build
```

启动顺序：

1. `postgres` 等待健康检查通过；
2. `init` 执行 `alembic upgrade head`，再执行 `init_db.py`、`create_readonly_role.py`、`init_runtime_store.py`、`migrate_phase5.py`；
3. `init` 成功后 `app` 启动，API 使用 `eba_readonly` 查询业务数据、使用 `eba_runtime` 读写运行状态；
4. 工作台地址为 `http://127.0.0.1:8000/workbench/`。

需要演示数据时，在 `.env.docker` 中设置 `EBA_SEED_DATA=true`。该选项会重置业务数据，只应在本地或一次性演示环境使用。

查看状态和日志：

```bash
docker compose --env-file .env.docker ps
docker compose --env-file .env.docker logs -f app
docker compose --env-file .env.docker logs init
```

停止并保留数据库卷：

```bash
docker compose --env-file .env.docker down
```

删除数据库卷会丢失运行记录和业务数据，执行前必须确认：

```bash
docker compose --env-file .env.docker down -v
```

### 2.3 健康检查与日志

- `postgres` 使用 `pg_isready`；
- `app` 使用 Python `urllib` 请求 `/health`；
- 容器日志使用 Docker `json-file` 驱动，单文件 10 MB，最多保留 5 个；
- 应用日志为 JSON，包含 `request_id`、`trace_id`、`span_id`、方法、路径、状态码和耗时；请求体和密钥不会写入。

### 2.4 可选 tracing

`.env.docker.example` 默认关闭 tracing。本地查看 span：

```text
EBA_OTEL_ENABLED=true
EBA_OTEL_EXPORTER=file
EBA_OTEL_TRACE_FILE=/app/data/traces/eba-spans.jsonl
```

`file` 模式需要把 `data/traces` 挂载到持久卷，或直接 `docker compose cp` 取出后使用 `scripts/view_traces.py` 查看。接入真实 OTLP Collector 时改为 `EBA_OTEL_EXPORTER=otlp` 并设置 `EBA_OTEL_EXPORTER_ENDPOINT`；本仓库尚未验证真实 OTLP 网络导出。

## 3. 不使用 Docker 的 PostgreSQL 部署

```bash
export DATABASE_URL='postgresql+psycopg://<admin>:<password>@<host>:5432/enterprise_business_agent'
export ADMIN_DATABASE_URL="$DATABASE_URL"
export EBA_RUNTIME_SETUP_URL="$DATABASE_URL"
export EBA_RUNTIME_PASSWORD='<runtime-password>'
export EBA_READONLY_PASSWORD='<readonly-password>'
export PYTHONPATH=src

alembic upgrade head
python scripts/init_db.py
python scripts/create_readonly_role.py
python scripts/init_runtime_store.py
python scripts/migrate_phase5.py
```

API 进程只加载：

```text
READONLY_DATABASE_URL=postgresql+psycopg://eba_readonly:<password>@<host>:5432/enterprise_business_agent
RUN_STORE_URL=postgresql+psycopg://eba_runtime:<password>@<host>:5432/enterprise_business_agent
EBA_API_KEYS=...
EBA_ALLOW_ANONYMOUS_API=false
```

不要把 `DATABASE_URL`、`ADMIN_DATABASE_URL`、`EBA_RUNTIME_SETUP_URL` 或初始化密码带入 API 进程。

## 4. 上线前检查

```bash
python scripts/preflight.py --env-file <env-file>
```

存在 `FAIL` 时不要上线。另需确认：

- `EBA_API_KEYS` 使用长随机值，且已通过密钥管理系统注入；
- 数据库连接使用 TLS（如环境支持）；
- 反向代理终止 TLS，并限制 `/mcp` 和工作台访问来源；
- 日志、备份和运行数据保留策略已配置；
- Phase 7.9 已知风险仍被跟踪：`live-004` / `live-005` 提供方超时、`live-023` 结论越界、模型成本单价未配置。

## 5. 当前验证边界

- `docker compose config` 已用占位配置验证；
- 当前开发环境无法从 Docker Hub 拉取 `python:3.12-slim`，因此 **Docker 镜像构建和 `docker compose up` 未完成实跑**；
- 本地已用 SQLite + `uvicorn` 实际验证工作台页面、身份、运行历史、异步提交、SSE 和失败落库；
- 没有浏览器自动化环境，前端只做静态资源检查和 HTTP 链路验证，未做真实浏览器视觉/交互验收。
