# Phase 6 收尾 + Phase 7 升级最终验收

项目：`enterprise_business_agent`
版本：`0.6.0`
验收日期：2026-10-31

> 本报告只记录**本轮实际执行的命令与结果**。未执行的项目明确写"未执行"并说明原因，
> 不把脚本化模型结果、历史记录或推测当作本轮通过证据。
>
> 本轮没有连接或修改任何真实生产服务器，没有切换生产 `.env`，没有创建或修改生产角色，
> 没有读取、打印、复制或提交任何数据库密码/API Key/Token，没有轮换 GLM API Key。

---

## 1. 最终结论

| 阶段 | 状态 | 结论 |
| --- | --- | --- |
| 第一阶段：Phase 6 收尾与可靠性 | 完成 | 截断/异常处理、分段叙述、OTel、Preflight、部署文档与 Phase 6 验收报告已落地并通过测试 |
| 第二阶段：Phase 7 评测与可观测性 | 完成（框架） | 92 条语料、数据库实测真值、多维评分器、回归门禁、metrics 成本/性能扩展与 4 份文档已完成 |
| 第三阶段：综合回归与最终验收 | 完成 | SQLite、Phase 6 定向、PostgreSQL、多进程并发、五场景、一致性、异常、预算、OTel、Phase 7 回归均实际执行 |
| 真实 GLM 复测 | **未执行** | 未获得"当前 Key 状态已人工确认"的结论，按安全约束不调用真实模型 |
| 真实 OTLP 后端 | **未验证** | 本机没有 Collector/厂商后端，只能确认 span 生成与计数 |
| 生产部署 | **未执行** | 未接触生产环境；部署与角色创建需人工按变更流程执行 |

**总体判断：代码与本地/一次性测试库验证通过，可进入受控预发布；不能据此声称生产已就绪或真实模型质量已验收。**

---

## 2. 第一阶段交付

| 文件 | 变更 |
| --- | --- |
| `src/enterprise_business_agent/llm/*` | Anthropic `stop_reason` 与 GLM `finish_reason` 透出；截断与格式错误区分；空响应、超时、连接/认证失败有界处理 |
| `src/enterprise_business_agent/evidence/report.py` | 报告叙述分段生成、逐段 Numeric Validator 校验、失败回退确定性文本并标注 |
| `src/enterprise_business_agent/agent/graph.py` | 最终结论截断检测与一次有界续写；失败显式进入结构化状态 |
| `src/enterprise_business_agent/observability/*` | Run/决策/模型/工具/DB/Checkpoint span；`run_id`/`tool_call_id` 关联；仅 `SUCCESS` 计入导出 |
| `scripts/preflight.py` | 只读上线前检查；连接串脱敏；JSON 与退出码 |
| `docs/PHASE6_FINAL_ACCEPTANCE.md` | Phase 6 收尾验收、测试矩阵、真实 GLM 未复测声明、残留风险 |
| `README.md` | 状态、能力、限制与文档链接更新 |

---

## 3. 第二阶段交付

| 文件 | 变更 |
| --- | --- |
| `scripts/build_phase7_corpus.py` | 生成 66 条 Phase 7 用例；canonical 查询实测值；强制 `human_check` |
| `tests/evals/phase7_cases.jsonl` | 66 条语料；与基础 26 条合计 92 条，题目/ID 唯一 |
| `src/enterprise_business_agent/agent/evaluation.py` | 多维评分、计划替代、禁用工具、角色、拒绝、数字匹配、报告结构/引用/Numeric Validator 维度 |
| `scripts/run_phase7_evals.py` | 语料检查、真实模型显式开关、停止策略、JSON/Markdown 报告、成本块 |
| `tests/test_phase7_evals.py` | 语料、真值、评分器、越权、注入、汇总、门禁测试 |
| `src/enterprise_business_agent/api/run_metrics.py` | `cost`、`performance`、`model.calls_per_run`、分域执行时长、超时、重试口径 |
| `src/enterprise_business_agent/config.py` | 模型单价、价格来源、币种配置；未配置时不做估算 |
| `tests/test_phase6_metrics.py` | 成本估算、延迟、工具耗时、分域、超时与重试口径测试 |
| `pyproject.toml` | `unit`/`integration`/`postgres`/`live_model` 标记；默认排除 `live_model` |
| `tests/test_*postgres*.py` | 增加 `postgres` 标记 |
| `docs/phase7/PHASE7_EVALUATION_PLAN.md` | 评测目标、语料、真值、维度、门禁、失败原则 |
| `docs/phase7/PHASE7_EVALUATION_REPORT.md` | 本轮无真实模型结果；语料/评分器证据与限制 |
| `docs/phase7/PHASE7_OBSERVABILITY.md` | metrics 字段口径、OTel span、诚实状态与限制 |
| `docs/PRODUCTION_READINESS.md` | 工程就绪评估、Go/No-Go、人工事项与残留风险 |

