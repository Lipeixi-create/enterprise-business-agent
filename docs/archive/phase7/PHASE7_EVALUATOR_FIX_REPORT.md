# Phase 7.8 评测器缺陷修复报告

日期：2026-11-02

本报告只记录实际修改、实际运行的测试与实际观察到的真实模型输出。真实调用数、Token 与通过率以归档 JSON 为准；未触发路径明确标注“未触发”，构造数据验证不写成“真实归档重放”。

---

## 1. 问题一：P008 最大值字段映射错误

### 根因

`agent/relations.py` 的字段选择原先只覆盖 `value`、分子、分母等少数词，同句出现多个字段提示时没有稳定取舍。历史第三轮 `phase7-026` 的真实答案写出：

> P008 分母（14779.000000）为 8 个产品中最大

P008 的 `denominator=14779` 不是全表最大分母；P003 的 `denominator=18163` 才是。断言本身是错误结论，但旧逻辑没有在正确字段上验证它。

### 修复

- `_FIELD_HINTS` 增加采购金额/采购额 → `numerator`、采购数量/采购量 → `denominator`，并保留分母/分子、贡献、变化率、当前值/基期等提示。
- `_field_for` 以“距离标记最近”为首要排序，距离相同时取更长提示，避免短词 `值` 压过 `库存天数`、`采购金额` 等具体字段。
- `_check_superlative` 对每个被点名成员逐一解析字段并在该字段上比较；字段缺失时跳过，不退回 `value`。
- 括号内容在检测最高/最低标记前被遮蔽，避免同一句的对照说明生成第二个断言。

### 离线验证

`tests/test_phase7_6_evaluator.py` 新增并通过：

| 用例 | 断言 |
| --- | --- |
| `test_p008_wrong_inventory_days_maximum_is_rejected` | P008 错误最大值被拒绝 |
| `test_historical_p008_denominator_sentence_is_rejected` | 历史真实句 `P008 分母（14779.000000）为 8 个产品中最大` 在真实 8 行分母值下被拒绝，`field=denominator`，实际最大 P003 18163 |
| `test_p003_correct_inventory_days_maximum_passes` | P003 正确最大值通过 |
| `test_field_hints_keep_numerator_and_denominator_apart` | 分子/分母不混用 |
| `test_named_amount_and_quantity_claims_use_numerator_and_denominator` | 采购金额/采购量字段映射 |

### 真实模型是否触发

- **是（历史真实输出）**：`data/exports/phase7_eval.third_round.a_phase7_026.json` 中模型明确写出“P008 分母（14779.000000）为 8 个产品中最大”。这是真实模型产生的错误比较。
- **不能声称该历史归档已被重放拦截**：该归档是旧 schema，没有 `tool_messages`/`visible_data`；`relation_issues` 没有可信行可比较，直接对该归档重放返回空。可以确认的是：同一真实措辞、同一组真实值构造的离线 trusted 消息会被拒绝。
- **本轮 A 批次未再次触发**：`data/exports/phase7_eval.a_batch2.phase7_026.json` 的答案改为按库存天数排序，没有再写 P008 分母最大。

---

## 2. 问题二：多成员最高/最低断言

### 根因

旧实现只取断言附近的一个成员，或用前缀/后缀并集猜测，导致：

- “A 和 B 都是最高”只验证 A；
- 把“最高的是 A，最低的是 B”中的 B 误并入最高断言；
- 不区分唯一极值与并列；
- 数据被截断时仍推断全局极值；
- Top N 表述（“最低的三名”）被误当成单个成员断言。

### 修复

- `_claim_members` 先找到标记，再从标记后截取成员串；只有成员之间由列表连接词（`、,，和与及以及&/+`、英文 filler）连接时才接受多个成员，否则只取最近的一个；不做前缀与后缀并集。
- `_marker_end` 统一处理中英文标记长度；`_is_top_n_phrase` 识别“前/后/第 N 名/个/项”和英文 `top/bottom/first/last N`。
- `_check_superlative` 对每个成员逐一验证；所有被声称“最高/最低”的成员都必须等于真实极值。并列第一必须全部等于极值；把第一名和第二名都称为“最高”会失败。
- 截断数据只有可见行参与比较；若断言暗含全局极值且 `truncated=true`，记录 `truncated_scope_overreach`。
- 最高与最低在同一句中出现时分别检测；括号内对照说明不生成额外断言。
- 中英文均支持；普通指标词或成员名称中的词不会仅因包含“最大/最高”就被当成成员。

### 离线验证

`tests/test_phase7_6_evaluator.py` 中与本问题直接相关的用例包括：

