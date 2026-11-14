# Phase 7.6 / 7.7 离线验收报告

> 后续状态（2026-11-02）：Phase 7.8 的 A/B/C/D 真实批次已执行完成，66 个 `phase7-*` 全部有真实记录。本文件第 8 节“等待授权”只反映当时状态；最终统计、失败分类与 P008/多成员触发情况以 [`PHASE7_FINAL_ACCEPTANCE_REPORT.md`](PHASE7_FINAL_ACCEPTANCE_REPORT.md) 为准。

- 生成日期：2026-11-02
- 范围：评测器可信度修复（7.6）与全量离线回归（7.7）
- 真实模型调用：**0 次**。本报告中的全部结论来自离线测试、`--check-only` 与对历史归档的只读复核。
- 历史归档：不覆盖、不改写。验证前后 4 个 JSON 与 13 个 Markdown 的 SHA-256 完全一致。

## 1. 结论

| 项目 | 结果 |
|------|------|
| 7.6 数字核验数据源修复 | 完成 |
| 7.6 业务关系校验 | 完成 |
| 7.6 模型工具消息归档 | 完成 |
| 7.6 离线回归测试 | 新增 27 条，全部通过 |
| 7.7 全量 pytest | 617 passed, 25 skipped |
| 7.7 92 用例 check-only | 92 cases, OK，未发起模型调用 |
| 7.7 compileall | 通过 |
| 历史归档完整性 | 17 个文件哈希不变 |

离线验收通过。7.8 真实模型验收尚未开始，需单独授权（见第 8 节）。

## 2. 修改前的缺陷（已从代码确认）

1. **数字核验只看摘要**
   `AgentRuntime._unverified_numbers()`（`src/enterprise_business_agent/agent/graph.py:1928`）只拼接
   `evidence.result_summary`、`verified_findings` 等文本，而工具消息里已经显示给模型的明细行不在核验范围。
   结果是：正确明细数字被判为“无法核对”，虚构数字却仍可能混入摘要。
2. **来源数字使用完整结果**
   `_source_numbers()`（`src/enterprise_business_agent/agent/evaluation.py:1148`）在没有消息时使用
   `result.data` 全量数据，包含未展示给模型的行；模型不可能引用未看到的数据，来源校验口径不一致。
3. **没有业务关系校验**
   评测器只做数字是否出现过的匹配，不判断最大值/最小值、排名、Top N 方向、数量、并列等关系。
   P008 的分母 14779 确实存在于数据中，因此被当作合法数字；但“P008 最大”在业务上是错的。

### 历史证据（phase7-026，真实归档，未改动）

`data/exports/phase7_eval.third_round.a_phase7_026.json` 记录：

- `unsourced_numbers = []`、`answer_number_provenance = true`
- `warnings = ["模型结论包含无法在工具证据中直接核对数字：26.762447, 26.642075, 87.345507, 79.203518, 30.000000"]`
  —— 这 5 个数都是明细表中真实存在、且模型看得到的正确数字。
- 结论写“P008 分母（14779.000000）为 8 个产品中最大”，实际最大值为 P003 的 18163.000000。
  旧评测器没有发现该错误，只把 `conclusion_overreach` 归因到上面的误报。

## 3. Phase 7.6 修复内容

### 3.1 数字核验使用“模型实际看到且可追溯”的数据

新增契约 `ModelToolMessage`（`src/enterprise_business_agent/agent/contracts.py`）：

- 工具调用 ID、工具名、被请求工具名、状态、是否复用
- Evidence ID 列表、参数、消息正文、正文 SHA-256、正文字节数
- 模型可见数据、总行数、展示行数、是否截断
- `trusted` 标记

`trusted=True` 必须同时满足（`graph.py` `_act`）：

1. 工具状态为 `ok`；
2. 返回了 Evidence；
3. 所有 Evidence 均可追溯；
4. 存在模型可见数据。

消息在追加到 `state["messages"]` 的时刻捕获（`_capture_tool_message`，`graph.py:1831`），
不是运行结束后重建。语义去重复用时同样捕获一条 `reused=True` 的消息，
其 `trusted` 取决于被复用覆盖记录是否可追溯、是否有可见数据。

