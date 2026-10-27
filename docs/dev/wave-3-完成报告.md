# Wave 3 完成报告

| 项 | 内容 |
| --- | --- |
| 日期 | 2026-10-26（**2026-10-27 追加 QA 复核与修复**，见 §8） |
| 分支 | `feature/wave-3-execution-chain`（基于 `main` = `4af06b9`） |
| 父任务 | `.trellis/tasks/10-25-m1-mvp` |
| 子任务 | 7 个（T-E3 / T-E5 / T-E1 / T-E2 / T-E7 / T-E4 / T-E6），全部 `archive --no-commit` 至 `.trellis/tasks/archive/2026-10/` |
| 门禁 | `make check` **六项全绿**（506 单测 + 49 pg + ruff check/format + mypy 108 源文件 + 依赖白名单 108 文件 + 哈希链校验 1100 条完整） |
| QA 复核 | 2026-10-27 人工审核发现 **4 类红线级缺陷**（熔断跨组误清理 / 恢复跨组错发 / E9 超时失效 / 审计链并发断链），已修复并复验，见 §8 |
| 代码状态 | **未提交、未推送**（见 §6 待办） |

> **⚠ 缺第三方复核（部分，已闭环）。** T-E6 第一轮 check 由 `trellis-check` 完成并给出两项 REWORK；
> 第二轮修复中段遭遇 `429 Token Plan 用量上限`（54 tool uses 后中断），
> 按 fallback 协议**由主会话接手完成剩余修复**（`_assemble_rows` 上下文级去重、
> pending 清理键改三元组、`mock_adapter` 链尾委托 `chain_tip`）。
> 该部分修复当时缺第三方复核，已纳入 §5 人工 review 清单第⑥项；
> **2026-10-27 修复全部缺陷后，另派独立 check 代理复核整波修复**（结论与遗留项见 §8.7）。
> 其余 6 个子任务均走完 implement + check 双通道。

---

## 1. 交付文件清单

### 1.1 源码（新增 16 个文件，修改 4 个）

| 文件 | 归属 | 说明 |
| --- | --- | --- |
| `src/audit/chain.py` | T-E6 | **链尾唯一口径**：`CHAIN_TIP_SQL` + `chain_tip(conn, tenant_id)`；**4 个**审计写入方共用（events / approval-audit / rbac / mock_adapter——"通用事件"即 events，原清单重复计数，已于 §8 更正）；§8 追加同租户写锁 + 写事务 |
| `src/audit/events.py` | T-E6 | 熔断审计 DB 写入器（lazy psycopg，走 `insert_statement_sql`） |
| `src/audit/hash_chain.py` | T-E3 | 增 `main(argv)` CLI（`--verify` / `--tenant-id`，exit 0/1/2），仅接线既有 verify 函数 |
| `src/auth/roles.py` | T-E5 | Role / Permission 枚举 + PERMISSION_MATRIX（PRD 十一章 52 格） |
| `src/auth/rbac.py` | T-E5 | `check_permission()` 纯函数 + `record_denial()` 审计（链尾走 `chain_tip`） |
| `src/graph/nodes.py` | T-E1 | 节点常量独立模块（破循环 import）；workflow 再导出 |
| `src/graph/workflow.py` | T-E1 | 5 节点 StateGraph（segment→decision→confirm_plan→gate→send）；`_begin_trace` 防轨迹伪造；send 前断言 |
| `src/graph/gate.py` | T-E2 | `evaluate_gate`（DENY 先于 interrupt）/ `run_gate` / `interpret_resume`（E9 超时）/ `rejudge_with_current_config`（ADR-030 重判入口） |
| `src/graph/path_assert.py` | T-E4 | `assert_path()`（E14 / PATH_ASSERT）；`_REQUIRED_NODES` 模块私有 + 闭包捕获只读 |
| `src/graph/circuit_breaker.py` | T-E6 | `CircuitBreaker`（trip/reset/is_open + 挂起队列 + 实测耗时）+ `ResumeOutcome`（sendable/denied/undecided）；恢复重判只调 gate |
| `src/approval/router.py` | T-E2 | `required_signers()` 只调 `route_by_impact`（裁决#25 三档唯一实现点）+ 48h 超时纯函数 |
| `src/approval/audit.py` | T-E2 | `write_rejudge_audit`（`insert_statement_sql`） |
| `src/db/checkpoints.py` | T-E7 | `record_checkpoint` / `list_checkpoints` |
| `src/api/routes/tasks.py` | T-E7 | `GET /api/v1/tasks/{task_id}/checkpoints` |
| `src/api/schemas/task_checkpoint.py` | T-E7 | 响应 schema |
| `migrations/003_task_checkpoint.sql` | T-E7 | 7 列位点表（G-04 / FR-14），幂等，无 snapshot |

