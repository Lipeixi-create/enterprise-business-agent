# Phase 7.9 完成报告（真实模型验收与评测器修复）

日期：2026-11-02

范围：`enterprise_business_agent` Phase 7.9.1–7.9.5。保留 LangGraph 单 Agent 循环、8 个工具、语义层、SQL 安全、权限、Evidence 与人工审批架构；未进入 Phase 8，未改业务原始数据、标准答案或容差。

---

## 1. 结论摘要

| 指标 | 结果 |
| --- | --- |
| 唯一真实执行用例 | **29**（26 个 `live-*` + 3 个修复用例 `phase7-026/039/054`） |
| 最新结果通过 | **26 / 29（89.7%）** |
| 最新结果失败 | **3**：`live-004`、`live-005`（模型请求超时），`live-023`（真实内容错误） |
| `live-*` 最新结果 | **23 / 26** 通过；计分用例 **22 / 24** 通过 |
| 修复用例 | **3 / 3** 通过 |
| 真实模型调用（所有尝试，含重跑） | **111** 次 |
| 最新结果口径调用 | **70** 次（`live-*` 63 + 修复用例 7） |
| 超时记录 | **6** 条（`live-004` 2 条、`live-005` 3 条、`live-023` 1 条） |
| 离线重放通过 | `live-*` **23 / 26**；与运行当时一致 |
| 92 条语料 check-only | `corpus: 92 cases` / `corpus check: OK (no model call was made)` |
| 定向测试 | **210 passed** |
| 全量 pytest | **684 passed, 25 skipped** |
| 编译检查 | `compileall` 通过 |
| 成本 | **null / 未估算**：模型单价未配置（`price_source=unconfigured`） |

**明确区分：** 上述“最新结果通过”是真实模型归档的判定；离线重放只是用当前评测器重算同一批归档，不是新的模型调用，也不增加真实通过数。`live-026` 为设计上的不计分“无证据/拒答”用例，通过但不等同于一次数据查询成功。

---

## 2. 文件变更与各阶段修改

### 2.1 核心代码

| 文件 | 阶段 | 修改 |
| --- | --- | --- |
| `src/enterprise_business_agent/agent/graph.py` | 7.9.1 / 7.9.2 / 7.9.4 | 扩充决策提示词约束（未知指标不得替换、排名/趋势/异常工具选择、结果反馈与未决问题）；模型超时后追加“优先基于已有结果简短作答、最多再调一个必要工具”的恢复提示；新增 `failure_category` 区分模型超时、模型请求预算、用例截止时间、工具超时、工具失败与预算耗尽；最终答案纠错输出被截断时不再覆盖完整原答案；底层 provider 已耗尽全部重试时不再进行第二次整体重试，避免重复请求与重复计费风险。 |
| `src/enterprise_business_agent/agent/graph.py` | 7.9.3 | `_matches_derived` 接受 0–6 位小数的四舍五入派生值（如 `8.142` 匹配 `8.141989`），仍拒绝未四舍五入的相邻值。 |
| `src/enterprise_business_agent/agent/evaluation.py` | 7.9.3 | Evidence 链支持同一 Run 内工具消息的 call-local Evidence ID 归一化；数值核验支持“相对变化率”等后置比率条款继承指标；派生列表（如“降幅贡献”）在表头未重复指标名时，可从引入该表的叙述继承指标；带日期的 Markdown 粗体标题仍可为下方列表提供指标上下文。 |
| `src/enterprise_business_agent/agent/relations.py` | 7.9.2 / 7.9.4 | `_field_for` 优先选择邻近的指标/标签而不是通用列提示；`_check_rank` 优先识别序数后紧跟的成员；`_check_superlative` 对带符号的贡献/变化按绝对值比较；排名正则排除“第四季度”等时间表达；否定表达允许短修饰语间隔（如“不能称为并列最高”）。 |
| `src/enterprise_business_agent/agent/tools.py` | 7.9.2 | 拒绝 `yoy_year` 与 `quarter` 同时出现；`query_metric.grain` 默认月度；工具描述引导排名用 `top_n`、趋势用 `trend`、异常用 `anomaly_scan`。 |
| `src/enterprise_business_agent/agent/contracts.py` | 7.9.4 | `AgentStateSnapshot` 增加 `failure_category`。 |
| `src/enterprise_business_agent/evidence/validator.py` | 7.9.3 | 校验 Evidence/Finding 与报告 Run、Case、Tool Call 的归属，拒绝跨 Run、跨 Case 或不存在的引用。 |

### 2.2 测试