数字核验口径（`_unverified_numbers`）：

- 摘要、明细、截断、未展示四类数据被明确区分；
- 只有 `trusted` 消息中 `visible_data.rows` 的数字进入核验；
- 因展示上限未出现在 `rows` 的第 21 行及以后，不参与核验；
- 不可追溯的工具结果不参与核验；
- 差、和、比率等可由已返回数字复算的结果仍然允许。

来源校验口径（`_source_numbers`）：

- 有捕获消息时只使用 `trusted` 消息的可见数据；
- 捕获列表为空时**不再**回退到完整 `result.data`（避免把未展示行重新当作来源）；
- 仅在调用方显式传 `None`（无运行时记录的旧调用方式）时回退。

### 3.2 确定性业务关系校验

新增 `src/enterprise_business_agent/agent/relations.py`，只对 `trusted` 且含可见行的数据集生效：

- 最大值/最小值：先解析字段（分母→`denominator`、分子→`numerator`，否则按数据集主字段），
  再定位结论中离“最大/最高/最多/最小/最低/最少”等词最近的成员，比较该成员的值与数据集极值；
- 并列与唯一性：值相等时多个赢家，若结论称“唯一”，判为不一致；
- 数量：`共/合计/总计/一共 N 个/条/项/名/种` 与 `row_count` 比较；
- 排名：结论中的“排名第 N”与工具返回的 `rank` 比较；
- Top N 顺序：按请求的 `ascending` 检查行顺序，并检查 `rank` 是否从 1 连续；
- 结论顺序：结论中成员的先后顺序不能与 Top N 排序方向相反；
- 截断结果：问题记录带 `truncated=true`，措辞为“已展示的 N 行中”，不把未展示行当作不存在。

命中后：

- `_finish` 追加警告“模型结论与工具数据的业务关系不一致：…”，并写入 `relation_issues`；
- 评测器把 `unsupported_relation` 计入 `conclusion_overreach`；
- `LiveEvalResult`、归档记录与 Markdown 指标新增 `relation_validation_passed`、`relation_validation_failures`。

### 3.3 模型工具消息归档

`_archive_tool_messages(run_id, case_id, messages)`（`evaluation.py:1109`）写入每条记录：

`run_id`、`case_id`、`tool_call_id`、`tool`、`requested_tool`、`status`、`reused`、`trusted`、
`evidence_ids`、`arguments`、`content`、`content_sha256`、`content_bytes`、`content_truncated`、
`visible_data`、`row_count`、`displayed_count`、`truncated`、`created_at`。

- 归档内容就是运行时追加到 `state["messages"]` 的 tool 消息正文；
  测试 `test_runtime_archived_tool_message_equals_the_message_the_model_received`
  断言它与脚本模型客户端收到的 tool 消息逐字节相同、SHA-256 一致。
- 密钥样式文本（`api_key`、`password`、`secret`、`access_token` 等赋值）在写入前被替换为 `***`。
- 正文超过 16,384 字节时截断正文，但 `content_sha256` 仍对**原始**正文计算，便于事后核对。
- 权限隔离由运行主体决定：消息只包含该次运行已授权工具返回、且已展示给模型的数据。
- 不记录 API Key、数据库密码、连接串或其他凭据。

### 3.4 新增离线回归（27 条，全部有明确预期）