**修改 4 个：** `src/app.py`（挂载 tasks 路由，+2 行）、`src/auth/__init__.py`（导出，+1 行）、`src/adapters/mock_adapter.py`（`_last_record_hash` 委托 `chain_tip`，删第二份 SQL，-11/+3）、`ruff.toml`（`line-ending "lf"→"auto"`，见 §4）。

### 1.2 测试（新增 4 个目录 + 2 个文件，约 130 条新用例）

| 文件/目录 | 用例数 | 覆盖 |
| --- | ---: | --- |
| `tests/graph/test_workflow_structure.py` | ~10 | BFS 全 START→send 路径必含 confirm_plan+gate；节点接线 |
| `tests/graph/test_workflow_dry_run.py` | ~5 | dry-run 五节点全序 |
| `tests/graph/test_path_assert.py` + `test_path_assert_e2e.py` | ~15 | E14 拦截；伪造轨迹回归；send 派发前断言 |
| `tests/graph/test_gate.py` | ~25 | DENY 先于 interrupt；approve 不能救 DENY 候选（:316）；E9 超时 |
| `tests/graph/test_circuit_breaker.py` | 27 | send 零发生+挂起；恢复中途改 config 重判 DENY；行不合并回归（:721）；链尾 SQL 单一实现守卫 |
| `tests/auth/test_rbac_matrix.py` | 68 | 52 格矩阵 + 管理员不蕴含 + DENY 不可绕过 |
| `tests/auth/test_rbac_audit_pg.py` | 2 pg | 拒绝审计入链 |
| `tests/integration/test_hash_chain_cli.py` | 2 pg | CLI exit 0；篡改定位第 N 条 |
| `tests/integration/test_task_checkpoint_pg.py` | pg | 位点表读写 |

## 2. 验收命令实测输出

```
uv run pytest -q            → 488 passed, 47 deselected
make pg-test                 → 47 passed（真 PG，未用 SQLite）
python -m src.graph.workflow --dry-run
  → visited = [segment, decision, confirm_plan, gate, send]；放行 1 / DENY 0；dry-run 完成
uv run ruff check .          → All checks passed
uv run ruff format --check . → 110 files already formatted
uv run mypy                  → Success: no issues in 107 source files
scripts/check_dependencies   → 107 文件，白名单外 import 零
make verify-chain            → 737 条记录，链完整
make check                   → all gates green
```

## 3. 红线落地核对

| 红线 | 落地方式 |
| --- | --- |
| ① policy_kernel 零改动 | 本波未 import 任何 LLM；`src/policy_kernel/` 无 diff |
| ② DENY 不可绕过 | gate：DENY 在 interrupt **之前**抛出；测试 `test_approve_resume_cannot_rescue_denied_candidate`；熔断：行不合并（上下文级去重），避免恢复比常规路径更松 |
| ③ 审计哈希链 | 全部写入走 `insert_statement_sql()`；链尾唯一口径 `src/audit/chain.py`（守卫测试 `test_chain_tip_sql_has_a_single_implementation`）；写失败阻断业务（熔断 trip 审计失败回滚状态） |
| ④ 路径断言 | send 派发前断言（任何到 send 的路径都被拦）；`_REQUIRED_NODES` 私有 + 闭包只读；伪造轨迹回归测试 |
| ⑤ 零供应商依赖 | `import litellm` 仍仅 `src/providers/`；依赖白名单扫描 107 文件通过 |

## 4. 环境问题处置（Windows / autocrlf）

`core.autocrlf=true` 与 `ruff.toml line-ending="lf"` 冲突：checkout 出 CRLF，ruff format --check 全仓报红（86 文件），并衍生 92 个「内容一致、仅 stat 缓存失效」的幻影 M 条目。

处置：① `ruff.toml` 改 `line-ending = "auto"`（保留各文件既有行尾，Windows 与 CI/Linux 均稳定）；② 对 92 个幻影文件 `git add` 刷新 stat 缓存（内容零变化，`git diff --cached` 为空）；③ 真实改动仅剩 5 个 tracked 文件（§1.1 修改 4 个 + ruff.toml）。

