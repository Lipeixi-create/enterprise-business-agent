# Phase 7.5 实施报告：Agent 工具结果可见性与评测准确性优化

日期：2026-11-02
范围：离线开发、离线测试、离线评测
状态：**已完成离线实现与回归；未发起任何真实 LLM 调用，未启动第三轮 A 复测、B 批或 C 批。**

> 证据分级：本报告中所有“通过/失败”均来自 pytest 或 `--check-only` 的离线执行；涉及模型行为的部分使用 scripted TEST FAKE，只验证控制流与数据可见性，**不构成真实模型验收**。
> 密钥边界：未读取、打印或记录任何 API Key、数据库密码及其他凭证；未修改 `.env`。

## 0. 结论摘要

| 问题 | 结论 |
| --- | --- |
| 1. 正常工具结果是否让模型看到完整明细 | **是（离线验证）**。`query_metric` / `top_n` / `trend` / `anomaly_scan` / `compare_periods` / `contribution` / `metric` 描述 / 指标列表都会附带 `data` 视图，包含指标、维度、时间、值、来源表、Evidence ID、工具调用 ID 与查询条件。 |
| 2. 语义去重命中后能否复用完整结果 | **是（离线验证）**。拦截回传现在携带与原工具结果相同的 `data` 明细、行数、截断信息、原始 Evidence ID，并明确标注 `reused=true`、`source=existing_tool_result`、“未执行新的数据库查询”。 |
| 3. `query_metric` / `top_n` 是否仍有真实截断 | **没有数据层截断**；唯一截断是模型可见层的 20 行上限，且会显式给出总数、展示数、剩余数、`has_more` 与继续方式。8 行场景完整展示 8 行；24 行场景展示 20 行并标注截断。 |
| 4. `number_accuracy` 假阴性是否修复 | **是（离线验证）**。仍保留严格的指标归属、错误数字、无依据数字与表格列错配拒绝；容差仍为 `0.005`，未放宽。 |
| 5. 统计口径是否统一 | **是**。JSON 与 Markdown 均区分计划内重复调用、模型发起调用、实际执行调用、语义去重拦截、成功数据查询；Token 仅汇总有独立记录的案例并声明缺失数量，不推断。 |
| 6. 语义去重是否仍启用 | **是**。未关闭、未降低匹配阈值；新增的是“原结果可见性不足时不得拦截”的门控。 |
| 7. 是否有真实模型证据 | **没有**。本阶段严格遵守离线边界；8 产品端到端场景是 scripted TEST FAKE，不是真实模型评测。 |

## 1. 变更文件

| 文件 | 变更 |
| --- | --- |
| `src/enterprise_business_agent/agent/tools.py` | 新增 `MODEL_VISIBLE_ROW_LIMIT = 20`、行字段映射与 `model_visible_data()`；`top_n`、`contribution` 摘要改为最多展示 20 条并显式标注展示数量。 |
| `src/enterprise_business_agent/agent/graph.py` | 正常工具消息写入 `data`/`reused`；覆盖记录保存 `visible_data`、可见完整性、行列数与查询参数；新增 `_visible_can_satisfy()`；去重复用前重新授权；拦截回传携带完整可见明细；`ToolCallRecord` 记录可见性元数据。 |
| `src/enterprise_business_agent/agent/contracts.py` | `ToolCallRecord` 新增 `reused`、`row_count`、`displayed_count`、`truncated`。 |
| `src/enterprise_business_agent/agent/evaluation.py` | 分句改为“句号/问号/感叹号/换行”硬边界 + 子句级指标—数字归属；保留表格列校验；新增 `attempted_tool_calls`、`successful_data_tool_calls`；统计块新增重复/执行/去重/数据查询与 Token 覆盖字段；`tool_call_trace` 增加可见性字段。 |
| `scripts/run_phase7_evals.py` | Markdown 重命名并拆分重复调用口径，新增模型发起、实际执行、语义去重拦截、成功数据查询行；Token 行声明覆盖与缺失。 |
| `tests/test_agent_runtime.py` | 新增 8 产品去重复用、5/8/20/>20 行可见性、截断不误拦截、复用前重新授权测试。 |
| `tests/test_phase7_evals.py` | 新增分号/英文分号/逗号/换行/表格归属、错误指标与错误数字拒绝、`number_accuracy` 集成、统计口径测试。 |
| `tests/test_phase7_1_preflight.py` | 新增 JSON/Markdown 统计口径一致性测试。 |
| `docs/phase7/PHASE7_5_COMPLETION_REPORT.md` | 本报告。 |

