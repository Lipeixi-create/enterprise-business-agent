# Phase 7.1 完成报告

项目：`enterprise_business_agent`
版本：`0.6.1-dev`
日期：2026-10-31
范围：真实模型预发布验证准备（不重写架构、不重复已交付功能）

---

## 1. 交付概览

| 交付物 | 位置 |
| --- | --- |
| 运行前预检与首轮子集定义 | `src/enterprise_business_agent/agent/live_readiness.py`（新增） |
| 评测器补强 | `src/enterprise_business_agent/agent/evaluation.py` |
| 评测运行器补强 | `scripts/run_phase7_evals.py` |
| 语料构建校验 | `scripts/build_phase7_corpus.py` |
| 边界测试（评分器） | `tests/test_phase7_evals.py`（10 → 21 条） |
| 边界测试（预检/运行器） | `tests/test_phase7_1_preflight.py`（新增 17 条） |
| 可观测性测试 | `tests/test_observability_spans.py`（新增 2 条） |
| 预发布就绪说明 | `docs/phase7/PHASE7_LIVE_READINESS.md`（新增） |
| 本报告 | `docs/phase7/PHASE7_1_COMPLETION_REPORT.md`（新增） |
| 方案与报告更新 | `docs/phase7/PHASE7_EVALUATION_PLAN.md`、`PHASE7_EVALUATION_REPORT.md` |

---

## 2. 项目现状盘点

按"以代码与测试为准"核对，Phase 6/7 与本次相关的功能状态如下：

| 功能 | 盘点结论 | 本次处理 |
| --- | --- | --- |
| 92 条语料 + canonical 真值 | 已实现且测试通过 | 保持不变，仅新增 `require_order` 字段 |
| 多维度评分器 | 已实现但缺少边界测试 | 补齐 11 条针对性测试 |
| 数字准确率 | **部分实现**：任意位置出现即通过，存在同值巧合 | 改为"指标所在句"匹配，标题可借用 |
| 多指标同时出现时识别目标指标 | **尚未实现** | 已实现：按句归属，张冠李戴判失败 |
| 工具调用顺序 | **尚未实现** | 已实现：`require_order`，仅对措辞固定顺序的用例计分 |
| 重复工具调用统计 | **尚未实现** | 已实现：`repeated_tool_calls` / `extra_tool_calls`（诊断，不参与判定） |
| 回答数字溯源 | **尚未实现** | 已实现：`unsourced_numbers`（诊断，不参与判定） |
| 回答引用有效性 | **部分实现**：只在报告层校验 | 补齐到回答层，并作为通过条件 |
| 叙述完整性 | **部分实现**：只统计，不影响 `passed` | 已改为通过条件（拒绝类用例豁免） |
| 非数值 canonical 的 `expect_number` | **文档与代码不一致**：`equals="volatile"` 仍期望数字，用例永远无法通过 | 修正语料、构建期强校验、评分守卫 |
| 越权 / 缺数 / 注入 / 预测分类 | 已实现且测试通过 | 保持，新增"拒绝文本 + 违规调用"反例测试 |
| 运行前预检 | **部分实现**：散落在 `main()` 内 | 抽出 `live_readiness.py`，12 项检查，可单测 |
| 调用预算与时间上限 | **部分实现**：只有状态级停止 | 新增 `--max-model-calls` / `--max-seconds`，每例前检查 |
| 未执行用例统计 | **文档声称存在，代码缺失** | 已实现：`run.not_executed_cases`，不进分母 |
| 归档模型配置标识 | **尚未实现** | 已实现：`config` 块，无密钥 |
| 评测器异常处理 | **尚未实现**：异常直接抛出 | 已实现：记录 `eval_error` 后停止 |
| OTel span 与进程内导出计数 | 已实现但缺少时序属性测试 | 补 2 条：LLM 延迟/token、工具耗时的有无 |
| 工具重试次数 | 未持久化 | 保持 `null`，不推断、不编造 |
| PostgreSQL/生产环境 | 前序阶段已验证 | 本轮未接触 |

结论：不需要重复开发已完成功能；本轮只补"判定强度"和"运行安全"两块。

---

## 3. 代码变更

### 3.1 新增 `agent/live_readiness.py`

- `FIRST_ROUND_IDS`：固定的首轮 20 条用例。
- `first_round_coverage(cases)`：报告缺失的 ID / 域 / 题型。
- `config_snapshot(settings, cases, preset=...)`：归档用配置快照，含
  Provider、Model、Base URL host、Prompt 版本、`api_key_configured`（布尔）、
  选中用例 ID、LLM/Agent 限额、成本价格来源；**不含任何密钥值或前缀**。