## 5. 待人工逐行 review 清单（6 项）

> **2026-10-27 已复核完毕**：6 项中 4 项发现缺陷并已修复（含 2 个 P0），2 项通过；
> 复核过程、修复证据与挂账项见 **§8**。下列清单保留原始 review 视角。

1. **LangGraph 图编排与 confirm_plan 位置** — `src/graph/workflow.py`：5 节点顺序、条件边、ADR-035 confirm_plan 图内必经（所有自主度等级）。
2. **闸门 DENY 不可绕过** — `src/graph/gate.py`：DENY 先于 interrupt；approve 恢复不能救已 DENY 候选；E9 48h 超时不默认通过。
3. **审批恢复后 rule_version 重判** — `src/graph/gate.py::rejudge_with_current_config` + `src/approval/router.py`：resume 时现载 config（ADR-030）；`required_signers` 无自有阈值（三档唯一实现点在 `decision/impact.py`）。
4. **审计哈希链写入路径** — `src/audit/chain.py`（链尾 SQL 唯一）+ 5 个写入方的 `insert_statement_sql` 接线；熔断 trip 审计失败的状态回滚补偿。
5. **RBAC 矩阵与管理员不蕴含** — `src/auth/roles.py` PERMISSION_MATRIX 52 格 vs PRD 十一章；管理员不含下级权限（ADR-044 / K-M3）。
6. **熔断恢复重判（含主会话修复段）** — `src/graph/circuit_breaker.py::_assemble_rows`（上下文级去重、行不合并的理由写在 docstring）、pending 清理三元组键（:672-676）、`ResumeOutcome.undecided_candidate_ids` 结构性防静默丢弃。**此段缺第三方复核，请重点看。**

## 6. 已知遗留

| # | 事项 | 归属 |
| --- | --- | --- |
| 1 | T-E6 第二轮修复曾缺第三方复核（429 回退主会话） | **已闭环**：修复后由独立 check 代理复核本波全部修复（结论见 §8.7），遗留项已登记 |
| 2 | `escalation_required` 字段暂无下游消费者 | Wave 4 或按需清理 |
| 3 | `tests/evaluation/path_assertions.yaml` 与 `src/graph/path_assert.py` REQUIRED_NODES 两套词表未统一 | T-G1（Wave 4） |
| 4 | API 端点无租户鉴权中间件 | Wave 4 接 T-E5 |
| 5 | 熔断开关/队列为进程内实现（代码已标注），持久化未做 | Wave 4+（本波唯一迁移写者是 T-E7） |
| 6 | `customer_rows` 多行口径已统一为「末条」（gate.py:178 与熔断 docstring 一致） | 已收口，review 时知悉即可 |

## 7. 提交说明

仅提交 §1 文件 + 本报告 + Wave 4 提示词；**不提交 `.trellis/`**；**不 push**。

---

## 8. QA 复核（2026-10-27）

### 8.1 复核方式

对 §5 的 6 项人工 review 清单逐项派独立子代理复核（代码 + 实测复现），主会话对每条结论**二次核验到 file:line**；随后由 3 个 implement 子代理分模块修复，再由独立 check 代理复验。**PG 为真实 PostgreSQL**（`governcrm-db` 容器），非 SQLite 替身。

### 8.2 结论：6 项中 4 项发现缺陷，2 项通过

| # | 审核项 | 结论 |
| --- | --- | --- |
| ① | 熔断恢复重判 | ❌ **发现 2 个 P0**（跨组误清理 / 跨组错发+审计串味），已修 |
| ② | 闸门 DENY 不可绕过 | ⚠️ DENY 相关**全部通过**；但 E9 超时**实际失效**（独立于 DENY 的第 3 个缺陷），已修 |
| ③ | 审批恢复后重判 ADR-030 | ✅ 通过（附僵尸配置挂账，见 §8.4） |
| ④ | 审计哈希链 | ❌ **发现并发断链**（第 4 个缺陷），已修 |
| ⑤ | LangGraph 图编排 | ✅ 通过（2 项建议已挂账） |
| ⑥ | RBAC 矩阵 | ✅ 矩阵本体通过（52 格逐格核对）；"越权 403" 未接线，已挂账 |

### 8.3 已修的 4 个缺陷（全部有失败用例先证伪）