## 2. 问题根因与修复前后

### 2.1 正常工具消息缺少明细

**根因**：`ToolResult.data` 已包含完整 `points`/`items`，但 `_act()` 只把 `status`、`summary`、`error`、`validation_errors`、`evidence_ids`、`recovery_hint` 写入模型消息。`query_metric` 的 `summary` 只有首条/末条，`top_n` 的 `summary` 只列前 5 条，模型因此看不到 8 个产品的逐项值。

**修复后**：

- 所有数据类工具在 `status=ok` 时附带 `data`：`tool`、`metric`、`label`、`unit`、`source_table`、`time_range`、`dimensions`/`dimension`/`grain`、`row_count`、`displayed_count`、`truncated`、`remaining_count`、`display_limit`、`has_more`、`rows`、`next_step`；比较/贡献类另有期间与合计信息。
- 每行只保留必要字段（如 `query_metric` 的 `period`、`dimensions`、`value`、`unit`、`status`、`reason`），不附带 SQL 或执行计划。
- 超过 20 行时明确写出“当前仅展示前 20 条，共 N 条”、剩余数量与继续方式；未截断时明确写“已展示当前结果的完整明细”，不再用首/末值暗示完整性。
- `top_n`/`contribution` 的摘要同步改为最多展示 20 条，并标注“展示 M/N 条”。

**权限边界未改变**：`authorize_call`、SQL Guard、只读事务、指标/维度/过滤域校验全部保留；可见层只序列化工具已经返回的数据。

### 2.2 语义去重命中后信息不完整

**根因**：`_coverage_from_result()` 只保存摘要、成员代码与 Evidence ID；`_covered()` 与 `_suppress_duplicate_call()` 也只回传这些字段。判重规则在数据层正确，但模型拿到“已被拦截”的消息后仍看不到数值，形成信息不对称。

**修复后**：

- 覆盖记录新增 `arguments`、`visible_data`、`visible_complete`、`row_count`、`displayed_count`、`truncated`。
- 拦截回传携带与正常工具消息同一来源的 `data` 视图、行数/展示数/截断标记、原始 `evidence_ids`，并显式写 `reused=true`、`source=existing_tool_result`。
- 文案明确“本次未执行新的数据库查询，而是复用已有工具结果”；结果截断时改为提示按 `data.next_step` 缩小范围。
- **不创建新 Evidence**：回传只引用原 Evidence ID；离线测试断言结果仍只有 1 条 Evidence。
- **复用前重新授权**：用覆盖记录保存的原始参数调用 `ToolRegistry.authorize_call()`。若当前 principal 已无权读取，则不拦截、走正常执行路径，由工具返回 `permission_denied`，避免通过去重路径泄露数据。
- **可见完整性门控**：`_visible_can_satisfy()` 规定截断结果不能被 `top_n`/`trend` 复用（这些请求需要未展示的行），必须允许正常执行；唯一例外是重复同一条 `query_metric`——重新查询也不会产生更多行，此时仍拦截并返回已有的 20 行视图及继续提示。
- 判重条件仍包含指标、时间范围、过滤条件、维度、粒度、排序方向、Top N 数量与完整性；未降低阈值。

### 2.3 `query_metric`/`top_n` 截断分层核查

| 层 | 观察 | 结论 |
| --- | --- | --- |
| 数据库查询 | `AnalysisGateway` 返回的行数与 `row_count` 一致（8 产品=8，24 个月=24） | 无 SQL 层 `LIMIT` 截断 |
| 工具结果 | `ToolResult.data.points`/`items` 保存全部行 | 无工具层丢行 |
| Evidence | 每个成功结果生成 1 条 Evidence，引用同一 `tool_call_id`；Evidence 保存摘要与溯源信息，不另行复制受限数据 | 去重复用不新增 Evidence |
| 模型可见消息 | 正常调用与去重复用均使用同一 `model_visible_data()` | 唯一截断点为 20 行上限 |
| 去重复用 | 复用读取覆盖记录中的 `visible_data` | 与原结果一致，不重新查询、不伪造 |