| 文件 | 新增/调整 |
| --- | --- |
| `tests/test_agent_runtime.py` | 超时恢复提示、`failure_category` 分类、未知指标不得替换、截断纠错保留原答案、provider 重试耗尽后不再整体重试。 |
| `tests/test_phase7_evals.py` | 比率条款继承指标、三位小数派生值、派生表继承叙述指标、带日期标题下的指标上下文、Evidence 链归属。 |
| `tests/test_phase7_6_evaluator.py` | 最高值使用命名指标而非辅助列、序数后紧跟成员、降幅贡献按绝对值比较、“第四季度”不是排名、否定并列最高不是最大值断言。 |
| `tests/test_numeric_validator.py` | 报告/Evidence/Finding Run 归属与引用完整性。 |
| `tests/test_live_agent_evals.py` | 真实评测记录字段与失败分类。 |

---

## 3. 修复前后对比

### 3.1 指定真实内容错误

| 用例 | 修复前 | 修复后 | 结果 |
| --- | --- | --- | --- |
| `phase7-026` | 旧评测器存在假通过/关系误判历史 | 增加确定性关系校验与数字溯源回归 | 真实重跑 **通过**（2 次调用） |
| `phase7-039` | 同比参数选择可能混用季度/年度 | `yoy_year` 与 `quarter` 互斥校验 | 真实重跑 **通过**（2 次调用） |
| `phase7-054` | 未知产品未明确给出空答案 | 无数据时生成显式“无法回答”，不编造数值 | 真实重跑 **通过**（3 次调用，`no_data`） |

### 3.2 真实运行暴露并修复的误判

| 用例 | 现象 | 根因 | 修复后真实结果 |
| --- | --- | --- | --- |
| `live-008` | 数字准确率失败 | 后置“相对变化率”条款未继承指标 | 通过 |
| `live-010` | ROI 最低被判错误 | 关系校验误用营销投入分母 | 通过 |
| `live-011` | 派生差 `8.142` 被判无法核对 | 只接受 2/6 位小数 | 通过 |
| `live-012` | 表内精确值无法关联销售额 | 派生表头未继承叙述指标 | 通过 |
| `live-019` | 带日期标题下的转化率数字失败 | 标题含年份被当作“有数字，不能提供上下文” | 通过 |
| `live-024` | “第四季度”被当作“第 4 名” | 排名正则未排除时间单位 | 通过 |
| `live-025` | 截断纠错覆盖完整答案；否定最大被误判 | 纠错截断未拦截；“不能称为并列最高”未识别 | 通过 |

### 3.3 仍失败的真实用例

| 用例 | 最新状态 | 原因 | 处理 |
| --- | --- | --- | --- |
| `live-004` | `llm_timeout` | provider 连续超时；最新一次 5 次顶层调用、165.8s，失败分类 `model_timeout` | 如实记录，不判通过；已加入 provider 重试耗尽保护，避免再次重复整体重试 |
| `live-005` | `llm_timeout` | 先错误替换指标，提示修复后只调用 `list_metrics`，随后 provider 超时；180s 预算仍失败 | 如实记录；该用例不计分，但任务未完成 |
| `live-023` | `completed_with_warnings` | 答案把 SC 与 NC 合称“两者毛利率反而是最高的”，SC 实际不是最高；关系校验正确判定 `conclusion_overreach` | 属于真实内容错误，保留失败；未放宽判据 |

---

## 4. 真实模型执行明细（最新结果）

配置：provider `glm`，model `glm-5.3-flash`，prompt `phase5-2026-10-31.2`；显式使用 `--allow-live-model --load-dotenv`；单次模型超时 20s，provider `max_retries=2`，Agent 工具上限 6，单用例预算 120s（`live-004/005` 重试用 180s）。未输出任何 API Key、数据库密码或完整连接串。

### 4.1 `live-*` 最新结果

