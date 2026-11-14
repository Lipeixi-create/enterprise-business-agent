# Phase 5 开发完成报告（Evidence Chain + 报告生成）

> 生成时间：2026-10-31
> 范围：Phase 5.1 Evidence Ledger、5.2 Evidence Collector、5.3 Finding Engine、5.4 归因、5.5 Report Generator、5.6 Numeric Validator，以及 Agent/API 集成
> 原则：本文只写已经执行并留下记录的内容。**已实现 / 已自动化测试 / 真实 GLM 已验证 / 仅代码未验证 / 计划中** 分别标注；脚本化假客户端、真实模型调用和未执行项不混算。
> 环境说明：真实 GLM 验收入口为项目本地 SQLite 数据集（`enterprise_business_agent.db`，2024-01 至 2025-12）；`.env` 中的 PostgreSQL 连接使用 `eba_admin`，本地容器不存在该角色且认证失败，因此**没有修改 `.env`**，验收改用 `--database-url` 指向本地 SQLite。PostgreSQL 集成测试使用容器内已有超级用户创建的独立测试库，通过进程环境变量传入，未写入或打印凭据。**密钥轮换仍未确认**；本报告不声称已经轮换。

---

## 1. 完成状态

| 子阶段 | 状态 | 实现 | 自动化测试 | 真实 GLM |
| --- | --- | --- | --- | --- |
| 5.1 Evidence Ledger | ✅ 已实现 | `agent_evidence` 持久化、run/tool_call 关联、指纹去重、事务与并发处理 | 10 条 | 已通过五场景间接验证（证据落库） |
| 5.2 Evidence Collector | ✅ 已实现 | 工具结果自动转换、状态映射、原始结果保留、失败/无数据不伪造 | 23 条 | 已通过五场景间接验证 |
| 5.3 Finding Engine | ✅ 已实现 | 七类 finding、证据引用、规则置信度、限制与验证建议 | 19 条 | 已通过五场景间接验证 |
| 5.4 可解释归因 | ✅ 已实现并接入 finding | 绝对变化、相对变化、贡献率、零分母/缺失基准/残差/非可加 | 11 条 | 场景 A 的渠道/商品拆解已验证 |
| 5.5 Report Generator | ✅ 已实现 | 12 区块结构化报告、ValueReference、版本追加、可选 LLM 叙述 | 10 条 | 五场景均生成了结构化报告 |
| 5.6 Numeric Validator | ✅ 已实现 | 证据/发现引用、数字、单位、范围、口径、假设与因果检查 | 18 条 | 五场景报告均通过校验 |
| API | ✅ 已实现 | 证据列表/单条、发现、报告、版本、校验结果 | 6 条 | 场景通过脚本直接读取 Run Store |
| Agent 集成 | ✅ 已实现 | 工具后自动采集；结束时生成 finding/report/validation；失败不破坏 run | 5 条 | 五场景均走 Agent 循环 |

**总测试数：** Phase 5 专项 103 条；全量 pytest **414 passed, 10 skipped**；PostgreSQL 专项 3 条通过，原有 PostgreSQL 集成 6 条通过、1 条跳过；离线 NL/SQL Guard 评测 **75/75**；Agent 控制流评测 **6/6**。

**没有做的事：** 未实现完整 RLS/列级权限、人工审批状态机、LangGraph checkpointer、run 按步回放接口、毛利率桥（price/cost/mix）、10 份报告人工评分、Langfuse/OTel exporter。以上不属于 Phase 5 最小闭环，列在第 15 节。

---

## 2. 架构与数据流

Phase 5 没有改变“一个统一经营分析 Agent”的架构，也没有让模型接触数据库或 SQL。新增的是工具执行后的证据落库和结束时的报告生成：

```text
POST /api/v1/agent/runs
        │
        ▼
RunManager（run_id、状态、审计、取消、幂等）
        │
        ▼
AgentRuntime（LangGraph：decide ⇄ act）──► LLM（只返回工具调用或最终文本）
        │
        ▼
ToolRegistry ─► 授权 ─► AnalysisGateway ─► Semantic Layer/QueryCompiler
        │                                      │
        │                                      ▼
        │                         SemanticQueryGuard → SafeQueryExecutor（只读）
        ▼
Phase5Pipeline.record_tool_result
        │  Collector：工具名/参数/原始 ToolResult
        ▼
EvidenceLedger（agent_evidence）  ← 与 run_id、tool_call_id 关联
        │
        │ run 结束（完成、预算耗尽、超时、失败均可）
        ▼
FindingEngine（规则）──► FindingStore（agent_findings）
        │
        ▼
ReportGenerator（确定性骨架 + 可选 LLM 叙述）
        │
        ▼
NumericValidator（本次 run 的证据/发现）
        │
        ├─ 通过：agent_reports(status=validated)
        └─ 失败：结构化 issues；有限修正后回退确定性报告
```

**关键边界：**

- 证据表、finding 表、报告表存放在 Run Store 数据库（`RUN_STORE_URL`），不是业务数据库；业务数据库仍由应用以只读方式访问。
- 工具结果中的 `result` 原样保存，Collector 不重算、不四舍五入、不改写指标定义。
- 报告中的金额/数量/比例优先使用 `ValueReference` 指向证据字段；自由文本中的数字还要通过证据数字集合扫描。
- LLM 只生成叙述；报告结构、finding、归因和数字由确定性代码产生。LLM 叙述不通过校验时直接回退，不会降低规则。
- Phase 5 的失败是“软失败”：Agent run 继续完成，warning 中记录 `证据账本或报告生成失败`，不把分析结果整体丢弃。

---

## 3. 新增与修改文件

### 3.1 新增

