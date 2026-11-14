# Security Policy

## 支持范围

当前维护的是 `0.6.x`。安全修复只针对 `main` 分支的最新代码。

## 报告方式

请通过 GitHub Security Advisories 私下报告，不要先开 public issue：

<https://github.com/Lipeixi-create/enterprise-business-agent/security/advisories/new>

报告时请包含：受影响接口或模块、复现步骤、期望行为与实际行为。**不要在报告里粘贴真实 API Key、数据库密码或生产数据。**

## 重点关注的问题

- SQL Guard 绕过（多语句、注释、字面量过滤、DDL/DML、未知表或函数）；
- 认证或授权绕过（角色 scope、指标/维度/区域策略、跨账号资源访问）；
- 通过工具结果或缓存重放导致的越权；
- 日志、错误响应或 trace 中的密钥泄露；
- Prompt Injection 导致未授权工具调用或数据外泄。

## 设计边界（不是漏洞）

- 模型不持有数据库连接，也不能生成或执行 SQL；
- 业务库使用只读角色，运行时库只允许 `agent_*` 表 DML；
- 工具超时依赖线程 Future，没有进程级资源隔离；
- 当前授权是角色级策略，不是完整 RBAC/RLS；
- 本项目未做托管环境、多节点和容量压测。

## 已知限制

完整列表见 [docs/PROJECT_DEVELOPMENT.md 第 12 节](docs/PROJECT_DEVELOPMENT.md#12-已知限制)。