| 用例 | 结果 | 计分 | 状态 | 调用 | 壁钟 | Token 输入/输出 | 失败原因 |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| live-001 | 通过 | 计分 | completed | 2 | 29.3s | 14497/298 | - |
| live-002 | 通过 | 计分 | completed | 2 | 16.1s | 14575/299 | - |
| live-003 | 通过 | 计分 | completed | 2 | 16.8s | 14936/312 | - |
| live-004 | 失败 | 计分 | llm_timeout | 5 | 165.8s | 22891/1129 | task_completion |
| live-005 | 失败 | 不计分 | llm_timeout | 3 | 160.3s | 7102/291 | task_completion |
| live-006 | 通过 | 计分 | completed | 2 | 26.1s | 14653/372 | - |
| live-007 | 通过 | 计分 | completed | 2 | 53.7s | 14667/671 | - |
| live-008 | 通过 | 计分 | completed | 2 | 22.8s | 14655/448 | - |
| live-009 | 通过 | 计分 | completed | 2 | 43.9s | 14927/367 | - |
| live-010 | 通过 | 计分 | completed | 2 | 15.5s | 14720/311 | - |
| live-011 | 通过 | 计分 | completed | 2 | 17.7s | 14999/474 | - |
| live-012 | 通过 | 计分 | completed | 3 | 30.6s | 23050/763 | - |
| live-013 | 通过 | 计分 | completed | 3 | 36.2s | 23084/1288 | - |
| live-014 | 通过 | 计分 | completed | 3 | 84.6s | 15057/574 | - |
| live-015 | 通过 | 计分 | completed | 2 | 67.7s | 14959/986 | - |
| live-016 | 通过 | 计分 | completed | 2 | 23.0s | 14686/605 | - |
| live-017 | 通过 | 计分 | completed | 2 | 14.1s | 14851/409 | - |
| live-018 | 通过 | 计分 | completed | 2 | 17.4s | 14804/456 | - |
| live-019 | 通过 | 计分 | completed | 2 | 33.3s | 14758/476 | - |
| live-020 | 通过 | 计分 | completed | 2 | 17.3s | 15037/558 | - |
| live-021 | 通过 | 计分 | completed | 3 | 91.5s | 15666/970 | - |
| live-022 | 通过 | 计分 | completed | 2 | 24.9s | 14915/674 | - |
| live-023 | 失败 | 计分 | completed_with_warnings | 6 | 126.7s | 31969/1630 | conclusion_overreach |
| live-024 | 通过 | 计分 | completed | 2 | 15.2s | 14928/413 | - |
| live-025 | 通过 | 计分 | completed | 2 | 14.1s | 14877/389 | - |
| live-026 | 通过 | 不计分 | no_evidence | 1 | 15.9s | 7114/440 | - |

### 4.2 修复用例

| 用例 | 结果 | 状态 | 调用 | 壁钟 | Token 输入/输出 |
| --- | --- | --- | ---: | ---: | ---: |
| phase7-026 | 通过 | completed | 2 | 25.6s | 15147/714 |
| phase7-039 | 通过 | completed | 2 | 18.4s | 14580/371 |
| phase7-054 | 通过 | no_data | 3 | 93.9s | 14345/840 |

### 4.3 调用与超时

- 所有 Phase 7.9 归档（含重跑）：**111 次**模型调用、43 条记录、20 个归档文件。
- 最新结果口径：**70 次**调用（`live-*` 63 次，修复用例 7 次）。
- 超时记录 **6 条**：
  - `live-004`：2 条（首次 120s 预算内 `model_timeout_budget`；重试 180s 预算内 `model_timeout`）
  - `live-005`：3 条（两次 120s 预算内 `model_timeout_budget`；一次 180s 预算内 `model_timeout`）
  - `live-023`：1 条（首次 120s 预算内 `model_timeout`，第二次完成但内容错误）

---

## 5. 离线重放与真实运行分开统计

| 口径 | `live-*` | 修复用例 | 合计 |
| --- | ---: | ---: | ---: |
| 真实运行当时通过 | 23 / 26 | 3 / 3 | 26 / 29 |
| 当前评测器离线重放 | 23 / 26 | 3 / 3 | 26 / 29 |
| 差异 | 0 | 0 | 0 |

离线重放方法：用当前 `relations.py`、`evaluation.py` 对归档中的 `final_answer`、`tool_messages`、`observed`、`conclusion_terms` 重新计算关系断言、数字准确性与叙事完整性；工具选择、参数、查询、任务完成、证据充分性、Evidence 链结果沿用归档字段。重放不产生模型调用、Token 或成本。

**不能把这 23 条重放通过写成“新的真实模型通过”**；它们与最新真实归档的判定一致，仅用于确认当前评测器没有版本漂移。

---

## 6. 规划、工具选择与证据链指标（最新 `live-*`，26 条）

| 指标 | 结果 | 说明 |
| --- | ---: | --- |
| 工具选择正确 | **26 / 26** | 最新结果中无选择错误 |
| 工具参数正确 | **26 / 26** | 包括时间范围、维度、过滤条件 |
| 查询结果正确 | **24 / 24 适用** | `live-005`、`live-026` 不适用 |
| 任务完成 | **24 / 26** | `live-004`、`live-005` 未完成 |
| 证据充分 | **26 / 26** | 均有可用证据或设计性拒答 |
| Evidence 链有效 | **25 / 25 适用** | `live-026` 无证据，记录为 N/A |
| Evidence 链问题 | **0** | 无跨 Run / 跨 Case / 不存在引用 |
| 数字准确率 | **22 / 22 适用** | 4 条 N/A；0 条 false |
| 叙事完整 | **26 / 26** | 含超时用例的“未完成”说明 |
| 结论越界（关系错误） | **1** | `live-023` |
| 额外工具调用 | **4** | 未导致失败 |
| 重复工具调用 | **0** | - |
| 重复/被抑制调用 | **0** | - |
| Evidence 总数 | **30** | 最新 26 条记录 |

