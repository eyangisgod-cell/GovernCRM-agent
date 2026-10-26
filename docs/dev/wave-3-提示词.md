# Wave 3 会话提示词（执行链与治理）

> 复制以下正文到新会话执行。Wave 2 已完成并归档。

---

【任务】执行 GovernCRM M1 MVP 的 Wave 3（执行链与治理），在 F:\my-project\market-auto-agent，
严格按 Trellis 流程，主会话派子代理实施。

当前状态：Wave 2（T-D1/T-D2/T-D3 决策层）已完成并归档，branch=feature/wave-2-decision-layer，make check 全绿。
父任务 .trellis/tasks/10-25-m1-mvp 状态 in_progress。

## 执行方式
1. git checkout -b feature/wave-3-execution-chain
2. python ./.trellis/scripts/task.py current
3. 按 docs/specs/MVP_任务拆解.md:225 起「Wave 3：W5 + W6 执行链与治理」建子任务，**按依赖序**：

   **Phase A（可并行，仅依赖 T0.2）**：
   - T-E3 审计 append-only + 哈希链 → `src/audit/hash_chain.py`
   - T-E5 RBAC 4 角色 + 权限矩阵 → `src/auth/rbac.py`, `src/auth/roles.py`

   **Phase B（串行，核心）**：
   - T-E1 LangGraph 执行图（4 步 + confirm_plan）→ `src/graph/workflow.py`

   **Phase C（T-E1 完成后可并行）**：
   - T-E2 治理闸门 + 审批 interrupt → `src/graph/gate.py`, `src/approval/router.py`
   - T-E4 路径完整性断言 → `src/graph/path_assert.py`
   - T-E7 位点表（G-04）→ `migrations/003_task_checkpoint.sql`

   **Phase D（T-E2 完成后）**：
   - T-E6 熔断（一键停）→ `src/graph/circuit_breaker.py`

4. 每个子任务：task.py start <slug> → 派 trellis-implement → 回来后派 trellis-check
   → 通过则 task.py finish + archive <slug> --no-commit
5. 子代理 prompt 第一行必须是：Active task: .trellis/tasks/<task-path>
   （取自 task.py current）
   正文只给三件事：① 目标 = 子任务 prd.md 的产出文件与验收命令；
   ② 读法 = 子任务 implement.md 的「读法」表（精确 file:line）；
   ③ 红线 = .trellis/spec/backend/ 下的 5 条红线。
6. 若子代理通道报 429 / Unsupported model：回退「主会话直接实施 + 自检」，
   并在报告里注明「缺第三方复核」。

## 【TDD 纪律】—— 强制
每个行为走红→绿→重构循环，**先看到测试失败，才允许写实现代码**：
  1. 从子任务 prd.md 的「验收标准」直接翻译成 assert 写测试
  2. 跑 pytest 确认**红**，且红的理由必须对（断言不成立，不是 ImportError / 收集错误）
  3. 写让测试通过的**最少**实现
  4. 跑 pytest 确认**绿**
  5. 重构，保持绿
  6. 下一个行为
配套硬纪律：
  - 测试名描述**行为**不描述实现
  - 断言**具体**：断言报错消息里出现了 rule_id，好过断言 raises(Exception)
  - **测试即规格**：每个测试的 docstring 写明依据的权威条款
    （如「附录D 规则 2」「ADR-035」「ADR-030」），让人能反查
  - **负向用例不许 skip**：环境缺失用 pytest.fail（沿用「可跳过的门等于没有门」）
  - 子代理须把「红→绿」的 pytest 输出贴进报告
TDD 适用范围：
  - **纯函数类**（T-E3 哈希链 / T-E4 路径断言 / T-E5 RBAC）：标准红绿重构，每条规则一条测试
  - **生命周期类**（T-E1 LangGraph 图 / T-E2 interrupt / T-E6 熔断）：允许先搭骨架，
    但**负向路径（DENY / 跳节点 / 熔断拒绝）仍须先有测试**
  - **PG 特性**（T-E3 触发器 / T-E7 迁移）：先写集成测试（`pytestmark = pytest.mark.pg`），
    **不得用 SQLite 代替**

## 硬约束
- 规则语义只认 docs/specs/GovernCRM_附录D_冻结版.md；
  PRD v5.2 / 架构 v5.2 的附录 D 草案已作废，禁止按草案实现。
- 5 条红线不可破：policy_kernel 纯函数零 LLM / DENY 不可绕过（含管理员）/
  审计 append-only + 哈希链且写失败即阻断业务 / 路径断言节点不可 opt-in /
  业务层零供应商依赖（import litellm 仅允许 src/providers/）。
- **confirm_plan 节点必须保留**（ADR-035）：置于 entry 后、validate 前，
  纳入 `REQUIRED_NODES`，所有自主度等级必需。没有它，路径断言演示不成立。
