# Phase 9.2 真实 PostgreSQL 验收报告

生成时间：2026-11-04

> 本报告只记录真实 PostgreSQL 上的执行结果。所有测试均连接本地 PostgreSQL 17.11 实例，没有使用 SQLite 代替，也没有使用 Mock 数据库。LLM 使用仓库内脚本化测试替身，因此本报告不构成真实模型质量结论。

## 1. 结论

Phase 9.2 在真实 PostgreSQL 上完成了运行时权限、数据持久化、Checkpoint 恢复、并发控制、Lease/Fencing、幂等、服务重启恢复、用户/区域隔离、越权访问和数据库异常场景验收。

- **真实 PostgreSQL 测试：30 项全部通过**（`30 passed, 736 deselected`，4.12s；该次执行早于新增的 11 个评测器回归测试）。
- `eba_runtime` 只能对 `agent_*` 表执行 DML，对业务表无 SELECT 权限，也不能执行 DDL。
- `eba_readonly` 可以查询业务表，但不能 INSERT/UPDATE/DELETE，也不能执行 DDL。
- 并发异步提交在真实 PG 上只会创建一个 Run；跨用户读取返回 403/404；区域受限角色无法通过省略、放宽或替换过滤器绕过区域限制。
- 服务重启后未完成 Run 被标记为 `interrupted`，`stop_reason=process_restart`；Checkpoint 在进程崩溃后可从真实 PG 恢复。
- 撤销 `CONNECT` 权限后，运行时存储立即抛出 `OperationalError`，没有静默回退到 SQLite。
- 完整离线回归：**746 passed, 30 skipped**（未设置真实 PG 测试 URL 时跳过 30 项 PostgreSQL 专项）。
- 未验证外部托管 PostgreSQL、多节点部署、TLS、备份恢复、长时间压测和真实模型下的 PG 全链路。

**阶段判定：核心目标完成。** 本地真实 PostgreSQL 的权限边界和关键恢复路径已通过测试；生产级托管数据库与多实例部署仍属于未验证范围。

---

## 2. 环境与方法

### 2.1 真实 PostgreSQL 环境

| 项目 | 值 |
| --- | --- |
| 版本 | PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) aarch64 |
| 容器 | `mark-postgres`（`pgvector/pgvector:pg17`，healthy） |
| 端口 | `localhost:5432` |
| 业务数据库 | `enterprise_business_agent` |
| 验收专用数据库 | `eba_phase9_2_test`（owner `eba_admin`，2026-11-04 创建） |
| 应用管理角色 | `eba_admin` |
| 业务只读角色 | `eba_readonly` |
| 运行时角色 | `eba_runtime` |
| 测试专用角色 | `eba_test_readonly`、`eba_test_runtime`（存在，未作为本轮主路径） |

验收使用独立数据库 `eba_phase9_2_test`，测试会创建、清空和删除其中的业务表与 `agent_*` 运行时表，不影响生产业务库。连接串从 `.env` 读取，报告和日志中没有输出密码。

### 2.2 真实/SQLite 边界

| 测试类别 | 数据库 | 说明 |
| --- | --- | --- |
| 30 项 PostgreSQL 专项 | **真实 PostgreSQL 17.11** | 本报告的核心结果 |
| 完整离线回归 746 项 | SQLite（除 30 项 PG 跳过外） | 只证明代码正确性，不替代 PG 结论 |
| LLM | 脚本化测试替身 | 验证控制流与持久化，不是真实模型质量 |

没有把 SQLite 测试计入 PostgreSQL 通过数，也没有把 Mock/脚本化 LLM 的结果描述为真实模型结果。

### 2.3 执行命令

```bash
# 真实 PostgreSQL 全量专项
EBA_TEST_POSTGRES_URL=… \
EBA_TEST_POSTGRES_RUNTIME_URL=… \
EBA_TEST_POSTGRES_READONLY_URL=… \
PYTHONPATH=src .venv/bin/python -m pytest -q -m postgres tests/

# 完整离线回归（未设置 PG URL，PG 专项跳过）
PYTHONPATH=src .venv/bin/python -m pytest -q
```

---

## 3. 权限边界验证

### 3.1 表级权限（查询 `has_table_privilege`）