| 文件 | 作用 |
| --- | --- |
| `src/enterprise_business_agent/evidence/__init__.py` | Phase 5 公共导出 |
| `src/enterprise_business_agent/evidence/contracts.py` | Evidence、Finding、ValueReference、Conclusion、ReportDocument、ValidationResult 等 Pydantic 契约 |
| `src/enterprise_business_agent/evidence/schema.py` | `agent_evidence`、`agent_findings`、`agent_reports` 表与幂等 `create_schema` / `drop_schema` |
| `src/enterprise_business_agent/evidence/ledger.py` | Evidence Ledger 读写、按 run 查询、`(run_id, fingerprint)` 去重、并发插入处理 |
| `src/enterprise_business_agent/evidence/collector.py` | ToolResult → EvidenceRecord；工具类型/状态/时间/粒度/来源/口径版本映射 |
| `src/enterprise_business_agent/evidence/findings.py` | Finding Engine 与 FindingStore；事实/变化/异常/贡献/相关/假设/建议 |
| `src/enterprise_business_agent/evidence/pipeline.py` | `Phase5Pipeline`：采集、finding、报告的统一编排 |
| `src/enterprise_business_agent/evidence/report.py` | 报告生成、报告版本存储、可选 LLM 叙述与确定性回退 |
| `src/enterprise_business_agent/evidence/validator.py` | 独立 Numeric Validator |
| `src/enterprise_business_agent/analysis/attribution.py` | 归因：绝对变化、相对变化、贡献率、残差/缺失/非可加状态 |
| `scripts/migrate_phase5.py` | 对现有 Run Store 幂等创建 Phase 5 表 |
| `scripts/run_phase5_acceptance.py` | 五个业务场景的真实 GLM 验收入口（不打印 key） |
| `tests/test_evidence_ledger.py` | 证据表、字段、去重、并发、状态、隔离 |
| `tests/test_evidence_collector.py` | 工具映射、状态、原始值、重复调用、维度 |
| `tests/test_findings.py` | finding 类型、证据引用、置信度、相关性措辞、归因接入 |
| `tests/test_attribution.py` | 绝对/相对/贡献率、零分母、残差、非可加 |
| `tests/test_report_generator.py` | 12 区块、引用、版本、LLM 回退、无证据报告 |
| `tests/test_numeric_validator.py` | 数字、单位、范围、口径、假设、因果、方向 |
| `tests/test_phase5_api.py` | 证据/发现/报告/版本/校验 API、权限隔离、异步 run |
| `tests/test_phase5_agent_integration.py` | Agent 持久化、无数据、失败工具、预算耗尽、软失败 |
| `tests/test_phase5_postgres.py` | PostgreSQL 表、去重、finding/report 持久化 |

### 3.2 修改

| 文件 | 修改 |
| --- | --- |
| `src/enterprise_business_agent/agent/graph.py` | 工具结果后调用 Phase 5 采集；结束时调用 finalize；传递 `stop_reason`；Phase 5 失败隔离；提示词增加“无数据支持时明确拒绝预测未来期间”，版本升至 `phase5-2026-10-31.1` |
| `src/enterprise_business_agent/agent/tools.py` | 修正指标摘要：维度拆分结果不再被描述成时间序列，单点结果不再写成“从 X 到 X”，空值不再输出为 “None”；`ContributionArgs` 增加期间合法性/重叠校验；校验错误去掉不可序列化的 `ctx`，保证 ToolResult 可写入 JSON |
| `src/enterprise_business_agent/llm/glm.py` | HTTP 调用增加整次请求的墙钟/单调时钟双截止时间，使用不依赖线程锁超时的轮询，避免提供方逐字节返回时读取超时被不断重置 |
| `src/enterprise_business_agent/agent/contracts.py` | Agent 结果中的 `finding_ids`、`report_id`、`report_version`、`report_status`、`report_validation_passed` |
| `src/enterprise_business_agent/api/routes.py` | Phase 5 查询端点 |
| `src/enterprise_business_agent/api/app.py` | Phase 5 存储/管线装配与不可用时的 503 |
| `src/enterprise_business_agent/api/run_manager.py` | 异步 run 使用 RunManager 的 `run_id` 调用 runtime，保证证据/报告与 run 一致 |
| `src/enterprise_business_agent/config.py` | Phase 5 开关、叙述开关、修正次数、表自动创建 |
| `src/enterprise_business_agent/__init__.py` / `pyproject.toml` | 版本 `0.5.0` |
| `README.md`、`docs/README.md`、`docs/03-agent-runtime.md`、`docs/04-security-and-governance.md`、`docs/05-development-roadmap.md`、`docs/06-evaluation-and-observability.md` | Phase 5 状态、能力、验收与限制 |

---

## 4. 迁移与表结构

Run Store 在 Phase 4 已用 `MetaData.create_all` 管理（没有 Alembic）。Phase 5 沿用同一机制：启动时 `create_schema(engine)` 幂等建表；已有 `agent_runs` / `agent_audit_events` 的数据库不会被删除或重建。`scripts/migrate_phase5.py` 可单独执行升级，并打印实际创建的表名（不打印连接凭据）。

### 4.1 `agent_evidence`

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `evidence_id` | VARCHAR(128) PK | 证据 ID |
| `run_id` | VARCHAR(64) NOT NULL, index | 关联 run |
| `tool_call_id` | VARCHAR(128) NOT NULL, index | 关联一次工具调用 |
| `evidence_type` | VARCHAR(32) | comparison/contribution/ranking/trend/anomaly/metric_value/metric_definition/catalog |
| `domain` / `metric_code` | VARCHAR | 业务域与指标 |
| `metric_definition_version` | VARCHAR(32) | 指标口径版本 |
| `time_range_start` / `time_range_end` | VARCHAR(10) | 当前期 |
| `baseline_period` / `baseline_time_range_start` / `baseline_time_range_end` | VARCHAR | 基准期 |
| `grain` | VARCHAR(16) | month/quarter/year |
| `filters` / `dimensions` / `query_parameters` | TEXT(JSON) | 筛选、维度、工具参数 |
| `result` | TEXT(JSON) | 工具原始结果（原样） |
| `result_summary` | TEXT | 可读摘要 |
| `unit` / `source_tables` / `calculation_method` | VARCHAR/TEXT | 单位、来源表、确定性方法 |
| `status` | VARCHAR(24) | valid/insufficient_data/not_calculable/failed/rejected |
| `error_code` / `error_message` | VARCHAR/TEXT | 失败原因 |
| `traceable` | BOOLEAN | 是否可追溯 |
| `fingerprint` | VARCHAR(64) | `sha256(tool+arguments+run)` |
| `created_at` | VARCHAR(40) | UTC 时间 |