结论：这是**有意的模型上下文上限**，不是数据丢失。截断时会显式说明，模型不会把“只看到 20 行”误认为“总共 20 行”。

### 2.4 `number_accuracy` 假阴性

**根因**：`_SEGMENT_BOUNDARY` 含中文分号 `；`。A 批回答中“库存天数……；首条值 26.713990”被切成两段：数字所在段没有指标词，前一段虽含指标词但同时含其他数字，旧规则不允许继承，于是正确数字被判为“不邻近”。这不是数字造假，`numeric_validation_passed` 当时已为 `true`。

**修复后**：

- 硬边界改为 `。！？\n`；分号、逗号、顿号、冒号作为子句边界，在同一句内做归属。
- 数字优先匹配自身子句中的指标词。
- 仅当数字子句是“值延续”（`首条值`、`末条值`、`其中`、`同比`、`环比`、`增长`、`下降`、裸数值等）或前序子句是标签（`：`、`:`、`为`、`是`、`如下`、`分别`、`依次`）时，才允许从前序子句继承指标。
- 跨行/列表场景支持 `P001=3`、`- 华北 3 天` 等格式；若子句标签明显属于另一个指标（如 `净利润`），或列表项标签含指标语义，则拒绝继承。
- Markdown 表格仍先按表头列归属，双指标表格换列仍失败。
- 容差仍为 `0.005`；未移除任何数字检查，也未因“数字出现在 Evidence 中”而放行。

验证结果：正确数字通过；错误数字（27.5 vs 26.713990）失败；属于其他指标的数字失败；无指标支撑的数字失败；表格列错配失败。

### 2.5 统计口径

**根因**：Markdown 的“重复工具调用次数”取 `plan.repeated_calls`（计划内重复），而 JSON 记录级另有 `duplicate_tool_calls`（语义去重拦截）；同名不同义。Token 方面，记录中实际存在 `llm_tokens={'input','output'}`，A 报告所说的“记录级 token 为空”指的是不存在 `input_tokens`/`output_tokens` 这两个字段名，**并非没有 token 数据**。

**修复后**：

- JSON `overall` 与 Markdown 同时提供：
  - `planned_repeated_tool_calls`（计划内重复）
  - `attempted_tool_calls`（模型发起的工具调用，含被拦截）
  - `executed_tool_calls`（实际执行）
  - `semantic_dedupe_blocks`（语义去重拦截/复用，不产生新数据库查询）
  - `successful_data_tool_calls`（成功的数据查询调用）
- Token 只从含有非空 `llm_tokens` 的记录汇总，并输出 `token_records`、`token_records_missing`、`tokens_complete`；Markdown 在缺失时写明“仅汇总 n/m 条，缺失 k 条不做推断”。
- `tool_call_trace` 新增 `reused`、`row_count`、`displayed_count`、`truncated`，便于区分执行与复用。

## 3. 新增离线测试

| 测试 | 覆盖 |
| --- | --- |
| `test_deduped_top_n_returns_every_existing_product_value` | 8 个产品（值各不相同）→ `top_n(n=8)` 被去重拦截 → 拦截回传包含全部 8 行、原 Evidence ID、`reused=true`；数据库只查询 1 次；无新 Evidence。 |
| `test_model_visible_rows_cover_5_8_20_and_more[...]`（4 例） | 5/8/20 行完整展示；24 行展示 20 行并给出总数、剩余数、`has_more` 与继续提示；记录级 `displayed_count`/`truncated` 一致。 |
| `test_truncated_query_metric_does_not_block_a_different_tool` | 截断的 24 行 `query_metric` 不再拦截 `trend`，验证“无法满足即放行”。 |
| `test_duplicate_reuse_is_reauthorized_before_it_is_returned` | 复用前对原始参数重新授权；拒绝时走正常执行，不静默复用。 |
| `test_number_match_binds_values_across_clause_boundaries` | 中文/英文分号、逗号、换行、列表、Markdown 表格、变化率句式。 |
| `test_clause_level_matching_still_rejects_wrong_attribution` | 其他指标、指标标签冲突、无指标数字、错误数值仍失败。 |
| `test_number_accuracy_uses_the_clause_level_binding` | 端到端 `number_accuracy=true`（仍为 scripted TEST FAKE）。 |
| `test_summary_separates_planned_repeats_dedupe_blocks_and_tokens` | JSON 统计字段互不混用；Token 缺失声明。 |
| `test_markdown_reports_dedupe_and_tokens_with_the_json_meanings` | Markdown 五类工具调用口径与 Token 行和 JSON 一致。 |