工具使用分布（最新 `live-*`）：`compare_periods` 8、`top_n` 7、`query_metric` 6、`contribution` 4、`trend` 2、`anomaly_scan` 2、`list_metrics` 1。未强制固定工具顺序；`plan_order_correct` 在 26 条中均为 `None`（用例未要求顺序），因此不把“顺序不同”判为失败。

---

## 7. Token、延迟与成本

| 口径 | 模型调用 | 输入 Token | 输出 Token | 合计 Token | 壁钟总时长 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 最新结果 29 条 | 70 | 456,449 | 17,528 | 473,977 | 1,318.4s |
| 所有尝试（含重跑） | 111 | 672,108 | 27,820 | 699,928 | 2,170.2s |

- 最新 `live-*` 26 条：LLM 延迟合计约 **734.7s**，壁钟合计约 **1,180.5s**。
- 修复用例 3 条：LLM 延迟合计约 **76.0s**，壁钟合计约 **137.9s**。
- **Token 缺失说明：** provider 超时的底层尝试没有返回 usage，归档只累计成功返回的调用。上述 Token 是已记录值，不是实际消耗上限；缺失部分不估算。
- **成本：** `cost.estimated=false`、`price_source=unconfigured`、输入/输出单价为 `null`，`estimated_total=null`。没有可计算成本，未做估算。

---

## 8. 剩余问题、风险与下一步建议

### 8.1 剩余问题

1. **`live-004` / `live-005` provider 超时**：单次模型超时 20s，provider 内部已重试 2 次；180s 用例预算仍无法完成。已增加“provider 重试耗尽后不再整体重试”的保护，避免重复请求与重复计费风险，但超时本身未能通过代码消除。
2. **`live-023` 真实内容错误**：模型把 SC 与 NC 合称“两者毛利率反而是最高的”，关系校验正确拦截。未修改标准答案或放宽容差。
3. **`live-026` 不计分**：按设计返回无证据/拒答，不能计入“数据查询真实通过”。
4. **成本不可计算**：未配置模型单价；所有成本字段保持 null。
5. **Phase 7 的 66 条 `phase7-*` 未在本阶段全量重跑**：本阶段只真实重跑 3 条指定修复用例；其余结论仍以历史归档和离线重放为准，不能表述为 Phase 7.9 的新真实通过。

### 8.2 风险

- provider 端 20s 墙钟超时对长上下文/多工具综合题不稳定，`live-004/005` 属于该类；提高用例总预算不会改变单次请求超时。
- 真实内容错误仍可能出现（如 `live-023` 的“并列最高”误述）；确定性关系校验能拦截已建模的排名/极值/方向/计数关系，但不能覆盖所有自然语言断言。
- 超时尝试可能已产生未返回 usage 的请求；Token 与成本统计不完整，报告不做补写。

### 8.3 下一步建议

1. 若继续处理 `live-004/005`，需显式授权调整 provider 单次超时或改用低延迟模型；不得静默修改 `.env`。
2. 为 `live-023` 类错误补充“多成员共享最高值”提示词约束与离线回归，但保留当前失败记录作为基线。
3. 如需 Phase 7 全量结论，另行授权重跑 66 条 `phase7-*`，并与本报告 26 条 `live-*` 分开统计。

---

## 9. 归档索引

新增归档与报告（未覆盖历史文件）：

- `data/exports/phase7_eval.phase7_9.fixed.json` / `docs/phase7/PHASE7_EVALUATION_REPORT.phase7_9.fixed.md`
- `data/exports/phase7_eval.phase7_9.live26.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live005_013.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live005_retry.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live006_013.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live008_012_retry.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live011_012_retry2.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live014_018.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live019_022.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live019_retry.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live023_026.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live023_retry.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live-004_retry.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live-005_retry.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live-024.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live-024_retry.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live-025.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live-025_retry.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live-025_retry2.json` / `.md`
- `data/exports/phase7_eval.phase7_9.live-026.json` / `.md`

所有真实调用均记录 case ID、模型、prompt 版本、调用次数、延迟、Token、工具轨迹与结果；未打印或归档任何 API Key、数据库密码或完整连接串。