- `live_preflight(...)`：12 项检查，返回 `PASS`/`WARN`/`FAIL` 列表与 `blocking`。
  预算为 `FAIL` 当且仅当 `<= 0`、小于用例数或超过硬上限 2000。
  *（Phase 7.2 更正：实际为 13 项恒定检查 + 1 项 `--preset first-round-20` 条件检查；
  当时的"12 项"漏数了条件项，且 `counts()` 未输出总数。见附录 A。）*

### 3.2 `agent/evaluation.py`

- `LiveEvalCase.require_order`；`LiveEvalResult` 新增
  `plan_order_correct`、`extra_tool_calls`、`repeated_tool_calls`、
  `failure_reasons`、`answer_number_provenance`、`unsourced_numbers`、
  `cited_evidence_ids`、`answer_citations_valid`、`llm_model`。
- `PlanMatch` + `_match_alternative` / `_best_plan`：一次匹配只能被使用一次，
  重复调用计入 `extra_calls` 与 `repeated_calls`；顺序由匹配到的下标是否递增决定。
- `_terms_apply`：数字必须与指标词同句；相邻句只有在它是标题（不含其他数字，或以
  `：` 结尾）时才可借用指标词。
- `_number_matches`：单位（万/亿/千）、百分比、相对容差 0.5%。
- `_numeric()` 守卫：canonical 结果不是数字时 `number_accuracy=None`。
- `answer_citations_valid`：回答中的 `[E#]` / evidence id 必须属于本次证据集合。
- `passed` 公式：新增 `plan_order` 与 `answer_citations_valid`，叙述完整性成为
  通过条件（期望拒绝的用例豁免），数字错误仍一票否决。
- `_FAILURE_FIELDS` / `_failure_reasons`：单条记录与汇总共用同一套失败标签。

### 3.3 `scripts/run_phase7_evals.py`

- 新参数：`--preset first-round-20`、`--max-model-calls`、`--max-seconds`、
  `--preflight-only`。
- 门禁顺序：语料 → 选例 → 预检 → 授权（退出码 2）→ 预算（退出码 3）→ 阻塞项
  （退出码 1）→ 构建客户端。
- `_execute_cases()`：逐条执行；预算、wall time、`STOP_STATUSES`、评测器异常
  四种情况都停止并记录原因；返回未执行 ID 列表。
- 归档新增 `run` 与 `config` 块；`summary.corpus` 与 `run` 指向同一份数据。
- `check_corpus` 新增：非数值 expectation 必须 `expect_number=false`；
  `require_order` 至少需要两个调用。

### 3.4 语料

- `tests/evals/phase7_cases.jsonl`：重新生成，唯一差异是 `phase7-064/065/066`
  增加 `require_order: true`；所有实测真值不变（diff 已核对）。
- `tests/evals/live_agent_cases.jsonl`：`live-014`、`live-015` 增加
  `expect_number: false`（其 canonical 结果是方向字符串 `volatile`）。

### 3.5 `observability/spans.py`

- `tool.duration_ms` 只在事件确实带有耗时属性时写入；缺失时保持"不存在"，
  而不是写入 `null` 或推断值。补充 2 条测试覆盖 LLM 延迟/token 属性与工具耗时。

---

## 4. 验证结果

全部为**真实执行**的离线命令，未调用任何模型。

| 顺序 | 命令 | 结果 |
| --- | --- | --- |
| 1 | `.venv/bin/python scripts/run_phase7_evals.py --check-only` | `corpus: 92 cases`；`corpus check: OK`；退出码 0 |
| 1 | `.venv/bin/python scripts/run_phase7_evals.py --preflight-only --preset first-round-20` | `9 ok, 1 warn, 3 fail`（未授权/未确认预算/未配置 Key），退出码 1，`real LLM calls: 0` |
| 2 | `pytest tests/test_phase7_evals.py -q` | `21 passed in 2.42s` |
| 3 | `pytest tests/test_phase7_1_preflight.py -q` | `17 passed in 2.97s` |
| 4 | `pytest -q`（全量，默认排除 `live_model`） | `533 passed, 25 skipped in 44.64s` |
| 5 | `pytest tests/test_phase6_*.py tests/test_phase5_agent_integration.py tests/test_agent_runtime.py tests/test_agent_security_boundaries.py -q` | `93 passed, 14 skipped in 21.67s` |
| 5 | `.venv/bin/python scripts/run_agent_evals.py` | `6/6 passed`（脚本化 planner，明确标注非真实模型） |
| 7 | `EBA_OTEL_ENABLED=1 pytest tests/test_observability_spans.py tests/test_otel_setup.py -q` | `14 passed in 0.24s` |
| 7 | `EBA_OTEL_ENABLED=0` 同上 | `14 passed in 0.26s` |