约束：`UNIQUE(run_id, fingerprint)`；索引 `run_id`、`tool_call_id`、`(run_id, evidence_type)`。

### 4.2 `agent_findings`

字段：`finding_id`(PK)、`run_id`、`title`、`domain`、`finding_type`、`severity`、`statement`、`evidence_ids`(JSON)、`confidence`、`confidence_basis`、`explanation`、`limitations`(JSON)、`suggested_validation`(JSON)、`created_at`；索引 `(run_id, finding_type)`。

### 4.3 `agent_reports`

字段：`report_id`(PK)、`run_id`、`report_version`、`status`(validated/rejected)、`report_json`、`validation_json`、`created_at`；唯一约束 `(run_id, report_version)`，报告只追加不覆盖。

---

## 5. Evidence Ledger 示例

以下为场景 A 真实运行 `p5-A-420f81bc47` 中的一条比较证据（已截断 `result.explain` 中的 SQL）：

```json
{
  "evidence_id": "p5-A-420-call_-7121729124560794870-e1",
  "run_id": "p5-A-420f81bc47",
  "tool_call_id": "call_-7121729124560794870",
  "evidence_type": "comparison",
  "domain": "Sales",
  "metric_code": "sales_amount",
  "metric_definition_version": "2.0.0",
  "time_range_start": "2025-12-01",
  "time_range_end": "2025-12-01",
  "baseline_period": "2025-11-01",
  "grain": "month",
  "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
  "dimensions": {"region": "EC"},
  "result": {
    "status": "ok",
    "metric": "sales_amount",
    "label": "销售额",
    "unit": "CNY",
    "current_period": "2025-12",
    "baseline_period": "2025-11",
    "items": [{
      "current_value": "18129454.190000",
      "baseline_value": "28548840.500000",
      "absolute_change": "-10419386.310000",
      "change_rate": "-0.364967"
    }]
  },
  "unit": "CNY",
  "source_tables": ["sales_fact"],
  "calculation_method": "deterministic period comparison",
  "status": "valid",
  "traceable": true
}
```

验证点：

- `run_id` 与 `tool_call_id` 均可回到 Agent run/工具调用；
- `result` 是工具实际返回值，报告数字可直接与它比对；
- 同一 run 内相同工具+参数+时间/筛选的重复调用由 `(run_id, fingerprint)` 去重；不同时间范围或筛选不会合并。

---

## 6. Evidence Collector 示例

Collector 在每次工具执行后运行，不重新查询数据库。核心规则：

| 工具 | evidence_type | 计算/来源 |
| --- | --- | --- |
| `query_metric` | `metric_value` | 确定性指标聚合 |
| `compare_periods` | `comparison` | 确定性期间比较 |
| `top_n` | `ranking` | 确定性排名 |
| `contribution` | `contribution` | 确定性加性贡献 |
| `trend` | `trend` | 确定性趋势 |
| `anomaly_scan` | `anomaly` | 确定性异常规则 |
| `describe_metric` | `metric_definition` | 语义层定义 |
| `list_metrics` | 不产生证据 | 只暴露指标目录 |

状态映射：

```text
ok                  → valid
no_data / insufficient_data → insufficient_data
not_calculable      → not_calculable
timeout / execution_error → failed
其他拒绝/未知错误     → rejected
```

真实失败示例（自动化测试）：调用不存在的指标时，证据行保留 `status=rejected`、`error_code=unsupported` 和错误消息，`traceable=false`；Finding Engine 不会把它变成事实，报告只能把它列为无效证据引用。无数据查询保存 `status=insufficient_data`，不生成 finding。

时间/粒度推导只从工具参数和返回的时间点推断；例如 `compare_periods(kind=mom, period=2025-12-01)` 得到 `grain=month`，`describe_metric` 不带时间时保持 `null`。Collector 不会为了让字段好看而补造时间或来源。

---

## 7. Finding Engine 示例

真实场景 A 的渠道贡献 finding：

```json
{
  "finding_id": "F-413dd74bf387e602",
  "run_id": "p5-A-420f81bc47",
  "title": "销售额 贡献拆解",
  "finding_type": "contribution",
  "severity": "primary",
  "statement": "销售额 在 2025-12-01 至 2025-12-01 相对基准期：绝对变化 -10419386.310000，相对变化率 -0.3649670574186716970169068688；按 channel 的贡献拆解：ECOM 贡献 -3451443.870000（贡献率 0.331252）；DIST 贡献 -2628527.520000（贡献率 0.252273）；DIRECT 贡献 -2249420.710000（贡献率 0.215888）。",
  "evidence_ids": ["p5-A-420-call_-7121738504769364797-e1"],
  "confidence": 0.85,
  "confidence_basis": "确定性加性贡献拆解，绝对变化、相对变化与贡献率分别计算",
  "limitations": ["贡献合计与总变化存在残差 -2089994.210000，维度成员可能不完整"],
  "suggested_validation": ["核对贡献合计与总变化是否一致", "对最大贡献维度继续下钻"]
}
```

规则约束：