- **审批恢复后以当前 rule_version 重新判定**（ADR-030），不得用挂起时的旧版本。
- **抑制/频控判定唯一实现点 = src/policy_kernel/rules.py**，
  src/decision/ 与 src/graph/ 不得出现第二份；只做编排调用。
- **三档路由判定唯一实现点 = src/decision/impact.py**，
  T-E2 的审批模块只**调用**它，不另写一份。
- **写 audit_log 必须走 src/audit/hash_chain.insert_statement_sql()**，
  record_hash 由数据库算；不要自己拼 INSERT，否则哈希链断裂，
  make verify-chain 会红。给 audit_log 加列必须同步改 HASHED_FIELDS。
- 依赖白名单制：pyproject.toml 清单外的第三方库禁止 import。
  LangGraph 是新增依赖——如不在白名单，**停下来报告**。
- 真 PG 验证：触发器/哈希链/CHECK 约束不得用 SQLite 或内存库。
- **迁移单一写者**：本波仅 T-E7 写 `migrations/003_task_checkpoint.sql`；
  其余子任务不写迁移（T-E3 的触发器迁移已在 T0.2 完成）。
  如需其他迁移先停下来报告。
- 目录命名统一用 src/policy_kernel（已按权威文档对齐，勿改回 src/policy）。
- 上下文纪律：文档超 150 行禁整读，Grep -n 拿行号 → Read(offset, limit)；
  大文件用 mcp__context-compress__execute_file 摘要。
- 不改 docs/specs/ 下任何文档；不提交 .trellis/（已 gitignore）；不 push。
- 遇与权威文档冲突时**停下来报告，不要自行拍板**。

## 各子任务验收命令（写进各子任务 prd.md）

### T-E1 LangGraph 执行图
- 产出：`src/graph/workflow.py`
- 节点：`segment → decision → confirm_plan → gate(interrupt) → send(mock)`
- 验收：`python -m src.graph.workflow --dry-run` 走通全图

### T-E2 治理闸门 + 审批 interrupt
- 产出：`src/graph/gate.py`, `src/approval/router.py`
- 验收：① DENY 客户进 execute → 闸门拒绝 ② REQUIRE_APPROVAL → 挂起，审批后恢复 ③ 审批三档路由正确

### T-E3 审计 append-only + 哈希链
- 产出：`src/audit/hash_chain.py`
- 验收：① 先插入至少 2 条真实记录 ② `UPDATE audit_log ...` → 触发器拒绝 ③ 手动改一条 `prev_hash` → `python -m src.audit.hash_chain --verify` 报错并定位到第 N 条

### T-E4 路径完整性断言
- 产出：`src/graph/path_assert.py`
- 规格：`visited_nodes ⊇ REQUIRED_NODES`，缺失即 DENY + 告警（E14）
- 验收：端到端：构造"跳过 confirm_plan"的图 → 断言 DENY

### T-E5 RBAC 4 角色 + 权限矩阵
- 产出：`src/auth/rbac.py`, `src/auth/roles.py`
- 规格：4 角色（管理员/运营负责人/运营专员/合规，裁决 #23）；管理员不自动蕴含下级权限（ADR-044）
- 验收：① 管理员对 SUP_HIGH_VALUE 客户强制放行 → **被拒** ② 越权调用被拒并审计

### T-E6 熔断（一键停）
- 产出：`src/graph/circuit_breaker.py`
- 规格：全局开关；停止派发 + 挂起排队；恢复 = 挂起动作以当前 rule_version 重新判定（ADR-030 / FR-28）
- 验收：熔断后新动作不派发；恢复后重判且写审计

### T-E7 位点表（G-04）
- 产出：`migrations/003_task_checkpoint.sql`
- 规格：`task_checkpoint` 存审计级位点索引；不存 graph_state_snapshot——恢复用 LangGraph checkpointer
- 验收：查询接口 `GET /api/v1/tasks/{task_id}/checkpoints` 返回位点列表（FR-14）

## 完成标准
- 本波全部子任务验收过 + make check 全绿 + 各 checklist 全勾
- 输出《Wave 3 完成报告》：产出文件清单、验收命令实际输出、遗留问题
- **同时给出一份「待人工逐行 review 清单」**：
  ① LangGraph 图节点编排与 confirm_plan 位置（红线④路径断言）
  ② 闸门 DENY 不可绕过逻辑（红线②）
  ③ 审批恢复后 rule_version 重判（ADR-030）
  ④ 审计哈希链写入路径（红线③）
  ⑤ RBAC 权限矩阵与管理员不蕴含下级（ADR-044）
  ⑥ 熔断恢复后的重判逻辑
- 收尾时给出下一波（Wave 4）的会话提示词
- **本波做完即停**，不要顺手做下一波