**① 熔断跨组误清理（P0，候选静默消失）**
`_settle_dispatched` 原按 `(trace_id, candidate_id)` 匹配且无 `tenant_id`，另有 candidate_id-only 全表删兜底 → 跨租户/跨轨迹的同名候选被连带清出队列，**既不放行也无留痕**（该模块反复声明要防的失败模式）。修复：三元组键 + `_ready` 一并三元组化。
证据：`test_settle_dispatched_keeps_same_candidate_in_other_tenant` 等 6 例修复前 **6 failed** → 修复后全绿。

**② 恢复跨组拍平 → 跨任务/跨租户错发（P0，审计串味）**
`reset()` 把各 (tenant, trace) 组的 sendable 拍平成并集，`_dispatch_rejudged` 在**当前 state 的租户/任务**下派发该并集 → 任务 B 恢复时派发了任务 A 的候选；恢复事件只按首组写（tenant/trace 取自首组、task_id 取自全部 action）→ 审计行串味、其余组无恢复事件。熔断器是全局单例，**单租户 MVP 下两个并发任务即可触发**。
修复：`ResumeOutcome.groups` + `group_for()` 按组取；恢复事件按组各写一条；派发只消费本组，其余进 `deferred_candidate_ids`（可见、不丢）。

**③ E9 审批超时失效（红线③依赖项）**
超时判定原用客户端可自带的 `resumed_at`，或 `state["now"]`（任务起始的陈旧值）→ **真实墙钟从未参与**，48h 超时既检测不出来、客户端回填时间戳即可绕过。
修复：过期**只**由服务端墙钟决定（窗口起点 = 服务端记录并随 interrupt 载荷持久化的挂起时刻，终点 = 恢复时墙钟）；客户端 `resumed_at` 降级为仅记录；取不到服务端挂起记录 → **fail-closed** 判过期。`state["now"]` 仅保留用于业务规则重判（规则语义）。

**④ 审计链并发断链（红线③）**
取链尾（SELECT）与插入（INSERT）分属两个事务（写入方 `autocommit=True`），`audit_log` 无任何唯一约束 → 同租户并发写读到同一链尾 → 两条记录同 `prev_hash` → 链断裂、`verify_chain` 失败。
修复：应用层（同事务 + `pg_advisory_xact_lock` 同租户串行 + 唯一约束冲突重试一次且不吞异常，四个写入方全部改造）+ DB 层（`migrations/004_audit_log_chain_unique.sql`：`UNIQUE INDEX (tenant_id, prev_hash)`，静默断链 → 显式阻断）。
实测：并发写同租户，修复前 `total=10 ok=False breaks=7`，修复后链完整。

### 8.4 纪律偏离登记

- **新增迁移 004**：本波原声明「唯一迁移 = 003」，现为 003 + 004（链结构不变量必须落 DB 层，001 已应用不可改）。已在 `migrations/003_task_checkpoint.sql` 头注释登记。
- **上报口径更正**：§1.1 原写「5 个审计写入方」，实为 **4 个** SQL 写入点（events / approval-audit / rbac / mock_adapter）——原清单把 events 与"通用事件"重复计数。

### 8.5 修复后门禁复验（真实 PostgreSQL）

```
pytest -q                      → 506 passed, 49 deselected
pytest -m pg -q                → 49 passed（真 PG）
python scripts/verify_hash_chain.py → 1100 条记录，链完整（条数随 pg 用例增长）
ruff check .                   → All checks passed
ruff format --check .         → 111 files already formatted
mypy                           → Success: no issues found in 108 source files（src+tests+scripts）
scripts/check_dependencies.py  → 108 文件，白名单外 import 零
make check                     → all gates green
```

另修一处**环境脆弱性用例**：`tests/graph/test_workflow_dry_run.py` 的 CLI 子进程用例原依赖执行环境本地代码页（Windows = GBK → 中文输出被 UTF-8 解码成替换符 → **在非 UTF-8 平台必失败**，尽管原验收环境为绿）。已显式注入 `PYTHONIOENCODING=utf-8`，用例不再依赖环境。

另修一处**门禁盲区**：`make check` 的 typecheck 跑的是**全项目** mypy（`files = src, tests, scripts`，108 文件），而修复过程中的自检只跑了 `mypy src`（70 文件）——本波新增用例里有一处 `assert len(tuple) == 1` 的类型收窄与随后的下标访问冲突，全项目 mypy 报 `unreachable`。已改为「一次性断言租户列表 == [tenant_b]」（断言更强且不触发收窄），全项目 mypy 现为零错误。**教训：自检必须与 `make check` 跑同一条命令。**