| 覆盖点 | 测试 | 预期 |
|--------|------|------|
| 正确明细数字可通过 | `test_detail_number_in_trusted_tool_message_is_verified` | 无误报 |
| 虚构数字仍失败 | `test_fabricated_number_is_still_flagged` | 仅 `99999` 被标记 |
| 缺少 Evidence | `test_untrusted_tool_message_cannot_verify_numbers`、`test_tool_result_without_evidence_is_not_trusted_and_cannot_verify_numbers` | `trusted=false`，数字被标记 |
| 重复调用/结果复用 | `test_reused_dedupe_message_verifies_numbers_when_traceable`、`test_dedupe_reuse_is_captured_as_a_trusted_reused_message` | 复用消息可核验，且记录 `reused=true`、`requested_tool=top_n` |
| 截断数据 | `test_numbers_from_undisplayed_rows_of_a_truncated_result_do_not_verify`、`test_truncated_result_scopes_the_maximum_to_displayed_rows` | 展示行可核验，未展示行不可；关系问题带 `truncated=true` |
| 最大值正确 | `test_correct_maximum_claim_passes` | 无问题 |
| 最大值错误（P008 vs P003） | `test_wrong_maximum_claim_is_rejected_even_though_the_number_exists` | `max_mismatch`，`claimed=14779`、`actual=18163` |
| 不可追溯消息 | `test_relation_check_ignores_untrusted_messages` | 不产生关系问题 |
| 并列/唯一 | `test_unique_claim_is_rejected_when_values_tie`、`test_unique_claim_passes_with_a_single_winner` | 并列时报 `unique_claim_violated` |
| 排名 | `test_rank_claim_must_match_the_tool_rank` | `rank_mismatch` |
| 数量 | `test_count_claim_must_match_the_returned_row_count` | `count_mismatch` |
| Top N 排序方向 | `test_top_n_rows_must_be_ordered_by_the_requested_direction` | `top_n_order_mismatch` |
| 结论顺序 | `test_answer_order_cannot_reverse_the_top_n_result` | `answer_order_mismatch` |
| 来源数字 | `test_source_numbers_prefer_displayed_rows_over_the_full_result`、`test_source_numbers_ignore_untrusted_messages`、`test_source_numbers_never_fall_back_when_capture_is_empty` | 只用可见行；空捕获不回退 |
| 归档身份与哈希 | `test_archive_records_identity_hash_and_visible_rows` | run/case/tool/hash/rows 正确 |
| 归档复用标记 | `test_archive_marks_reused_calls_and_the_requested_tool` | `reused=true`、`status=duplicate_suppressed` |
| 归档脱敏 | `test_archive_redacts_secret_like_assignments` | 两处密钥均被 `***` 替换 |
| 归档大小上限 | `test_archive_caps_oversized_content_without_losing_the_hash` | 正文 ≤16,384 字节，哈希仍为原文 |
| 模型输入一致性 | `test_runtime_archived_tool_message_equals_the_message_the_model_received` | 归档正文 = 模型收到的 tool 正文 |
| 评测记录集成 | `test_eval_record_archives_tool_messages_and_flags_a_wrong_relation`、`test_eval_record_passes_when_the_claimed_maximum_is_correct` | 错误最大值使 `conclusion_overreach=true`；正确最大值通过来源校验 |

## 4. Phase 7.7 全量离线回归

执行的命令与结果：

```
.venv/bin/python -m pytest -q
→ 617 passed, 25 skipped in 53.21s

.venv/bin/python scripts/run_phase7_evals.py --check-only
→ corpus: 92 cases
→ corpus check: OK (no model call was made)
→ exit=0

.venv/bin/python -m compileall -q src scripts tests
→ COMPILE_OK
```

- 基线：Phase 7.5 记录为 590 passed；本轮 617 passed（新增 27 条）。
- 跳过项：25 条，原因均为未设置 `EBA_TEST_POSTGRES_URL`（需要外部 PostgreSQL 的用例），与本次修改无关。
- 覆盖检查：数字准确性、证据可追溯性、重复调用、截断、安全边界均在 `pytest` 中执行；
  相关文件包括 `tests/test_phase7_evals.py`、`tests/test_agent_runtime.py`、
  `tests/test_agent_security_boundaries.py`、`tests/test_phase7_1_preflight.py` 与本轮新增文件。
- **TEST FAKE / Scripted Planner 不计入真实模型调用**：
  - 全量 pytest 默认加 `-m 'not live_model'`；
  - `--check-only` 在读取 provider 配置之前返回；
  - 本轮没有调用任何真实模型，也没有生成新的真实模型 JSON。

## 5. 修复前后的实测对照（真实归档 + 只读真实数据）

