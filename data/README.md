# data/ 目录边界

本目录只存放**可再生的本地数据产物**，不存放代码、密钥或生产数据。

| 路径 | 内容 | 是否提交 | 说明 |
| --- | --- | --- | --- |
| `data/snapshots/` | SQLite 数据库快照、导出的 PostgreSQL 快照 | 否（仅 `.gitkeep`） | 快照可能包含完整经营数据，默认不进入版本库 |
| `data/exports/` | 评测结果、CSV/JSON 导出 | 否（仅 `.gitkeep`） | 由脚本生成，可重复运行 |
| `data/README.md` | 目录约定 | 是 | 本文件 |
| `../enterprise_business_agent.db` | 本地 SQLite 开发库 | 否 | 由 `scripts/init_db.py` + `generate_data.py` 生成 |

## 敏感配置

- 数据库密码只通过环境变量或本地 `.env` 提供；`.env` 已被 `.gitignore` 忽略。
- `.env.example` 只允许出现占位符，不得写入真实密码、API Key 或连接串。
- 应用服务只使用 `READONLY_DATABASE_URL`；`DATABASE_URL` 只用于建表、生成数据和创建只读角色。
- 日志不得打印连接串、密码、LLM API Key 或 SQL 绑定参数值；SQL 日志只记录哈希。

## 测试数据

- 单元测试使用内存 SQLite 或临时目录数据库，不读取 `data/snapshots/`。
- 集成测试如使用快照，必须显式传入路径，并在测试结束后清理临时文件。
- 模拟数据可以提交生成脚本与固定随机种子，但不提交生成后的数据库文件。
