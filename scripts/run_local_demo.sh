#!/usr/bin/env bash
# 本地一键试运行：SQLite 业务库 + SQLite 运行库，不依赖 PostgreSQL / Docker。
# 用法：bash scripts/run_local_demo.sh [端口]，默认 8000。
# 打开：http://127.0.0.1:<端口>/workbench/
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${1:-${PORT:-8000}}"

if [ ! -x .venv/bin/python ]; then
  echo "[setup] 创建虚拟环境并安装依赖"
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

# 业务库：不存在才初始化，避免覆盖已有演示数据
if [ ! -f enterprise_business_agent.db ]; then
  echo "[init] 初始化业务库 enterprise_business_agent.db"
  PYTHONPATH=src .venv/bin/python scripts/init_db.py \
    --database-url "sqlite:///./enterprise_business_agent.db"
  PYTHONPATH=src .venv/bin/python scripts/generate_data.py \
    --database-url "sqlite:///./enterprise_business_agent.db" --reset
else
  echo "[skip] 业务库已存在，复用现有数据"
fi

# 运行库：脚本本身幂等，可反复执行
echo "[init] 确保运行库表结构就绪"
PYTHONPATH=src .venv/bin/python scripts/init_runtime_store.py >/dev/null

# 数据看板的指标目录：语义词义层更新后自动重新导出
if [ "semantic_layer/metrics.json" -nt "src/enterprise_business_agent/web/metrics_catalog.json" ]; then
  echo "[init] 检测到指标定义更新，重新导出看板目录"
  .venv/bin/python scripts/export_metric_catalog.py
fi

echo "[run] http://127.0.0.1:${PORT}/workbench/  (Ctrl+C 停止)"
# 关键：EBA_LOAD_DOTENV=true 才会读取 .env 里的 GLM_API_KEY / LLM_PROVIDER；
# 三个 URL 显式覆盖 .env 中的 PostgreSQL 配置，走本地 SQLite。
EBA_LOAD_DOTENV=true \
PYTHONPATH=src \
DATABASE_URL='sqlite:///./enterprise_business_agent.db' \
READONLY_DATABASE_URL='sqlite:///./enterprise_business_agent.db' \
RUN_STORE_URL='sqlite:///./enterprise_business_agent_runs.db' \
exec .venv/bin/python -m uvicorn enterprise_business_agent.api.app:app \
  --host 127.0.0.1 --port "$PORT"