用 phase7-026 的真实 `final_answer`，对该题真实执行的 `query_metric`（`inventory_days`，按产品，
2025-12）重新取数并构造可信消息，得到：

| 项目 | 修复前（归档记录） | 修复后（当前代码） |
|------|--------------------|--------------------|
| 误报数字 | 5 个正确明细数字被标记 | `[]` |
| 最大值关系 | 未检查 | 1 条 `max_mismatch` |
| 问题内容 | — | `P008 的 denominator 为 14779.000000，但 8 行中最大值是 18163.000000（P003）` |

说明：修复前该用例 `passed=false` 的原因是误报；修复后同样的错误结论仍会失败，但原因变为
真实的业务关系错误。判据没有放宽，只是从“数字是否出现”升级为“结论是否与数据关系一致”。

## 6. 未验证项与剩余风险

1. **未用真实模型验证新字段**：7.6/7.7 全部离线；`tool_messages`、`relation_validation_passed`
   尚未在任何 GLM 真实运行中产生记录，需要 7.8 授权后才能确认线上表现。
2. **关系校验是启发式**：只在结论明确点名成员、且字段可解析时判定；
   泛化表述（如“整体最高”）不会触发，可能漏报。
3. **语言与字段覆盖有限**：字段提示目前覆盖分母/分子，其他字段依赖数据集主字段；
   数量正则只覆盖常见中文量词。
4. **截断数据**：关系检查只能基于已展示行，隐藏行无法验证；记录会标记 `truncated=true`，
   但不会推断“全局最大/最小”。
5. **归档正文可能被截断**：超过 16,384 字节时正文不完整，哈希仍对应原文，
   需要在读取端用哈希判断完整性。
6. **脱敏是正则式的**：覆盖常见的密钥赋值形式，不等同于通用 DLP；工具消息本身不含凭据。
7. **归档内容与 provider 传输层**：归档的是运行时交给 LLM 客户端的 tool 消息字符串；
   客户端到厂商的 wire 编码未在本轮验证（本轮没有真实请求）。
8. **未改动**：`.env`、Prompt、canonical 数据、业务期望、历史归档均未修改。

## 7. 涉及文件

新增：

- `src/enterprise_business_agent/agent/relations.py`
- `tests/test_phase7_6_evaluator.py`

修改：

- `src/enterprise_business_agent/agent/contracts.py`（`ModelToolMessage`、响应/快照字段）
- `src/enterprise_business_agent/agent/__init__.py`（导出）
- `src/enterprise_business_agent/agent/graph.py`（消息捕获、数字核验、关系校验、响应字段）
- `src/enterprise_business_agent/agent/evaluation.py`（归档、来源数字、关系指标）
- `scripts/run_phase7_evals.py`（Markdown 指标：关系一致性、归档数量与截断数量）

## 8. Phase 7.8 A 批次：建议与等待授权

离线验收已通过。A 批次仍然**没有执行**，需要明确授权。

建议命令（单用例、硬预算、独立输出、不改历史文件）：

```
.venv/bin/python scripts/run_phase7_evals.py \
  --allow-live-model --load-dotenv \
  --ids phase7-026 \
  --max-model-calls 40 \
  --case-max-seconds 120 \
  --output data/exports/phase7_eval.a_batch2.phase7_026.json \
  --markdown docs/phase7/PHASE7_EVALUATION_REPORT.a_batch2.phase7_026.md
```

- 预算：最多 40 次模型调用；单用例 120 秒；整轮默认 1800 秒。
- 上一次同一用例实测：2 次调用、14,319 tokens、约 24 秒，用于估算成本。
- 预期收益：验证新数字核验与关系校验在真实模型输出上的表现；
  同一份材料将首次包含真实 `tool_messages` 归档。
- 风险：结论关系错误会被新校验判失败（这是预期行为）；
  若出现超时/不可用/失败，按现有停止策略终止并如实记录，不把未执行用例计为失败。
- 不做：不运行 B/C/D 批次，不改判据/容差，不覆盖任何历史文件，不把离线结果算作真实结果。

等待明确授权后再执行 A 批次；B/C/D 批次需再次单独授权。