计数口径：

- PASS：533（全量）。
- SKIP：25，全部是 PostgreSQL / 可选后端相关（未设 `EBA_TEST_POSTGRES_URL` 等），
  **未伪装成通过**。
- FAIL：0。
- WARN：1（`cost.prices` 未配置单价，按设计只告警）。

退出码验证（由测试固化）：无授权 → 2；无预算 → 3；无 Key / 超硬上限 / 预检阻塞 → 1，
且都不会创建输出文件。

---

## 5. 四种状态声明

| 状态 | 本报告的主张 |
| --- | --- |
| 代码已实现 | 第 3 节所列全部变更已写入仓库 |
| 离线测试已通过 | 第 4 节命令与计数为真实执行结果 |
| 真实模型已验证 | **未验证**。本轮调用次数为 0，所有真实指标为 `null` |
| 生产环境已验证 | **未验证**。未接触生产数据库、角色、`.env` 或外部服务 |

不得用"离线测试通过"推断"真实模型可用"。真实模型评测必须在人工确认 Provider、
Key 状态与预算后，按 `PHASE7_LIVE_READINESS.md` 第 9 节的命令单独执行并归档。

---

## 6. 未解决问题与残留风险

1. 真实模型的首轮 20 条尚未执行，通过率、延迟、token、成本全部未知。
2. 数字溯源只作诊断：派生比例不会出现在工具返回值中，因此不设为通过门槛。
3. 语料绑定确定性数据集；数据或语义层变更后必须重建（构建脚本会重新测量真值）。
4. 工具内部重试次数未持久化，`performance.retries.tool` 固定为 `null`。
5. 可观测性只到进程内 span，未接入真实 OTLP Collector。
6. Provider 限流、余额与配额不在仓库检查范围内；预算只限制本地调用次数。
7. `--max-model-calls` 对单条用例内部的重试不做硬切分，因为一次 `run_live_agent_case`
   是原子的；用 `agent_max_tool_calls` 与 `llm_max_retries` 约束上界。

---

## 7. 下一步

1. 人工确认 `PHASE7_LIVE_READINESS.md` 第 8 节清单。
2. 执行首轮 20 条（`--preset first-round-20 --max-model-calls 200`）。
3. 按第 6 节验收口径判定；不通过则按"工具/参数 → 数据 → Evidence → 安全 → 报告"
   分类修复并回归。
4. 首轮达标后执行全量 92 条，并用 `--previous` 记录首轮对比。
5. 用真实生成的结果更新 `PHASE7_EVALUATION_REPORT.live.md`；不得手填估计值。

---

## 附录 A：Phase 7.2 增量（2026-10-31）

本节记录在原报告之后追加的改动；上文保持原样，作为 Phase 7.1 的历史记录。

### A.1 三个 FAIL 的根因

| 检查项 | 根因 | 性质 | 处理 |
| --- | --- | --- | --- |
| `authorization.live_model` | 复现命令未带 `--allow-live-model` | 人工授权关口，按设计 | 保留 FAIL，标记 `operator_gate=True`，汇总中单列 |
| `authorization.budget` | 复现命令未带 `--max-model-calls` | 人工预算关口，按设计 | 同上 |
| `provider.api_key` | 运行器 `get_settings()` 不加载 `.env`；按默认 `anthropic` 找 Key，而 `.env` 是 `glm` + `GLM_API_KEY` | **真实缺陷：配置发现** | 新增 `--load-dotenv`；新增 `config.dotenv` 检查 |

### A.2 统计口径

- 根因：恒定检查 12 项 + `--preset first-round-20` 条件项 1 项 = 13 项，文档写"12 项"；
  `counts()` 无 `total`，`9 ok / 1 warn / 3 fail` 无法自证等于检查项数。