- `fact` / `change` / `contribution` / `anomaly` 来自确定性工具结果，置信度由规则给出（如 0.85/0.9）；
- `correlation` 使用“可能相关”“需要进一步验证”，并明确“仅为统计关联，不构成因果关系”；
- `hypothesis` 是待验证假设，不能出现在“事实”结论中；
- `recommendation` 只作为建议，`requires_approval=true`；
- 没有有效证据时不产生 finding；证据不足时 confidence 降低并写 limitations；
- 归因残差、零分母、缺失基准会被写入 limitations 或使 finding 不生成。

---

## 8. 归因示例

`analysis/attribution.py` 直接消费 `analysis.engine` 的 `ContributionResponse`，不重新查 SQL：

```json
{
  "metric": "sales_amount",
  "dimension": "channel",
  "absolute_change": "-10419386.310000",
  "relative_change": "-0.3649670574186716970169068688",
  "allocatable": true,
  "items": [
    {"dimension_value": "ECOM", "contribution": "-3451443.870000", "contribution_rate": "0.331252"},
    {"dimension_value": "DIST", "contribution": "-2628527.520000", "contribution_rate": "0.252273"},
    {"dimension_value": "DIRECT", "contribution": "-2249420.710000", "contribution_rate": "0.215888"}
  ],
  "checks": ["贡献合计与总变化存在残差 -2089994.210000，维度成员可能不完整"],
  "missing_data": []
}
```

实现区分：

- **绝对变化**：`total_change`；
- **相对变化**：`total_change / total_baseline`；基准期为 0 或缺失时返回 `None`，并记录 `baseline_total` 缺失；
- **贡献率**：各维度 `contribution / total_change`，由分析引擎确定性计算；
- **残差**：贡献合计与总变化不一致时记录，并提示维度成员可能不完整；
- **非可加指标**：`allocatable=false`，返回 `additive_metric` 缺失，不输出伪贡献；
- **零分母/缺失基准/无 items**：返回 `INSUFFICIENT_DATA` 或明确 `reason`，不生成编造百分比。

归因结果已接入 Finding Engine；报告中的贡献结论引用证据中的原始金额和贡献率，`relative_change` 以 `@derived.relative_change` 结构化引用标出，Validator 可独立重算。

---

## 9. 完整报告示例

`ReportDocument` 的 12 个必需区块（外加 `narrative_mode` / `warnings` 两个运行元数据）：

```json
{
  "report_info": {
    "report_id": "...",
    "run_id": "p5-A-420f81bc47",
    "version": 1,
    "generated_at": "2026-10-31T...",
    "question": "分析华东 2025 年 12 月销售额下降的原因，并给出可验证的改进建议。",
    "principal_id": "key:acceptance",
    "role": "analyst",
    "provider": "glm",
    "model": "glm-5.3-flash"
  },
  "objective": "分析华东 2025 年 12 月销售额下降的原因，并给出可验证的改进建议。",
  "executive_summary": "销售额 2025-12 为 18129454.190000，基准 2025-11 为 28548840.500000，绝对变化 -10419386.310000，变化率 -0.364967。",
  "conclusions": [{
    "statement": "销售额 2025-12 为 18129454.190000...",
    "finding_ids": ["F-875737cae55e4d62"],
    "evidence_ids": ["p5-A-420-call_-7121729124560794870-e1"],
    "certainty": "fact",
    "value_refs": [{
      "evidence_id": "p5-A-420-call_-7121729124560794870-e1",
      "field": "items[0].current_value",
      "value": "18129454.190000",
      "unit": "CNY"
    }]
  }],
  "key_metrics": [{
    "metric": "sales_amount",
    "label": "销售额",
    "period": "2025-12",
    "value": {"evidence_id": "p5-A-...", "field": "items[0].current_value", "value": "18129454.190000", "unit": "CNY"},
    "change": {"evidence_id": "p5-A-...", "field": "items[0].change_rate", "value": "-0.364967", "unit": "ratio"},
    "status": "valid"
  }],
  "domain_analysis": [{"domain": "Sales", "narrative": "...", "finding_ids": ["F-..."], "evidence_ids": ["p5-A-..."]}],
  "anomalies_and_contributions": [{"statement": "销售额按 channel 的贡献拆解...", "finding_ids": ["F-..."], "evidence_ids": ["p5-A-..."], "certainty": "fact"}],
  "cross_domain": [],
  "evidence_references": [{"evidence_id": "p5-A-...", "tool_call_id": "call_...", "evidence_type": "comparison", "metric": "sales_amount", "status": "valid", "source_tables": ["sales_fact"]}],
  "risks_and_uncertainty": ["跨部门关联仅为统计关系，不构成因果结论", "未控制共同驱动因素"],
  "recommendations": [{
    "action": "核查该指标变化期间的业务事件并确认影响范围",
    "rationale": "销售额 ... 变化率 -0.364967。",
    "finding_ids": ["F-875737cae55e4d62"],
    "evidence_ids": ["p5-A-420-call_-7121729124560794870-e1"],
    "expected_direction": "若当前结构延续，该指标下一期仍受同一因素影响；需用新数据验证",
    "risk": "统计贡献与关联不等同于因果；执行前需业务复核，系统不自动执行任何业务动作",
    "validation_metric": "同指标下一期变化",
    "requires_approval": true
  }],
  "next_validation": ["完成剩余分析步骤后重新生成报告", "对最大贡献维度继续下钻"],
  "data_scope": {
    "metric_codes": ["sales_amount"],
    "metric_definition_versions": ["2.0.0"],
    "time_ranges": [{"start": "2025-12-01", "end": "2025-12-01"}],
    "filters": [{"dimension": "region", "operator": "eq", "value": "EC"}],
    "grains": ["month"],
    "source_tables": ["sales_fact"]
  },
  "narrative_mode": "deterministic",
  "warnings": []
}
```

约束与实现：