| 用例 | 断言 |
| --- | --- |
| `test_p008_wrong_inventory_days_maximum_is_rejected` | 错误最大值被拒绝 |
| `test_historical_p008_denominator_sentence_is_rejected` | 历史真实措辞被拒绝 |
| `test_p003_correct_inventory_days_maximum_passes` | 正确最大值通过 |
| `test_two_true_tied_firsts_pass` | 两个真实并列第一通过 |
| `test_claiming_first_and_second_as_highest_fails` | 第一、第二同时称最高失败 |
| `test_every_wrong_member_in_a_multi_member_claim_is_identified` | 每个错误成员都被指出 |
| `test_tied_members_with_unique_wording_are_rejected` | 并列却用“唯一最高”措辞被拒绝 |
| `test_truncated_data_cannot_infer_a_global_extremum` | 截断数据不能推断全局极值 |
| `test_minimum_and_multi_member_minimum_are_validated` | 最低与多成员最低均验证 |
| `test_max_and_min_claims_in_one_sentence_are_both_checked` | 同句最高/最低分别检查 |
| `test_english_multi_member_superlative_is_validated` | 英文多成员断言 |
| `test_metric_wording_is_not_treated_as_a_member` | 指标措辞不被当成成员 |
| `test_same_sentence_max_and_min_do_not_bleed_into_one_claim` | 同句最高/最低不串扰 |
| `test_parenthetical_contrast_does_not_create_a_second_claim` | 括号对照不生成第二断言 |
| `test_top_n_phrasing_is_not_a_single_member_minimum_claim` | Top N 不按单成员断言 |
| `test_named_amount_and_quantity_claims_use_numerator_and_denominator` | 采购金额/采购量字段映射 |

### 真实模型是否触发

- **是（本轮 A）**：`data/exports/phase7_eval.a_batch2.phase7_026.json` 的真实答案写：

  > 库存天数最高的是移动硬盘（87.345507）和会议摄像头（79.203518）

  P004 是真实最大，P005 不是。该归档有完整 `tool_messages`，新逻辑直接重放得到 `max_mismatch`，成员 P005，实际最大 P004。旧逻辑当时判为通过，属于**旧评测器的假通过**；离线重放改为失败。
- 本轮 B/C/D 未再观察到新的多成员最高/最低错误，不能声称 B/C/D 再次触发该路径。
- 真实并列观察：`phase7-035` 四个部门离职率均为 0，模型明确说明并列，关系检查未报错；该用例失败原因是工具链与用例要求不一致，不是关系错误。

---

## 3. 顺带修复与仍未修复的真实误报

### 3.1 已修复（离线重放生效）

1. `_terms_apply` 绝对/相对偏移混用，列表第二行以后数字落到错误分句；改为绝对偏移。
2. `_derived_candidates` 只生成 `left-right`；补充 `right-left`，小值在前时正差不再误报。
3. `_unsourced_numbers` 不接受派生值；改为使用两值差/和/比、三值求和与负数变化率重述。
4. 百分比两位小数重述与百分比重述（如 `0.257339 → 25.73%`）不再误报。
5. 日历年 `2025` 与工具调用 ID 不再被当作无来源业务数字。
6. Top N 表述不再生成单成员 `min_mismatch`。
7. “采购金额/采购量最大”按分子/分母比较。

对应测试包括：

- `test_number_accuracy_reads_a_later_numbered_list_item`
- `test_number_accuracy_reads_a_bullet_under_a_bold_heading`
- `test_difference_of_returned_values_is_sourced_when_the_smaller_comes_first`
- `test_unverified_difference_uses_the_positive_gap_regardless_of_order`
- `test_negative_rate_can_be_restated_as_a_positive_percentage`
- `test_percentage_and_rounding_of_a_returned_rate_are_sourced`
- `test_year_is_not_counted_as_an_unsourced_business_number`
- `test_sum_of_three_returned_values_is_sourced`

这些修复使 5 条本轮旧判定失败在离线重放下转为通过：`phase7-007/013/025/032/033`。

### 3.2 本轮续修（2026-11-02，未新增真实模型调用）

以下 7 条由真实归档暴露的评测器误报/漏报已在上一次报告之后继续修复。修复只改评测器，不改归档、模型输出、业务数据、用例期望、Prompt 或容差。