## 4. 回归结果

| 检查 | 结果 |
| --- | --- |
| 专项测试（runtime / phase7 evals / preflight） | **117 passed** |
| 全量 `pytest -q` | **590 passed, 25 skipped, 0 failed**（63.54 s） |
| `scripts/run_phase7_evals.py --check-only` | **92 cases，OK，no model call was made** |
| `python -m compileall -q src scripts tests` | **通过** |
| 格式/类型检查 | 项目未安装 `ruff`/`black`/`mypy`/`flake8`；本阶段未新增依赖，只执行了编译检查 |
| 真实 LLM 调用 | **0 次** |

基线对比：Phase 7.4 全量为 578 passed；本阶段净增 12 个测试（含 4 个参数化用例），跳过数未变。

## 5. 不变量与历史归档

| 项目 | 校验 |
| --- | --- |
| 第一轮归档 SHA-256 | `4ab4c4c82e65eeb6f8869bc5ef7266f7bcdab96faa3ff819619998750badeb4d`（未变） |
| 第二轮归档 SHA-256 | `0e9f60cfde12c19093f3dfb5b7dd2d3ff95d589c3518ed492164ae4acc079f05`（未变） |
| 第三轮 A 批归档 SHA-256 | `cc2e4a3e7109da7fc983bb5fded1c3598af7840154ed567def664a3f934f5427`（未变） |
| 92 例语料 SHA-256 | `5360495e395a08567fc3cac0f988eac721c873980fef926ca57594d7ad4fc9c3`（未变） |
| Prompt 版本 | 未修改 |
| 92 例预期答案 / canonical 值 | 未修改 |
| 测试删除或断言降低 | 无 |
| `.env` / 密钥 / 数据库密码 | 未读取、未打印、未修改 |

## 6. 剩余风险

1. **未做真实模型复验（高）**：8 产品端到端场景使用 scripted TEST FAKE。真实模型是否会更早/更晚触发去重、是否会正确使用 `data.rows`，必须由后续授权的 A 批复测或 B 批回答，本报告不作断言。
2. **20 行上限（中）**：超过 20 行的结果仍不会一次性全部进入模型上下文；设计上通过显式截断说明和“不同工具可重新执行”来补偿，但真实模型可能仍需要多轮缩小范围。
3. **重复同一截断 `query_metric` 仍被拦截（中低）**：重新查询不会产生更多行，因此拦截并返回已有 20 行是合理选择；若真实模型无法据此调整查询，表现为任务未完成而非数据丢失。
4. **指标标签启发式（中低）**：`number_accuracy` 的列表/表格归属依赖有限的中文指标语义提示（率、额、利润等）。对非常规写法仍可能误判；已用正向/反向用例锁定主要边界，未放宽容差。
5. **旧归档兼容（低）**：第一/二轮与 A 批归档没有 `attempted_tool_calls`、`successful_data_tool_calls` 等新字段，跨版本汇总时这些计数会显示 0 或按旧字段回退；不能把旧归档的新口径计数当作完整统计。
6. **上下文成本未测量（低）**：每行明细会增加 prompt token；本阶段没有真实调用，无法给出 token 增量。`MODEL_VISIBLE_ROW_LIMIT=20` 是当前控制手段。
7. **未安装静态检查工具（低）**：仅有 `compileall` 与 pytest 证据，没有 ruff/mypy 结果。

## 7. 本阶段未做事项

- 未发起任何真实 GLM 或其他 LLM 调用。
- 未执行真实模型评测，未启动 A 批复测、B 批、C 批或完整 92 例。
- 未修改 Prompt 版本、canonical 指标、业务数据或 92 例预期答案。
- 未关闭语义去重，未降低匹配条件，未绕过权限、SQL Guard 或 Evidence 账本。
- 未覆盖第一/二/三轮历史归档。
- 未删除失败测试、未降低断言、未放宽评分标准。