- 修复：`counts()` 输出 `total`；新增 `config.dotenv`（恒定项，共 13 项恒定 / 14 项含
  条件项）；打印 `preflight: 14 checks, 9 ok, 2 warn, 3 fail`；测试断言
  `total == len(checks)` 与三桶之和一致。
- 未删除、跳过或降低任何检查；`blocking` 与退出码语义不变。

### A.3 代码变更

| 文件 | 变更 |
| --- | --- |
| `src/enterprise_business_agent/config.py` | 新增 `llm_api_key_env_names()`、`dotenv_llm_declarations()`（只返回变量名，绝不返回值）；`_llm_api_key()` 复用前者；`get_settings(..., load_dotenv=False, dotenv_path=".env")` |
| `src/enterprise_business_agent/agent/live_readiness.py` | `LiveCheck.operator_gate`；`LiveReadiness.operator_gated` / `environment_blockers`；`counts()["total"]`；新增 `config.dotenv` 检查；`provider.api_key` 消息列出待检查变量名；`config_snapshot` 增加 `api_key_env_names`、`dotenv_loaded` |
| `scripts/run_phase7_evals.py` | 新增 `--load-dotenv`；打印 dotenv 加载状态；汇总打印 `N checks` 与关口/阻塞项分组；`run`/`config` 归档 `dotenv_loaded`；报告表格新增 Preflight 行 |
| `tests/test_phase7_1_preflight.py` | 17 → 28 条测试 |

### A.4 验证结果（真实执行，离线）

| 顺序 | 命令 | 结果 |
| --- | --- | --- |
| 1 | `pytest tests/test_phase7_1_preflight.py -q` | `28 passed` |
| 2 | `run_phase7_evals.py --check-only` | `corpus: 92 cases`；`OK`；退出码 0 |
| 3 | `pytest tests/test_phase7_evals.py -q` | `21 passed in 2.49s` |
| 4 | `scripts/run_agent_evals.py`（脚本化 planner） | `6/6 passed` |
| 5 | `pytest tests/test_phase6_*.py tests/test_phase5_agent_integration.py tests/test_agent_runtime.py tests/test_agent_security_boundaries.py -q` | `93 passed, 14 skipped in 22.59s` |
| 6 | `pytest -q`（全量，默认排除 `live_model`） | `544 passed, 25 skipped` |
| 7 | `EBA_OTEL_ENABLED=1 pytest tests/test_observability_spans.py tests/test_otel_setup.py -q` | `14 passed` |
| 7 | `EBA_OTEL_ENABLED=0` 同上 | `14 passed` |
| — | `run_phase7_evals.py --preflight-only --preset first-round-20` | `14 checks, 9 ok, 2 warn, 3 fail`，`real LLM calls: 0`，退出码 1 |
| — | `run_phase7_evals.py --preflight-only --max-model-calls 200 --preset first-round-20` | `14 checks, 10 ok, 2 warn, 2 fail`，`real LLM calls: 0`，退出码 1 |

### A.5 状态声明（Phase 7.2 时点）

| 状态 | 主张 |
| --- | --- |
| 代码已实现 | 是（A.3） |
| 离线测试已通过 | 是（A.4） |
| 真实模型已验证 | Phase 7.2 时点：**否**（0 次调用）；当日稍后首轮已执行，见 A.6 |
| 生产环境已验证 | **否**；`.env`、数据库、角色均未修改 |

### A.6 首轮真实模型评测（2026-10-31，人工授权后）

| 项目 | 实际值 |
| --- | --- |
| Provider / 模型 | `glm` / `glm-5.3-flash` |
| 授权方式 | `--load-dotenv --allow-live-model --max-model-calls 200` |
| Run Store | 本地 SQLite（`RUN_STORE_URL=sqlite:///./enterprise_business_agent_runs.db`） |
| 选中 / 执行 / 通过 / 失败 / 未执行 | 20 / 11 / 8 / 3 / 9 |
| 模型调用 | 28（上限 200，未触顶，拒绝 0 次） |
| 停止原因 | `phase7-040:llm_timeout` |
| 归档 | `data/exports/phase7_eval.first_round.json`、`docs/phase7/PHASE7_EVALUATION_REPORT.first_round.md` |

结果分类见评测报告 §1.3。本轮**未达到** `PHASE7_LIVE_READINESS.md` §6 的继续条件：
执行不完整、`stopped_reason` 非 null、安全类用例全部未执行。不得据此声称全量或生产可用。


---

## 附录 B：Phase 7.3 增量（2026-10-31）

