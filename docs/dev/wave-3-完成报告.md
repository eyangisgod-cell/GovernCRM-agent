# Wave 3 完成报告

| 项 | 内容 |
| --- | --- |
| 日期 | 2026-10-26 |
| 分支 | `feature/wave-3-execution-chain`（基于 `main` = `4af06b9`） |
| 父任务 | `.trellis/tasks/10-25-m1-mvp` |
| 子任务 | 7 个（T-E3 / T-E5 / T-E1 / T-E2 / T-E7 / T-E4 / T-E6），全部 `archive --no-commit` 至 `.trellis/tasks/archive/2026-10/` |
| 门禁 | `make check` **六项全绿**（488 单测 + 47 pg + ruff check/format + mypy 107 文件 + 依赖白名单 + 哈希链校验 737 条完整） |
| 代码状态 | **未提交、未推送**（见 §6 待办） |

> **⚠ 缺第三方复核（部分）。** T-E6 第一轮 check 由 `trellis-check` 完成并给出两项 REWORK；
> 第二轮修复中段遭遇 `429 Token Plan 用量上限`（54 tool uses 后中断），
> 按 fallback 协议**由主会话接手完成剩余修复**（`_assemble_rows` 上下文级去重、
> pending 清理键改三元组、`mock_adapter` 链尾委托 `chain_tip`）。
> **这部分修复缺 `trellis-check` 第三方复核**，已纳入 §5 人工 review 清单第⑥项。
> 其余 6 个子任务均走完 implement + check 双通道。

---

## 1. 交付文件清单

### 1.1 源码（新增 16 个文件，修改 4 个）

| 文件 | 归属 | 说明 |
| --- | --- | --- |
| `src/audit/chain.py` | T-E6 | **链尾唯一口径**：`CHAIN_TIP_SQL` + `chain_tip(conn, tenant_id)`；5 个审计写入方共用（events / approval-audit / rbac / mock_adapter / 通用事件） |
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

1. **LangGraph 图编排与 confirm_plan 位置** — `src/graph/workflow.py`：5 节点顺序、条件边、ADR-035 confirm_plan 图内必经（所有自主度等级）。
2. **闸门 DENY 不可绕过** — `src/graph/gate.py`：DENY 先于 interrupt；approve 恢复不能救已 DENY 候选；E9 48h 超时不默认通过。
3. **审批恢复后 rule_version 重判** — `src/graph/gate.py::rejudge_with_current_config` + `src/approval/router.py`：resume 时现载 config（ADR-030）；`required_signers` 无自有阈值（三档唯一实现点在 `decision/impact.py`）。
4. **审计哈希链写入路径** — `src/audit/chain.py`（链尾 SQL 唯一）+ 5 个写入方的 `insert_statement_sql` 接线；熔断 trip 审计失败的状态回滚补偿。
5. **RBAC 矩阵与管理员不蕴含** — `src/auth/roles.py` PERMISSION_MATRIX 52 格 vs PRD 十一章；管理员不含下级权限（ADR-044 / K-M3）。
6. **熔断恢复重判（含主会话修复段）** — `src/graph/circuit_breaker.py::_assemble_rows`（上下文级去重、行不合并的理由写在 docstring）、pending 清理三元组键（:672-676）、`ResumeOutcome.undecided_candidate_ids` 结构性防静默丢弃。**此段缺第三方复核，请重点看。**

## 6. 已知遗留

| # | 事项 | 归属 |
| --- | --- | --- |
| 1 | T-E6 第二轮修复缺第三方复核（429 回退主会话） | §5 第⑥项人工 review |
| 2 | `escalation_required` 字段暂无下游消费者 | Wave 4 或按需清理 |
| 3 | `tests/evaluation/path_assertions.yaml` 与 `src/graph/path_assert.py` REQUIRED_NODES 两套词表未统一 | T-G1（Wave 4） |
| 4 | API 端点无租户鉴权中间件 | Wave 4 接 T-E5 |
| 5 | 熔断开关/队列为进程内实现（代码已标注），持久化未做 | Wave 4+（本波唯一迁移写者是 T-E7） |
| 6 | `customer_rows` 多行口径已统一为「末条」（gate.py:178 与熔断 docstring 一致） | 已收口，review 时知悉即可 |

## 7. 提交说明

仅提交 §1 文件 + 本报告 + Wave 4 提示词；**不提交 `.trellis/`**；**不 push**。