### 8.6 挂账项（Wave 4 必做，非本波范围）

| # | 事项 | 说明 |
| --- | --- | --- |
| 1 | **RBAC 未接线** | `check_permission` / `record_denial` 在 `src/` 内**零调用方**，`src/` 下无任何 403 —— RBAC 目前是没人用的库，不是生效的访问控制。§5 第 6 项清单所写"越权 403 + record_denial"仅成立于单测层 |
| 2 | **DENY 分支无审计** | `gate.py` 的规则 DENY 与路径断言 DENY 直接 return，不写审计 → 被拒决策不可追溯（红线③可追溯性缺口） |
| 3 | **阈值僵尸配置** | `governance.yaml` 的 `single_sign_max/dual_sign_max` 被 config 解析进 dataclass 但**全仓无消费者** → 改 yaml 无效，实为第二处 10/100 口径副本 |
| 4 | **confirm_plan 缺人工挂点与审计** | 满足"必经"（跳过即 DENY），但无 `interrupt()`、无确认审计（ADR-035 的"人工节点"语义与 v5.3 的 confirm-plan API 未落地） |
| 5 | **API 无租户鉴权** | `src/app.py` 已挂载 tasks 路由，无鉴权中间件、`tenant_id` 为可选查询参数 → 知道 task_id 即可读他人租户位点 |
| 6 | 恢复输入为**入队快照**且只判挂起子集 | 挂起期间新增的抑制命中/频控触达不参与重判；`src/graph` 零 DB 依赖，无法刷新当前数据。已在代码 docstring 声明 |
| 7 | ADR-035 与 MVP 节点顺序口径 | 架构 v5.2 说 confirm_plan 在 "entry 后、validate 前"，MVP 实为 decision→confirm_plan，建议补 MVP 映射说明 |

### 8.7 第三方复核（`trellis-check`）遗留项

修复后另派独立 check 代理复核本波 4 个缺陷的修复与门禁，结论为 **a–g 全部通过、可提交、无需自修**。同时留下 7 项不阻塞提交的遗留项（按优先级）：

> **口径提醒**：该复核的 mypy 门禁跑的是 `mypy src`（70 文件），**不是** `make check` 的全项目 mypy（108 文件）——本波新增用例里的一处类型收窄问题因此漏过，主会话在最终 `make check` 时才发现并修复（详见 §8.5「门禁盲区」）。**第三方复核结论按其实际覆盖范围采信，不外推。**

| 优先级 | 事项 | 说明 |
| --- | --- | --- |
| P1 | 重复 resume 重复派发 | 同一审批被重复 resume 时，熔断队列已出队的候选不会被再次派发（幂等性缺口）。**HEAD 基线既有，非本次修复引入** |
| P2 | `reset()` 是全局的 | 熔断器为全局单例，`reset()` 关闭的是**全局**开关并重判**所有**组 → 租户 A 的操作员恢复会关闭租户 B 的熔断、并为 B 的候选写审计行、并可能提前把 B 的候选判为 DENY。MVP 单租户下不暴露，多租户须拆分 |
| P2 | 异步 checkpointer 不兼容 | 过期判定的挂起载荷来自 `saver.list()`；异步 checkpointer 返回协程会被吞掉 → 取不到挂起记录 → **fail-closed 判过期**（审批被误判超时）。MVP 用同步 psycopg checkpointer，不受影响；换异步须同步改造 |
| P3 | `client_resumed_at` 未落审计 | 客户端自报时刻已提取并随 `ResumeDecision` 返回，但**未写入审计记录**；`gate.py` 的 docstring 已按实情更正为「仅提取，不落审计」 |
| P3 | `rejudged_candidate_count` 是全局计数 | 熔断恢复事件按组各写一条，但该计数取的是全局值 → 单组事件里的候选数偏大 |
| P3 | 状态级 denied 列表被本组覆盖 | `src/graph/workflow.py:437-442` 用当前组的 denied 覆盖 state 级列表（派发路径），跨组/前序 DENY 记录可能从 state 上被抹掉 |
| P3 | `deferred_candidate_ids` 未出 API | 别的组被顺延的候选已在 state 与结论里可见，但未通过 API 暴露 → 运维看不到「谁还在排队」 |