本节记录首轮诊断后的修复；上文与附录 A 保持原样。**本轮没有调用任何真实模型**，
首轮归档文件未改动。

### B.1 修复清单

| 编号 | 问题（首轮证据） | 修复 |
| --- | --- | --- |
| P0-1 | 040 在 60 s 单例上限下 `llm_timeout`，批次中断 | 运行器新增 `--case-max-seconds`；运行期在已有工具证据且剩余时间不足时停止；超时/预算停止时生成有界部分结论，状态不变 |
| P0-2 | 029 的 `0.006162` 是两条 Evidence 之差，被数值校验器判为未溯源 | 运行期按 Evidence 原始数字做确定性四则运算验证（相对误差 1e-6） |
| P0-3 | 034 的表格数字挂在表头指标下，被误判为张冠李戴 | 评测器按 Markdown 表头列归属指标词 |
| P1-1 | 提示词对"各/每个"与 `top_n` 的表述冲突 | 明确"各"只用一次 `query_metric + dimensions`；`top_n` 仅用于明确排名；禁止重复查询同指标同维度同周期 |
| P1-2 | 040 问题未限定"2025 年 12 月"，模型取 Q4 窗口 | 问题改为明确要求 12 月库存天数；canonical 与断言不变，Q4 反例仍判失败 |
| P1-3 | evidence/call ID 中的数字被当作业务数字 | 抽取数字前屏蔽标识符片段 |
| P1-4 | 趋势 Finding 拼接内部规则文本（含 5% 阈值） | 用户可见语句不再包含规则；规则保留在审计 Evidence |
| P2-1 | 只有 `llm_timeout` 触发批次停止 | `timeout` 同样触发 |
| P2-3 | 归档缺少决策/调用轨迹 | 每条记录增加 `decisions`、`llm_calls`、`tool_call_trace` |

### B.2 代码变更

| 文件 | 变更 |
| --- | --- |
| `src/enterprise_business_agent/agent/evaluation.py` | 标识符屏蔽、表格列归属、`LiveEvalResult` 轨迹字段、`_CountingLLM` 调用事件、`run_live_agent_case(max_seconds=...)` |
| `src/enterprise_business_agent/agent/graph.py` | 派生数字验证、剩余时间检查、部分结论生成、提示词规则 11/12/15、Prompt 版本 `phase5-2026-10-31.2` |
| `src/enterprise_business_agent/evidence/findings.py` | 趋势 Finding 不再外泄内部规则文本 |
| `scripts/run_phase7_evals.py` | `--case-max-seconds`、`timeout` 停机、归档字段 |
| `scripts/build_phase7_corpus.py`、`tests/evals/phase7_cases.jsonl` | 040 问题文本（仅问题，canonical 未变） |
| `tests/test_phase7_evals.py` | 21 → 30 条（新增 9：表头列归属、标识符屏蔽、派生数字、趋势规则、040 参数、轨迹归档、超时判定、提示词规则等） |
| `tests/test_phase7_1_preflight.py` | 28 → 32 条（新增 4：两条超时停机参数化、预算越界、预算透传到预检） |
| `tests/test_agent_runtime.py` | 28 → 30 条（新增 2；更新超时测试以断言部分结论） |

### B.3 验证结果（真实执行，离线；无模型调用）

| 命令 | 结果 |
| --- | --- |
| `pytest -q` | `559 passed, 25 skipped` |
| `EBA_OTEL_ENABLED=1 pytest -q` | `559 passed, 25 skipped` |
| `pytest tests/test_phase7_evals.py tests/test_phase7_1_preflight.py tests/test_agent_runtime.py -q` | `92 passed` |
| `run_phase7_evals.py --check-only` | `corpus: 92 cases`；`OK`；退出码 0 |
| `run_phase7_evals.py --preflight-only --case-max-seconds 120` | `13 checks`；`real LLM calls: 0`；未生成输出文件 |

### B.4 状态声明（Phase 7.3 时点）

| 状态 | 主张 |
| --- | --- |
| 代码已实现 | 是（B.1、B.2） |
| 离线测试已通过 | 是（B.3） |
| 真实模型已验证 | **否**（本轮 0 次调用；首轮结果仍只覆盖旧的 Prompt 版本 11 条） |
| 生产环境已验证 | **否**；`.env`、API Key、数据库凭证均未修改 |

重新执行真实模型评测需要单独授权；下一轮结果必须按 `phase5-2026-10-31.2`
另行归档，不得与首轮合并计算。