| 角色 | 表 | SELECT | INSERT | UPDATE | DELETE |
| --- | --- | --- | --- | --- | --- |
| `eba_readonly` | `sales_fact`（业务表） | ✅ | ❌ | ❌ | ❌ |
| `eba_readonly` | `agent_runs`（运行时表） | ✅ | ❌ | ❌ | ❌ |
| `eba_runtime` | `sales_fact`（业务表） | ❌ | ❌ | ❌ | ❌ |
| `eba_runtime` | `agent_runs`（运行时表） | ✅ | ✅ | ✅ | ✅ |

### 3.2 直接行为探针

| 操作 | 结果 |
| --- | --- |
| `eba_readonly` 查询 `sales_fact` | 允许 |
| `eba_readonly` 向 `sales_fact` INSERT | 拒绝（`ProgrammingError`） |
| `eba_readonly` 执行 `CREATE TABLE` | 拒绝（`InternalError`） |
| `eba_runtime` 查询 `sales_fact` | 拒绝（`ProgrammingError`） |
| `eba_runtime` 执行 `CREATE TABLE` | 拒绝（`ProgrammingError`） |

对应自动化测试：

- `test_postgres_runtime_role_cannot_read_business_or_run_ddl`
- `test_runtime_role_can_dml_but_not_ddl_or_superuser`
- `test_independent_readonly_role_rejects_writes`
- `test_postgres_readonly_engine_rejects_writes`

---

## 4. 功能验收结果

### 4.1 数据持久化与重启恢复

| 场景 | 测试 | 结果 |
| --- | --- | --- |
| Runtime 表结构完整 | `test_postgres_runtime_schema_contains_all_batch2_tables` | PASS |
| 关闭并重开存储后 Run 仍存在 | `test_postgres_run_store_survives_reopen` | PASS |
| 进程崩溃后从真实 PG Checkpoint 恢复 | `test_postgres_checkpoint_resumes_after_a_crash` | PASS |
| 审批状态机持久化 | `test_postgres_approval_state_machine` | PASS |
| 服务重启把运行中 Run 标记为中断 | `test_postgres_restart_marks_unfinished_run_interrupted` | PASS |

重启测试使用真实 `PersistentRunStore`：先写入 `running` 状态，关闭存储，再用新实例启动应用，历史接口和 Run 详情都返回 `interrupted` / `process_restart`。

### 4.2 并发、Lease 与 Fencing

| 场景 | 测试 | 结果 |
| --- | --- | --- |
| 两个独立进程同时抢占，仅一个成功 | `test_two_independent_processes_only_one_claim_wins` | PASS |
| 有效 Lease 存在时恢复返回 409 | `test_live_lease_returns_409_on_the_resume_endpoint` | PASS |
| Lease 过期后可接管，旧 worker 不能写入 | `test_expired_lease_allows_takeover_and_stale_worker_cannot_write` | PASS |

这些测试使用真实 PostgreSQL 的条件 UPDATE、租约时间和 `claim_version` fencing，不是进程内锁或 SQLite 模拟。

### 4.3 幂等

| 场景 | 测试 | 结果 |
| --- | --- | --- |
| 4 个并发请求使用同一 Idempotency-Key | `test_postgres_concurrent_idempotency_creates_one_run` | PASS |

4 个并发异步提交全部返回 202，且 `run_id` 只有 1 个；结果由真实 PostgreSQL 唯一约束/事务保证。

### 4.4 用户与区域权限隔离

| 场景 | 测试 | 结果 |
| --- | --- | --- |
| 区域受限角色省略区域过滤 | `test_postgres_region_scope_and_department_isolation` | 403 |
| 指定其他区域 | 同上 | 403 |
| 用 `in` 放宽到 EC+NC | 同上 | 403 |
| 只查询授权区域 EC | 同上 | 200 |
| 其他用户读取 Run 详情 | `test_postgres_cross_user_run_isolation` | 403/404 |
| 管理员跨用户读取 | 同上 | 200 |

这些断言在真实 PostgreSQL 业务数据上执行，读路径使用 `eba_readonly` 的只读连接。

### 4.5 数据库异常

| 场景 | 测试 | 结果 |
| --- | --- | --- |
| Runtime 表缺失时拒绝启动，不回退 SQLite | `test_runtime_store_refuses_missing_schema_instead_of_using_sqlite` | PASS |
| PostgreSQL 不可达时快速失败 | `test_runtime_store_fails_fast_when_postgres_is_unreachable` | PASS |
| 撤销 `eba_runtime` 的 CONNECT 后连接失败 | `test_postgres_connection_privilege_failure_is_not_silently_degraded` | PASS |
| 查询超时映射为结构化错误 | `test_postgres_timeout_maps_to_structured_error` | PASS |
| 清理失败时 10 张表整体回滚 | `test_postgres_cleanup_rolls_back_every_table_on_failure` | PASS |