| 用例 | 根因 | 修复 |
| --- | --- | --- |
| `phase7-004` | 变化率 bullet 位于两条毛利 bullet 之后，`_terms_apply` 无法从前文继承指标 | 新增 `_prior_segment_names_metric`：仅当标签本身是利率且前两条含数字分句点明了指标时，允许该值归属该指标 |
| `phase7-009` | 表格 `贡献变化` 列不是直接指标列，数字匹配拒绝 | 新增 `_is_derived_column`，贡献/变化/增减/差额/差异列在所属指标表格内可归属 |
| `phase7-050` | 表格 `变化额` 列同上；“最大拖累”按代数最大值比较，选中 `-3,050,901` 而非更负的 `-5,294,788` | 派生列归属；`_NEGATIVE_EXTREMA` + 负向贡献/变化把“最大拖累/最大降幅”转为最小值比较，问题类型记为 `min_mismatch` |
| `phase7-043` | `0.060249（约 6%）` 的整数百分比未由派生比值量化得到 | `_matches_derived` 增加整数百分比量化（`abs(value)*100` 四舍五入到整数） |
| `phase7-044` | 两组 `top_n`（毛利率升序、缺货率降序）被每个答案段重复套用，产生 5 条关系误报 | `_marker_dataset` 按最近指标名/小节标题/唯一成员选择数据集；`_scoped_answer` 按空行块限定答案顺序检查；中文“第二高/第二低”按后置成员解析，比较句主语不再被当成第 2 名 |
| `phase7-065` | “投入 1361202.53 为三个活动中最高”按 `value` 比较 | `_FIELD_HINTS` 增加 投入/投入金额/花费 → `denominator` |
| `phase7-051` | `list_metrics` 仅展示 20/34 行，模型仍断言“其余 30 个指标…均无其他利润口径”；旧检查在无可信数据集时提前返回 | 新增 `_truncated_absence_issues`：使用 `truncated`/`row_count`/`displayed_count` 元数据，即使消息不可信也拒绝未展示范围的“不存在”断言；仅陈述“其余 N 个未展示”不报错 |

同一轮还修正了中文排名成员解析：优先取同一分句内标记之前的成员，避免“华南缺货率第二高，西部第四”把“西部”当成第 2 名。

### 3.3 续修验证

新增 12 条离线定向测试：

- `test_rate_bullet_after_metric_bullets_is_attributed`
- `test_contribution_column_under_a_sales_table_is_attributed`
- `test_change_column_under_a_profit_table_is_attributed`
- `test_swapped_metric_columns_are_still_rejected`
- `test_integer_percentage_of_a_derived_ratio_is_sourced`
- `test_unverified_number_accepts_the_same_value_with_trailing_zeros`
- `test_unverified_number_accepts_a_sum_of_four_returned_values`
- `test_two_top_n_datasets_do_not_cross_contaminate_relations`
- `test_investment_claim_uses_the_denominator_field`
- `test_largest_drag_compares_the_most_negative_contribution`
- `test_truncated_result_rejects_an_absence_claim_about_unshown_rows`
- `test_displayed_row_count_statement_is_not_an_absence_claim`

对 66 条归档使用当前评测器重放（不是重新调用模型）：

- `004/009/043/050` 由失败转为通过；`044/065` 的关系问题清零，但仍因工具链严格性失败；`051` 新增 `truncated_absence_overreach`，仍因工具/任务严格性失败。
- 关系检查失败从 5 条降为 2 条：`026`（真实多成员最大值错误）与 `051`（截断范围越界）。
- 当前 60 条记录的无来源数字为 **0**；运行时 `unverified_number_warning` 重放为 **0**。
- `026` 保持失败（旧评测器假通过），没有因续修被“修复”为通过。

---

## 4. 离线回归与最终统计

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| 定向评测器测试 | `pytest tests/test_phase7_6_evaluator.py -q` | **65 passed** |
| 全量测试 | `pytest -q` | **655 passed, 25 skipped** |
| 编译 | `python -m compileall src scripts tests` | 通过 |
| 92 条语料检查 | `run_phase7_evals.py --check-only` | `corpus: 92 cases` / `corpus check: OK (no model call)` |

最终真实统计：

- 66 个 `phase7-*` 全部有真实模型记录；26 个 `live-*` 没有 Phase 7 真实记录。
- 真实调用 151 次；记录 Token 867,241 输入 / 36,475 输出 / 903,716 合计；`phase7-057` 缺 Token。
- **运行当时**通过 33/66（50.0%）。
- **当前评测器离线重放**通过 41/66（62.1%）：本轮 60 条中 35 条，历史 6 条按原记录 6 条。重放不产生新的模型调用、Token 或成本。
- 超时 8 条：`017/040/041/042/052/057/063/064`。
- 成本未计算：单价未配置。

重放口径说明：对 60 条有 `tool_messages` 的记录，用当前 `relations.py`/`evaluation.py` 重算关系断言、数字溯源、数字准确性与运行时数字核对；工具链、任务完成、证据充分性、引用有效性沿用归档字段。6 条历史记录没有 `tool_messages`，保留原记录结果，不做完整关系重放。

---

## 5. 结论

三条评测器缺陷线均已处理：

1. P008 错误最大值在离线真实值/真实措辞下可拒绝；历史真实答案确实产生过该错误，但旧归档缺少 `tool_messages`，不能声称该归档被直接重放拦截。
2. 多成员最高断言在本轮 A 026 的真实归档上可直接重放并识别 P005 非最大值；旧判定为假通过。
3. 续修的 7 条真实误报/漏报（`004/009/043/050/044/065/051`）均已修复；当前重放通过 41/66，运行当时仍为 33/66。

41/66 是**离线重放结果**，不是新的真实模型通过率；归档中的真实调用次数、Token 与模型原始回答未改变，报告没有把重放结果写成重跑结果。