- 结论必须引用 `finding_ids`，finding 必须引用本次 run 的 `evidence_ids`；
- 金额/数量/比例使用 `ValueReference`，`change_rate` 等单位标为 `ratio`，不冒充货币单位；
- 没有有效证据时，报告明确写“本次运行没有产生有效证据”，不输出空泛结论；
- 预算/时间/token/LLM 失败时，报告仍用已有证据生成，并在 `risks_and_uncertainty` 首位写入对应不完整说明（例如“模型响应超时，调查提前结束，证据可能不完整”），`next_validation` 首位提示“完成剩余分析步骤后重新生成报告”；
- 报告按 `(run_id, report_version)` 追加：同一 run 再次生成得到 version 2，version 1 保留可查。

---

## 10. Numeric Validator 示例

Validator 不调用 LLM，独立读取本次 run 的证据和 finding。检查项包括：

- `evidence_reference_exists` / `finding_reference_exists`：不存在的 ID；
- `value_reference_matches_evidence`：字段路径、数值、单位；
- `metric_card_period_within_evidence`：指标卡期间在证据范围内；
- `scope_filters_match_evidence` / `scope_time_range_matches_evidence`：报告数据范围与证据筛选/时间一致；
- `no_unsourced_numbers`：自由文本数字必须出现在证据数字集合中；
- `hypothesis_not_asserted_as_fact`：假设不能写成事实；
- `no_causal_overreach`：统计相关不能写成因果；
- `no_direction_contradiction`：文字方向与数值方向一致。

篡改示例（对真实 A 报告做副本，不修改已存报告）：

```text
conclusions[0].value_refs[0].value = "99999999"
executive_summary += " 模型补充：销售额下降 37%。"
```

校验结果：

```json
{
  "passed": false,
  "issues": [
    {"code": "value_mismatch", "location": "conclusions[0]", "message": "值 '99999999' 与证据 ... 'items[0].current_value'='18129454.190000' 不一致"},
    {"code": "unsourced_number", "location": "executive_summary", "message": "数字 '37' 无法在任何证据中找到"}
  ]
}
```

失败处理：

- 结构化错误写入 `validation_json`，报告状态为 `rejected`；
- LLM 叙述最多修正 `phase5_max_fix_attempts` 次（验收配置为 1）；
- 修正仍失败时使用确定性报告，保留失败记录与 warning，不关闭规则、不放宽阈值；
- 用户目标中的数字（例如问题里的“2025 年 12 月”）不视为报告数据声明；只扫描报告自身的结论/摘要/风险/建议/验证文本。

---

## 11. 五个业务场景验收

执行命令（2026-10-31，最终代码；仅进程环境，未修改 `.env`）：

```bash
# 实际执行：A/B/E 一批，C、D 分别执行
LLM_TIMEOUT_SECONDS=90 LLM_MAX_RETRIES=1 LLM_MAX_TOKENS=4000 \
AGENT_MAX_TOTAL_TOKENS=120000 \
.venv/bin/python -u scripts/run_phase5_acceptance.py \
  --scenarios ABE \
  --database-url "sqlite:///./enterprise_business_agent.db" \
  --max-tool-calls 8 --max-seconds 600 \
  --output /tmp/phase5_ABE_final2.json

LLM_TIMEOUT_SECONDS=90 LLM_MAX_RETRIES=1 LLM_MAX_TOKENS=4000 \
AGENT_MAX_TOTAL_TOKENS=120000 \
.venv/bin/python -u scripts/run_phase5_acceptance.py \
  --scenarios C \
  --database-url "sqlite:///./enterprise_business_agent.db" \
  --max-tool-calls 8 --max-seconds 600 \
  --output /tmp/phase5_C_final2.json

LLM_TIMEOUT_SECONDS=90 LLM_MAX_RETRIES=1 LLM_MAX_TOKENS=4000 \
AGENT_MAX_TOTAL_TOKENS=120000 \
.venv/bin/python -u scripts/run_phase5_acceptance.py \
  --scenarios D \
  --database-url "sqlite:///./enterprise_business_agent.db" \
  --max-tool-calls 8 --max-seconds 600 \
  --output /tmp/phase5_D_final.json
```

A/B/E 批次退出码 0；C、D 单独执行退出码均为 0。下表只列最终代码上的运行结果，不混入早期修复前的运行。

| 场景 | run_id | 状态 | 工具/证据 | findings | 报告 | 校验 | 叙述 | tokens (输入/输出) | 模型延迟 | 总耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A 销售额下降归因 | `p5-A-a13eb63b0b` | completed | 8 / 8 | 23 | validated | 通过 | deterministic | 58693 / 5140 | 136.5s | 242.4s |
| B 营销 ROI | `p5-B-83c1286606` | completed_with_warnings | 8 / 8 | 9 | validated | 通过 | deterministic | 58348 / 10905 | 309.9s | 471.2s |
| C 库存积压与资金占用 | `p5-C-3e58015ea6` | completed | 8 / 8 | 8 | validated | 通过 | llm | 57911 / 9701 | 306.6s | 338.6s |
| D 五域综合经营 | `p5-D-ce516efb96` | completed | 8 / 8 | 8 | validated | 通过 | llm | 56681 / 4280 | 133.0s | 156.6s |
| E 2026 预测 | `p5-E-6a2f30aef1` | completed | 1 / 1 | 1 | validated | 通过 | llm | 12433 / 983 | 43.9s | 89.0s |

### A. 华东 2025-12 销售额下降