---

## 4. 实际测试命令与结果

| 命令 | 结果 | 性质 |
| --- | --- | --- |
| `.venv/bin/python -m pytest -q` | **503 passed, 25 skipped in 41.71s** | 全量 SQLite/离线；默认排除 `live_model` |
| `pytest tests/test_phase7_evals.py -q` | **10 passed** | Phase 7 语料与评分器 |
| `pytest tests/test_phase6_runtime.py ... test_phase6_cutover.py -q` | **49 passed, 2 skipped** | Phase 6 SQLite 定向 |
| `pytest tests/test_evidence_ledger.py test_findings.py test_numeric_validator.py test_phase6_approvals.py test_phase6_workbench.py -q` | **67 passed** | Evidence/Finding/Report/Numeric Validator/审批一致性 |
| `pytest tests/test_llm_client.py test_glm_client.py test_agent_runtime.py -q` | **56 passed** | 截断、空响应、格式、超时、连接/认证、非法工具、预算 |
| `pytest tests/test_otel_setup.py test_observability_spans.py -q` | **12 passed** | OTel 开/关、导出计数、span 属性 |
| `pytest tests/test_demo_results.py -q` | **6 passed** | 五业务域确定性演示 |
| PostgreSQL 5 个模块分别执行 | **7 + 4 + 5 + 7 + 2 = 25 passed** | 一次性 PostgreSQL 16 容器；非 Mock/SQLite 替代 |
| 多进程 Claim/Lease 两项 | **2 passed** | 两个独立进程仅一个 Claim 成功；过期租约接管与旧 worker 被拒 |
| `python scripts/run_phase7_evals.py --check-only` | **92 cases，OK，no model call was made** | 语料门禁 |
| `pytest -m live_model -q` | **528 deselected** | 证明没有测试被标记为真实模型 |
| `pytest -m postgres -q`（单文件顺序执行） | 见上 25 passed | 共享数据库模块需按文件隔离执行；合并执行会因各模块 `drop_all` 互相干扰 |
| Preflight（PG + 本地 API） | **20 ok, 6 warn, 0 fail** | 只读，未打印连接串 |

25 个 skipped 均为环境未提供的可选测试（PostgreSQL、可选后端等），不计为通过。

---

## 5. 五场景与一致性

五场景确定性回归：

- `tests/test_demo_results.py` → 6 passed。
- 覆盖销售、营销、财务、供应链、人力及跨指标调查。
- 数字来自本地确定性与数据库计算，不是模型生成。

真实 GLM 五场景：

- **本轮未执行。** 上一轮记录见 `docs/PHASE6_FINAL_ACCEPTANCE.md` 第 4.2 节。
- 未执行原因：没有获得当前 Key 状态与预算的人工确认。
- 残留风险：供应链/跨域场景的叙述截断修复目前只有确定性测试证据，真实模型下需复测。

一致性：

- Evidence：引用 ID 必须存在于 ledger。
- Finding：每条 Finding 必须引用证据；结论引用必须存在。
- Report：结构块完整度按适用块计算；`recommendations` 仅在可行动时必填。
- Numeric Validator：问题、未验证数字、通过状态均从存储报告读取。
- 审批：报告版本/指纹绑定，状态机与权限隔离测试通过。

---

## 6. 异常、预算与 OTel