权限撤销测试在 `finally` 中恢复 `CONNECT`，随后重新建立 `PersistentRunStore` 验证恢复。

### 4.6 清理与事务

| 场景 | 测试 | 结果 |
| --- | --- | --- |
| 单事务删除 10 类对象 | `test_postgres_cleanup_deletes_all_ten_objects_in_one_transaction` | PASS |
| dry-run 与活动 Lease 保护 | `test_postgres_cleanup_dry_run_and_active_lease_protection` | PASS |

---

## 5. 测试数量与结果

### 5.1 真实 PostgreSQL 专项

| 文件 | 数量 | 结果 |
| --- | --- | --- |
| `tests/test_phase6_postgres_runtime.py` | 5 | 5 passed |
| `tests/test_phase6_postgres_batch3.py` | 7 | 7 passed |
| `tests/test_postgres_integration.py` | 7 | 7 passed |
| `tests/test_phase5_postgres.py` | 4 | 4 passed |
| `tests/test_phase6_cutover.py` | 2 | 2 passed |
| `tests/test_phase9_2_postgres_acceptance.py`（本轮新增） | 5 | 5 passed |
| **合计** | **30** | **30 passed, 0 failed** |

统一执行结果：`30 passed, 736 deselected in 4.12s`。

### 5.2 离线回归

`746 passed, 30 skipped in 114.12s`（2026-11-05 复跑，包含 Phase 9.1-Final 新增的 8 个负向回归测试与 3 个正向对照）。30 个 skip 全部是未设置 `EBA_TEST_POSTGRES_URL` 等环境变量时的 PostgreSQL 专项，不是失败，也不计入通过率。

---

## 6. 本轮新增测试

新增文件：`tests/test_phase9_2_postgres_acceptance.py`，5 项测试：

1. `test_postgres_region_scope_and_department_isolation`：真实 PG 上的区域过滤绕过测试。
2. `test_postgres_cross_user_run_isolation`：真实 PG 上的跨用户 Run 读取隔离。
3. `test_postgres_concurrent_idempotency_creates_one_run`：真实 PG 上的并发幂等。
4. `test_postgres_restart_marks_unfinished_run_interrupted`：真实 PG 上的服务重启恢复。
5. `test_postgres_connection_privilege_failure_is_not_silently_degraded`：撤销 CONNECT 后的失败与恢复。

这些测试在没有真实 PG URL 时自动 skip，不会用 SQLite 顶替。

---

## 7. 未完成与未验证

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| 外部托管 PostgreSQL（云 RDS 等） | 未验证 | 仅验证本地容器 PG 17.11 |
| 多实例/多节点部署 | 未验证 | 单机容器；未验证跨节点网络分区和时钟偏差 |
| TLS / 连接加密 | 未验证 | 本地连接未启用 TLS |
| 备份、恢复、PITR | 未验证 | 未执行备份恢复演练 |
| 长时间稳定性/容量压测 | 未验证 | 仅短时功能与并发测试 |
| 真实模型 + PostgreSQL 全链路 | 未执行 | 本轮 PG 测试使用脚本化 LLM；真实模型结果见 Phase 9.1-Final |
| Docker 镜像构建 | 未验证 | 与本阶段数据库验收独立 |
| 生产部署验收 | 未完成 | 不把本地 PG 结果等同于生产验收 |

---

## 8. 产物

| 产物 | 路径 |
| --- | --- |
| 本报告 | `docs/phase9/PHASE9_2_REPORT.md` |
| 新增 PG 验收测试 | `tests/test_phase9_2_postgres_acceptance.py` |
| 完整离线回归日志 | `/tmp/phase9_final_pytest.log`（本地临时文件） |

---

## 9. 结论

Phase 9.2 在真实 PostgreSQL 17.11 上验证了最小权限角色、持久化、Checkpoint 恢复、并发抢占、Lease/Fencing、幂等、重启恢复、用户/区域隔离和数据库异常处理。30 项 PG 专项全部通过，且没有 SQLite 代替或 Mock 数据库。

该结果证明当前架构在单实例本地 PostgreSQL 上具备可用性和基本安全边界；它不证明外部托管数据库、多节点部署、TLS、备份恢复或生产容量已经达标。