- `compare_periods` 返回销售额 18,129,454.19，基准 2025-11 为 28,548,840.50，绝对变化 −10,419,386.31，变化率 **−0.364967**。
- 渠道贡献：ECOM −3,451,443.87（贡献率 0.331252）、DIST −2,628,527.52（0.252273）、DIRECT −2,249,420.71（0.215888）；产品贡献：P005 −2,251,384.00、P007 −1,733,885.89、P004 −1,670,641.45。
- 归因修复验证：本次 `contribution` 的 `baseline_end=2025-11-01` 与 `current_start=2025-12-01` 不再重叠，相对变化与 `compare_periods` 完全一致（−36.4967%），不再是早期缺陷中的 −61.16%。
- 报告通过数值校验；模型在达到 8 次工具调用上限后收束，报告仍生成，最终答复开头为“工具预算已用完，基于已验证数据给出结论”。渠道贡献仍存在约 209 万的未分配残差，finding 的 limitations 已标注“维度成员可能不完整”。
- 报告级 warning 为 `模型叙述生成失败，已回退到确定性文本`；即模型叙述调用失败（未返回可用 JSON/超时），不是数值校验失败。确定性报告本身通过校验。

### B. 2025Q4 营销 ROI

- 8 次工具调用覆盖整体月度 ROI、Q4/Q3 环比、活动排名、分活动投入与异常扫描；报告 validated。
- 最终答复按“事实 / 统计关联 / 假设”三段组织：事实仅复述工具返回值；统计关联明确写“非因果证明”；假设逐条标注“未验证”。本次没有早期运行的截断问题。
- warning：`模型结论包含无法在工具证据中直接核对数字：0.2`。该数字是 `anomaly_scan` 的阈值参数，不属于业务结果；报告本身仍通过校验。它说明当前自由文本数字检查只对照证据结果、不包含工具参数，属于已知限制。
- 叙述模式为 deterministic：报告级 warning 为 `模型叙述未通过数值校验，已回退到确定性文本：executive_summary 中的数字 '10'/'12'/'11' 无法在任何证据中找到`。这三个数字来自日期月份（2025-10/12/11），不是业务数值；当前自由文本扫描没有屏蔽日期中的月份，属于误报。报告仍使用确定性文本，未降低校验规则。

### C. 2025Q4 库存积压与资金占用成本

- 库存天数排名：P004 75.667078 天、P005 69.484810 天、P007 25.550924 天；期末库存量 P004 41,350、P005 22,048、P003 16,130；库存周转率从 34.493311 降至 28.108129，库存天数从 27.831517 升至 34.153489。
- 资金占用成本：报告明确写“给定事实中未包含资金占用成本测算所需的参数（如利率或资金成本率），无法据现有事实对该成本进行量化评估”，没有编造金额或自行做二次乘法。
- 空值处理：单位采购成本的 `null` 点位现在写作“不可计算”，不再出现“首条值 None”；本次报告的 findings 中已确认不含 “None”。

### D. 五域综合经营

- 8 个只读工具覆盖 Sales、Finance、Marketing、Inventory、HR：销售额 300,394,683.69（Q3 286,559,001.33，环比 +0.048282）、净利润 64,875,265.76、毛利率 0.293314、营销投入 2,720,732.11、ROI 1.985710、库存周转率 100.090486、人力成本 18,098,380.00。
- 报告按域汇总，未把跨域并列观察写成因果；最终答复明确列出未查询的区域/渠道/产品拆分、同比、预算等，并拒绝 2026 年及以后的前瞻推断。

### E. 2026 年员工满意度与离职率预测（必须拒绝）

- 只调用 1 次 `trend`，得到 turnover_rate 在 2025-01-01 至 2025-12-01 的方向为 decline；没有调用任何未来的“预测”工具。
- 最终答复原文包含：
  - “**无法提供预测**：无法预测 2026 年员工满意度和离职率的变化趋势。当前工具不支持任何未来期间的预测或外推，只能基于已覆盖的历史数据说明事实。”
  - “**员工满意度**不在当前可用指标范围内……该项数据来源缺失。”
  - “2026 年离职率是否会延续 2025 年的下降方向**未经验证**；如需评估，需要补充未来的实际数据或专门的人力资源预测数据源。”
- 验收脚本 `prediction_refusal_signal=True`，退出码 0。

### 验收结论

- 五个场景均产生结构化报告并通过 Numeric Validator；报告文本没有未溯源的业务数字，没有因果越界，也没有未来预测。
- A 的贡献率与期间比较一致；C 没有编造资金成本；E 明确拒绝预测并说明缺失数据。
- 已知非阻断问题：B 的自由文本阈值数字触发 warning；A/B 的模型叙述回退为确定性文本；B 第一次尝试遇到提供方 `llm_unavailable`，重试后在本批中完成（最终 run 为 `p5-B-83c1286606`）。


---

## 12. 自动化测试与回归结果

执行命令与结果（2026-10-31，最终代码）：

```bash
.venv/bin/python -m pytest -q
# 414 passed, 10 skipped

.venv/bin/python -m pytest \
  tests/test_evidence_ledger.py tests/test_evidence_collector.py \
  tests/test_findings.py tests/test_attribution.py \
  tests/test_report_generator.py tests/test_numeric_validator.py \
  tests/test_phase5_api.py tests/test_phase5_agent_integration.py -q
# 103 passed

EBA_TEST_POSTGRES_URL=... .venv/bin/python -m pytest tests/test_phase5_postgres.py -q
# 3 passed

EBA_TEST_POSTGRES_URL=... .venv/bin/python -m pytest tests/test_postgres_integration.py -q
# 6 passed, 1 skipped（只读角色 URL 未配置）

PYTHONPATH=src .venv/bin/python scripts/run_nl_evals.py
# 75/75 passed, 0 failed

PYTHONPATH=src .venv/bin/python scripts/run_agent_evals.py
# 6/6 passed, 0 failed
```

说明：

- 全量测试的 10 个 skip 包括 3 条 PostgreSQL 专项（未设置 `EBA_TEST_POSTGRES_URL`）和原有环境相关跳过；使用独立 PostgreSQL 测试库后，PostgreSQL 专项 3/3、原有集成 6/7 通过。
- 10 条 skip 不等于失败，也不计入通过；本节只报告实际执行结果。
- 上述 75/75 和 6/6 都是**离线确定性/脚本化假规划器**结果，不是真实模型质量结果；真实 GLM 结果见第 13 节。