| 项目 | 结果 |
| --- | --- |
| 截断（Anthropic `max_tokens` / GLM `length`） | 结构化 `LLMTruncated`，不混同格式错误 |
| 空响应 | 有界重试；耗尽后结构化失败 |
| 格式错误 | `LLMInvalidResponse`，不重试成无限循环 |
| 超时/连接/认证失败 | 结构化状态；不伪装成功 |
| 非法工具名/参数 | 校验错误回灌模型；达到上限后停止 |
| 多工具调用 | 只执行第一个并告警 |
| 最终结论截断 | 最多续写一次并拼接 |
| 预算耗尽 | `budget_exhausted` / 生命周期预算测试通过 |
| 叙述截断 | 分段生成 + 逐段校验 + 确定性回退 |
| OTel 关闭 | 业务行为不变，状态如实报告未创建 span |
| OTel 开启（本地 provider） | span 生成/父子关系/属性/成功失败计数通过 |
| 真实 OTLP 导出 | **未验证** |

---

## 7. Phase 7 评测状态

- 语料：92 条（基础 26 + Phase 7 66），唯一，域/题型覆盖满足要求。
- 真值：60 条带 `query_check`，逐条重放全部匹配；6 条安全/歧义/范围外用例不依赖数字真值。
- 评分器：脚本化 planner 下所有维度按预期判分；越权被识别为策略拦截；注入不调用禁用工具。
- 真实模型：**本轮 0 次调用**。没有通过率、延迟、token 或成本数字。
- 运行器在无 `--allow-live-model` 时打印 `real LLM calls: 0` 并退出码 2。

---

## 8. 未执行项、原因与残留风险

| 未执行项 | 原因 | 残留风险 | 后续动作 |
| --- | --- | --- | --- |
| 真实 GLM Phase 7 全量评测 | Key 状态/预算未人工确认 | 模型质量、截断恢复、延迟与成本未知 | 人工确认后执行并归档 `phase7_eval.json` |
| 真实 GLM 五场景复测 | 同上 | 供应链/跨域叙述可能仍截断 | 同上 |
| 真实 OTLP 后端导出 | 本机无 Collector/厂商后端 | 外部 trace 链路未知 | SRE 接入后观察 `exported_spans` 增长 |
| 生产部署/角色创建 | 安全约束禁止自动执行 | 生产参数、TLS、网络未验证 | 按 `docs/PRODUCTION_DEPLOYMENT.md` 人工执行 |
| 容量/压测/灾备 | 本轮范围外 | 并发上限与故障域未量化 | 预发布阶段补充 |
| 工具内部重试计数 | 未单独持久化 | metrics `retries.tool=null` | 需要时增加事件或列 |
| 多模型稳定性对比 | 未授权多模型 | 结论只适用当前模型 | 单独评测 |

---

## 9. 安全约束遵守声明

本轮：

- 未连接或修改真实生产服务器。
- 未自动修改生产 `.env` 或切换数据库。
- 未删除、覆盖或迁移现有 SQLite 数据；所有测试使用临时文件/内存/一次性容器。
- 未创建或修改生产角色；PostgreSQL 角色仅存在于本地一次性容器。
- 未打印、复制或提交数据库密码、API Key、Token 或完整连接串；脚本输出已脱敏。
- 未自动轮换 GLM API Key；密钥轮换仍为独立人工上线事项。
- 保留现有本地开发环境与配置。

---

## 10. 最终回答

1. **是否完成三阶段？** 代码、测试与文档层面完成；真实模型与生产部署明确未执行。
2. **是否调用真实 GLM？** 否，0 次。没有新的真实模型结论。
3. **脚本化结果是否与真实结果分开？** 是。`fake-offline-plan-llm` 仅用于验证评分器，不进入模型质量结论。
4. **未执行测试是否说明原因？** 是，见第 8 节；未执行项不计入通过率。
5. **是否隐藏失败或跳过测试？** 否。失败已修复；跳过项保留并标注原因。
6. **架构是否保持？** 是。单 Agent + LangGraph + Function Calling + Tool Registry；未引入 Kafka、Kubernetes、微服务或新 Agent。
7. **能否上线？** 进入受控预发布可以；生产切流需先完成第 8 节人工事项、预发布真实模型评测和 `preflight` 零 FAIL。
