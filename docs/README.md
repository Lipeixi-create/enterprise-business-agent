# 文档导航

> Enterprise Business Agent —— 企业多部门经营分析 Agent。
> 每个数字可追溯，每次调查可复盘，每条限制如实记录。

## 从哪里开始

| 文档 | 适合谁 | 内容 |
| --- | --- | --- |
| [../README.md](../README.md) | 第一次接触项目 | 项目定位、架构速览、快速开始、验证结果 |
| [PROJECT_DEVELOPMENT.md](PROJECT_DEVELOPMENT.md) | 面试 / 工程复盘 | 技术栈、系统架构图、核心算法、安全与可靠性设计 |
| [INTERVIEW_QUESTIONS.md](INTERVIEW_QUESTIONS.md) | 准备面试 | Agent 岗位高频题、参考答案、压力追问、自测清单 |
| [01-architecture.md](01-architecture.md) | 想看设计取舍 | 目标架构、分层职责、API 与数据契约 |
| [02-data-and-metrics.md](02-data-and-metrics.md) | 想看数据与指标 | 五域数据模型、34 个指标口径、语义层 |
| [03-agent-runtime.md](03-agent-runtime.md) | 想看 Agent | 状态图、工具契约、Evidence Chain |
| [04-security-and-governance.md](04-security-and-governance.md) | 想看安全 | SQL Guard、权限模型、审计 |
| [05-development-roadmap.md](05-development-roadmap.md) | 想看演进过程 | 分阶段目标、验收标准、已知差距 |
| [06-evaluation-and-observability.md](06-evaluation-and-observability.md) | 想看评测 | 评测语料、评分维度、trace 与告警 |
| [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) | 想部署 | 只读检查、初始化、上线步骤 |

## 当前状态（摘要）

- 五域业务数据 + 34 个指标口径 + 7 个维度注册表；
- 自然语言 → 语义解析 → 参数化 SQL → 确定性分析；
- 单 Agent LangGraph 决策循环，8 个只读工具，三重预算与语义去重；
- Evidence Ledger → Finding → 归因 → 12 区块报告 → Numeric Validator → 人工审批；
- PostgreSQL 运行时持久化、Checkpoint 恢复、Lease/Fencing、幂等与事务化清理；
- 工作台、SSE、取消、历史、审批；
- OpenTelemetry 全链路 span，支持 file / OTLP / console / none；
- Docker Compose 部署、外部 OTLP Collector、浏览器 E2E；
- 离线回归 765 passed / 30 skipped；真实 GLM 92 题 as-run 69/90。

完整边界与未验证项见 [PROJECT_DEVELOPMENT.md 第 12 节](PROJECT_DEVELOPMENT.md#12-已知限制)。

## 阶段性验收报告

历史阶段的逐轮报告、评测明细与验收记录统一归档在 [archive/](archive/)，按阶段组织：

- `archive/phase5/`：数据层与语义查询完成记录；
- `archive/phase6/`：运行时持久化、Checkpoint、审批；
- `archive/phase7/`：评测语料、评分器与多轮真实模型结果；
- `archive/phase8/`：可观测性、安全/MCP、工作台、生产验收；
- `archive/phase9/`：真实模型稳定性、PostgreSQL 验收、模型调用可靠性、Docker/OTLP/浏览器 E2E。

归档文档保留原始结论与当时的未验证项，可能与最新代码状态不一致；以 [PROJECT_DEVELOPMENT.md](PROJECT_DEVELOPMENT.md) 和根目录 [README.md](../README.md) 为准。