---

## 13. 真实 GLM 验收结果

本节只记录真实提供方调用。脚本化假客户端、离线评测和单元测试的结果在第 12 节，不与本节混算。

### 13.1 调用环境

- 提供方：`glm`；模型：`glm-5.3-flash`（由运行输出实际报告，不是文档推断）。
- 业务数据源：项目本地 SQLite `enterprise_business_agent.db`（2024-01 至 2025-12）。原因见第 14 节：`.env` 中的 PostgreSQL 连接使用 `eba_admin`，本地容器认证失败；未修改 `.env`。
- 运行参数：`LLM_TIMEOUT_SECONDS=90`、`LLM_MAX_RETRIES=1`、`LLM_MAX_TOKENS=4000`、`AGENT_MAX_TOTAL_TOKENS=120000`、`--max-tool-calls 8`、`--max-seconds 600`；这些只通过进程环境传入。
- API Key 从既有环境变量/本地 dotenv 读取；脚本只打印 `provider` 和 `model`，不打印、不复制、不写回密钥。

### 13.2 实际运行汇总

| 指标 | 结果 |
| --- | --- |
| 场景数 | 5（A–E） |
| 报告状态 | 5/5 `validated`，0 份 `rejected` |
| 数值校验 | 5/5 通过 |
| 工具调用 / 证据 | 33 / 33（A 8、B 8、C 8、D 8、E 1） |
| finding 总数 | 49（A 23、B 9、C 8、D 8、E 1） |
| 输入 / 输出 tokens | 244,066 / 31,009（五次运行合计） |
| 模型延迟 | 136.5s / 309.9s / 306.6s / 133.0s / 43.9s |
| 总耗时 | 242.4s / 471.2s / 338.6s / 156.6s / 89.0s |
| E 拒绝预测信号 | `True`，退出码 0 |

每条证据都保存了 `run_id`、`tool_call_id`、工具名、参数、原始 `result`、来源表、指标口径版本和状态；报告中的金额/比例使用 `ValueReference` 指向证据字段。可直接用 run_id 在 Run Store 中回查：

```text
p5-A-a13eb63b0b
p5-B-83c1286606
p5-C-3e58015ea6
p5-D-ce516efb96
p5-E-6a2f30aef1
```

### 13.3 可核对的关键数字

- A：销售额 `18129454.190000`（2025-12）对 `28548840.500000`（2025-11），变化率 `-0.364967`；渠道贡献 ECOM `-3451443.870000`（0.331252）、DIST `-2628527.520000`（0.252273）、DIRECT `-2249420.710000`（0.215888）。
- B：Q4 ROI `1.985710` 对 Q3 `1.967505`，变化率 `0.009253`；活动排名 C005 `4.971426`、C007 `1.734261`、C006 `0.572464`；`campaign_revenue` 的贡献分解返回 `insufficient_data`，报告没有补造活动级归因。
- C：库存天数 P004 `75.667078`、P005 `69.484810`、P007 `25.550924`；库存周转率 `34.493311 → 28.108129`；报告明确资金成本率缺失，未给出金额。
- D：销售额 `300394683.690000` 对 Q3 `286559001.330000`（+0.048282）、净利润 `64875265.760000`、毛利率 `0.293314`、营销投入 `2720732.110000`、ROI `1.985710`、库存周转率 `100.090486`、人力成本 `18098380.000000`。
- E：turnover_rate 2025 年方向为 `decline`；员工满意度指标不存在，数据来源缺失。

### 13.4 真实调用中暴露并已修复的问题

1. **GLM 响应长期不返回**：逐字节响应使 `httpx` 读取超时不断重置；单次 `join` 和 0.5 秒 `join` 轮询仍在真实调用中分别出现 20 分钟以上和约 18 分钟阻塞。最终改为 `sleep(0.05)` 轮询 + 单调时钟/墙钟双截止时间，并有本地黑洞/逐字节服务测试。
2. **归因基准期重叠**：早期 A 运行把 2025-11 与 2025-12 合并为基准，得到 −61.16%；现由 `ContributionArgs` 拒绝重叠期间，最终 A 为 −36.4967%。
3. **维度结果被写成时间序列**：B 早期报告把按 campaign 的数据点描述成“从 2025-10 到 2025-12”；已修正摘要。
4. **空值写成 None**：C 早期的“首条值 None”已改为“不可计算”。
5. **无依据的未来预测**：E 早期最终答复带有“若延续趋势”的外推；提示词增加明确拒绝规则后，最终答复改为“无法预测 2026 年……不做趋势外推”。

### 13.5 不能从本节得出的结论

- 这些运行使用本地 SQLite 业务数据，**不是** PostgreSQL 业务库上的真实 Agent 验收。
- 五次运行是单模型、单轮次结果，不构成模型稳定性或多模型对比；`glm-5.3-flash` 的实际质量仍需人工评分和更大样本评测。
- 没有完成提供方控制台中的密钥轮换；本报告不声称已经轮换。
- B 的 warning 和 A/B 的叙述回退说明：报告校验通过不等于叙述文本完美，仍需人工审阅。


---

## 14. 已知缺陷与风险

1. **`.env` 中的 PostgreSQL 连接不可用。** `DATABASE_URL` 指向 `eba_admin`，本地容器没有该角色且密码认证失败。没有修改 `.env`；真实验收改用本地 SQLite，PostgreSQL 测试使用独立测试库。部署前必须修复连接配置并重新验证。
2. **密钥轮换未确认。** 无法访问提供方控制台，本报告不声称已经轮换；继续真实调用或部署前必须在控制台完成轮换。
3. **归因残差。** 场景 A 的渠道贡献存在约 209 万的未分配残差，报告已在 limitations 中标注“维度成员可能不完整”；这不是数值错误，但说明渠道维度不能完全解释总变化，需继续下钻。
4. **finding 双证据未强制。** 一条高质量证据可以形成 finding；需要更强结论时应在后续阶段增加“主 finding 至少两条独立证据”的策略。
5. **相关性不等于因果。** 跨域 finding 只能使用“可能相关/需要进一步验证”，并注明统计关联；没有机制证据、时间先后和交叉验证时不能写因果。
6. **报告单位语义。** 验收中发现并修复了 `change_rate` 被误标为货币单位的问题：现在比例类引用标为 `ratio`，Validator 只对真正的 rate 字段放行该单位；金额字段标 `ratio` 仍会被拒绝。
7. **LLM 传输延迟与不确定性（已再次加固，仍受提供方影响）。** 验收中遇到真实 GLM 响应逐字节返回，`httpx` 的读取超时被每个字节重置，进程长时间阻塞。第一次修复只用单次 `Thread.join(timeout)`，在真实网络下仍出现过 20 分钟以上不返回；第二次改为每 0.5 秒 `join` 的轮询循环，但在报告叙述阶段又出现约 18 分钟不返回，调用栈停在 `Thread.join` 的线程状态锁上。现在改为不依赖 `join` 超时的 `sleep(0.05)` 轮询，同时使用单调时钟和墙钟截止时间（`llm/glm.py`）；用本地黑洞服务和逐字节服务手工复现时均在 2 秒内抛 `ReadTimeout`，仓库内另有墙钟期限单元测试。超时后按 `llm_timeout` 结束，仍生成带不完整说明的报告。最终验收把 `LLM_TIMEOUT_SECONDS` 设为 90 秒、`LLM_MAX_RETRIES` 设为 1（仅进程环境，不改 `.env`）。
8. **软失败边界。** Phase 5 落库或报告失败只记 warning，不阻断 Agent 结果；这保证可用性，但监控必须关注该 warning，否则可能长期无报告而不被发现。
9. **权限仍是角色级。** Phase 5 端点复用 run 归属和 `metrics:read`，不是 RLS/列级权限；知道 run_id 的其他 principal 仍返回 404，但管理员级隔离依赖 Phase 6。
10. **未做真实 PostgreSQL 业务数据验收。** 自动化测试覆盖了 PostgreSQL 表与业务 schema，但五个业务场景的真实模型验收使用 SQLite；PostgreSQL 上的完整 Agent 验收未执行。
11. **归因基准期重叠（验收中发现并已修复）。** 场景 A 中模型把 `baseline_end=2025-12-01` 与 `current_start=2025-12-01` 同时传入，工具把 11 月和 12 月合计当作基准，得到 −61.16% 的错误相对变化；同一次比较的 `compare_periods` 正确值为 −36.4967%。现已在 `ContributionArgs` 参数校验阶段拒绝基准期倒置或与当前期重叠，并补充回归测试；修复后的真实运行结果见第 11、13 节。
12. **未来预测拒绝仍是提示词约束。** 场景 E 要求模型拒绝无数据支持的 2026 年预测。本次在 Agent 提示词中新增了明确规则（版本 `phase5-2026-10-31.1`），但 Numeric Validator 目前没有“拒绝外推”的确定性规则，因此该行为仍依赖模型遵循提示词；如后续要求硬保证，需要在报告/最终答复层面增加外推检测。
13. **空值摘要（验收中发现并已修复）。** 场景 C 的单位采购成本部分点位为 `null`，早期摘要直接写成“首条值 None”，并进入 fact finding。现已将空值描述为“不可计算”，全空时明确写“所有数值均不可计算”，并补充两条回归测试；这属于措辞修复，不改变任何数值。
14. **日期月份被当作无来源数字（未修复）。** 场景 B 的模型叙述因 `2025-10`/`2025-12`/`2025-11` 中的 `10`/`12`/`11` 被判为“无法在证据中找到”而回退。误报导致叙述质量下降，但不会放过真实的无来源业务数字；修复方向是扫描前屏蔽日期/期间片段。未在本次改动，以免在验收后引入新规则。

---

## 15. 未完成事项与 Phase 6 建议

### 15.1 未完成（明确不属于本阶段）

- 完整 RBAC/RLS、列级权限、多租户隔离；
- 人工审批 API/状态机与审批审计（当前只有 `requires_approval` 标记）；
- LangGraph checkpointer、断点恢复、多进程任务队列；
- run 按步回放接口/UI；
- 毛利率桥（price/cost/mix）、费用率分解；
- 10 份报告人工评分、50–100 题扩展评测、多模型/多种子稳定性对比；
- OTel exporter、Langfuse、成本看板；
- 前端 Dashboard、MCP、生产部署与告警。

### 15.2 Phase 6 建议顺序

1. **先修环境与密钥**：修复 PostgreSQL 连接配置，完成提供方 key 轮换，重新跑 PostgreSQL + 真实 GLM 五场景。
2. **审批闭环**：把 `requires_approval` 接入持久化审批状态机，记录审批人、意见、证据和版本，禁止绕过；仍然不自动执行经营动作。
3. **权限增强**：RLS/列级权限与 run/证据/报告的细粒度授权，补权限矩阵测试。
4. **稳定性**：checkpointer/续跑、异步队列、长问题分段策略、超时与 token 预算的可配置化。
5. **评测**：50–100 题业务评测、10–20 份报告人工评分、归因正确性抽检、真实 PostgreSQL Agent 验收。
6. **可观测性**：OTel exporter/Langfuse、报告校验失败率、证据覆盖率、成本与延迟告警。

---

## 附录：状态标签说明

- **已实现**：代码存在并通过自动化测试；
- **真实 GLM 已验证**：使用配置的 GLM 提供方实际发起请求并留下 token/延迟记录；
- **仅代码未验证**：代码路径存在，但没有对应真实环境执行记录；
- **计划中**：未实现，不写成完成；
- **未执行**：明确标注，不计入通过率。
