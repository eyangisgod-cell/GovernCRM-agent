# GovernCRM MVP 任务拆解（v2.0）

| 项 | 内容 |
| --- | --- |
| 文档性质 | **MVP 开发任务拆解**：子任务级，含文件路径与验收命令。受 git 跟踪，多 worktree 共享 |
| 版本 | v2.1（2026-10-24 评审后重建 + 前端 5 屏落地） |
| 依赖 | 《GovernCRM_附录D_冻结版.md》《GovernCRM_开发实施计划.md》 |
| 并行策略 | Wave 0 串行 → Wave 1 三路并行（A/B/C）→ Wave 2 串行 → Wave 3 串行+评测并行 → Wave 4 API/前端/文档并行 |
| 前端 | **5 屏**（任务下达+计划确认 / 批量审批 / 决策回放 / 治理看板 / 系统配置），**MOCK 数据 + 真实交互**，详见《开发实施计划》§五之二 |

> **本版不设人日估算**（评审决定：AI 生成代码，人日不构成约束）。任务按**依赖关系**与**可独立验收性**切分。

---

## 一、MVP 范围界定

### 1.1 MVP 必须完成（缺一则演示闭环断裂）

| # | 能力 | 对应 FR/裁决 | 验收硬数字 |
|---|------|-------------|-----------|
| M1 | **纯代码策略内核**（零 LLM、零框架、可单测） | FR-20 / 裁决 #14 | 6 条规则全实现，DENY 分支全覆盖 |
| M2 | **冲突消解 + 抑制红线 + 全局频控** | FR-18/19/20 | 同客户双平台只放行 1 条 |
| M3 | **审计 append-only + 触发器拒改 + 哈希链** | FR-27 | UPDATE 被拒；篡改可定位 |
| M4 | **路径完整性断言**（含 `confirm_plan`） | FR-25 / ADR-035 | 跳过 `confirm_plan` → DENY |
| M5 | **审批 interrupt + 任务级批量 + 三档路由** | FR-26 / 裁决 #8/#25 | 挂起/恢复跑通；三档路由正确 |
| M6 | **LangGraph 执行循环**（5 节点：segment→decision→**confirm_plan**→gate(interrupt)→send(mock)） | FR-04 / ADR-035 | 5 节点图跑通；跳过 `confirm_plan` → 断言 DENY |
| M7 | **模拟适配器 + 合成数据**（2000 条） | FR-09/10 / 裁决 #16 | 数据可查、样本自洽 |
| M8 | **RBAC 4 角色 + 管理员强制放行被拒** | FR-30 / 裁决 #23 | 管理员对 DENY 客户强制放行 → 被拒 |
| M9 | **API 层 + 统一响应信封** | FR-38 | OpenAPI 快照入库；**每个 error.code 映射到某条 E 场景，或显式登记为非业务错误码**（PRD 附录 J.6）。MVP 覆盖 E1、E3–E11、E14、E15（E3 = LLM 超时重试 1 次转人工，G-06 / 架构 §I.2 INV-5）；E2/E12/E13/E16 属 v0.2/v0.3 范围 |
| M10 | **前端 5 屏**（任务下达+计划确认 / 批量审批 / 决策回放 / 治理看板 / 系统配置） | FR-05/25/26/29/33/38 | **演示脚本 §5.2 逐屏点击全通** |
| M11 | **评测集 20 条 + CI 回放对照** | FR-29 / 裁决 #19 | 策略命中一致率 100% |
| M12 | **熔断一键停** | FR-28 / ADR-030 | 熔断后新动作停止派发 |
| M13 | **README + 演示脚本 + 核心 ADR** | W13 / 裁决 #20 / FD-7 | **演示脚本 §5.2 逐屏点击版全通过**（唯一版本；原 4 步叙述版已作废） |

### 1.2 MVP 明确不做（后置到后续版本）

| # | 不做项 | 后置到 | 原因 |
|---|--------|--------|------|
| X1 | **记忆完整性门禁**（W6b） | v0.3 | 演示不涉及记忆写入；表结构先留 |
| X2 | **Jev 接入 + 降级链 + C9 出境管控**（W7） | v0.2 | MVP 用确定性规则替代判断模型。**但屏 5 需展示出境开关的当前状态（只读）**——这是权限可见性演示，不是出境管控能力实现。**开关的写操作与 L1/L2/L3 三层管控逻辑 v0.2 才做** |
| X3 | **话术生成 + 合规 RulePack + 注入防御**（W8） | v0.2 | 发送内容用固定模板；种子内容包规格已冻结待起草 |
| X4 | **RAG 检索**（W10） | v0.5 | 无需检索即可闭环 |
| X5 | **MCP Server 对外暴露**（W11） | v0.4 | 工具用本地函数直调 |
| X6 | **技能蒸馏产出逻辑**（W15a 产出部分） | v0.5 | **但 `skill_card` 表 + 状态机 MVP 建**（G-18） |
| X7 | **策略工作台 UI**（FR-17） | v0.2 | MVP 用 `governance.yaml` + 完整审批链路（G-03）。**注意：屏 5 系统配置 ≠ 策略工作台**，前者只管 provider 与出境开关 |
| X8 | **数字员工配置界面**（FR-01c） | v0.2 | **但 `worker`/`worker_template` 表 MVP 建** |
| X9 | **数字员工多实例 / 跨员工冲突** | v0.2 | MVP 单员工（FR-01d 标 P0 但 v0.2 才可触发） |
| X10 | **审批收敛三件套**（W18） | v0.2 | W5 任务级批量审批已覆盖主链路 |
| X11 | **真实适配器 / 真实电商数据 / 前端接真 API** | v0.7 / v0.8 | MVP 前端走 Mock 适配层；**真 API 接入时前端零改动**（`ApiClient` 接口已冻结） |
| X12 | **可观测性 SaaS**（LangSmith 等） | 后续版本 | MVP 自建成本表（N6） |
| X13 | **分区表 / Redis 集群** | 后置 | MVP 普通表 + 复合索引（N8） |

### 1.3 后续版本范围（不在本次拆解内）

| 版本 | 范围 | 关键验证 |
|------|------|---------|
| **v0.2** | 多数字员工（2 个可运行）+ 用户创新新员工 + **员工配置界面** + 跨员工冲突消解 + **策略工作台 UI** + L2 开放 + L1 白名单豁免 + 审批收敛三件套 + Jev 接入 + 话术生成与合规 RulePack | 是否真的可定制；多员工语义是否成立 |
| **v0.3** | 执行循环（规划/工具/记忆/反思）+ 记忆完整性门禁 + **智能体上线**（工具+权限、一次性、完整闸门、权限放大禁令） | 自主执行是否可控；权限隔离是否有效 |
| **v0.4** | MCP Server + 工具生态 + **多智能体协作**（声明式拓扑、责任唯一、各过各的闸门） | 工具能否被外部安全复用 |
| **v0.5** | 全自动蒸馏管线 + Prompt 沉淀 + 技能参数自优化 + 直播推广场景包 | 是否真能越用越强 |
| **v0.6** | 成本优化（Jev 路由、Prompt 缓存、上下文压缩、语义缓存） | 成本是否可测可降 |
| **v0.7** | 企业系统真实适配器 + "接入业务代码 diff 为空"验收 | Adapter 抽象是否成立 |
| **v0.8** | 真实电商平台数据接入 | 端到端真实数据闭环 |
| **v1.0** | 私有化部署包 + CI/CD + 完整权限组织模型 | 能否进企业 |

---

## 二、Wave 0：基础骨架（串行，所有路共享）

### T0.1 仓库骨架与依赖准入

| 项 | 值 |
|---|-----|
| 依赖 | 无 |
| 产出文件 | `pyproject.toml`, `Makefile`, `docker-compose.yml`, `.env.example`, `LICENSES.md` |
| 内容 | uv groups（`runtime`/`dev`/`evaluation`）；ruff 规则：**依赖白名单制**（依赖清单外的第三方库一律禁止 import）；`import litellm` **仅在 `providers/` 目录内允许**（N4 拍板为 B：litellm 作独立 provider 适配层引入，业务层不得直接依赖） |
| 验收命令 | `uv sync && docker compose up -d` |

### T0.2 核心数据模型（迁移 + 模型）

| 项 | 值 |
|---|-----|
| 依赖 | T0.1 |
| 产出文件 | `migrations/001_core_tables.sql`, `src/models/*.py` |
| 表清单 | `customer`, `customer_identity`, `customer_segment`, `action_candidate`, `decision`, `audit_log`, `suppression_record`, `approval`, `tool_call`, `worker`, `worker_template`, `worker_metrics`, `skill_card`, `cost_record` |
| **追加表** | `task_checkpoint` 由 **T-E7** 建（G-04 位点表），其迁移文件是 Wave 3 内唯一的迁移；**其余全部表 + `audit_log` 触发器在本任务一次性建**（Wave 0 统一建表，避免同一 wave 两个分支各写迁移，违反实施计划 §3.4「每个 wave 只允许一个分支写迁移」） |
| 关键约束 | `customer` 无明文 PII 列 + consent 三字段；`audit_log` append-only 触发器；`worker.memory_scope_key` 格式 `{tenant_id}:{worker_id}`；`skill_card` 含 `draft`/`published` 状态 |
| 验收命令 | `docker compose exec db psql -c "\dt" | wc -l` ≥ 15 |

### T0.3 CI 骨架 + 契约门禁配置

| 项 | 值 |
|---|-----|
| 依赖 | T0.1 |
| 产出文件 | `.github/workflows/ci.yml`, `ruff.toml`, `pyproject.toml [tool.pytest.ini_options]` |
| 门禁 | pytest + ruff + format-check + mypy + **真 PG 集成测试**（C13）+ **零供应商依赖扫描**（红线⑤） |
| 验收命令 | `make check` 全绿 |

### T0.4 种子 governance.yaml

| 项 | 值 |
|---|-----|
| 依赖 | T0.1 |
| 产出文件 | `governance.yaml` |
| 内容 | 九宫格分层阈值 + 频控参数（7天≤2 / 同通道72h）+ 抑制红线 + 冲突窗口 72h + 权益净值折算参数 + 审批路由三档与超时 + 预算 + 蒸馏阈值 N=3 |
| 验收 | `governance.yaml` 存在且能被 `policy_kernel.load_and_validate()` 成功加载 |

---

## 三、Wave 1：三路并行（真独立）

### 【A】W1 骨架 + 合成数据 + 模拟适配器

#### T-A1 合成数据生成器

| 项 | 值 |
|---|-----|
| 依赖 | T0.2 |
| 产出文件 | `src/data/generator.py`, `src/data/schemas.py` |
| 规格 | `--count` 参数化，默认 10000，MVP 演示用 2000（G-16）；全部标 `_synthetic: true` |
| 五类风控样本 | 高投诉 / 高退款 / 明确拒绝 / 无授权 / Top1%，各类 ≥10（MVP 量） |
| 验收命令 | `python -m src.data.generator --count 2000 && docker compose exec db psql -c "SELECT count(*) FROM customer WHERE _synthetic=true"` → 2000 |

#### T-A2 模拟适配器

| 项 | 值 |
|---|-----|
| 依赖 | T0.2 |
| 产出文件 | `src/adapters/mock_adapter.py` |
| 内容 | 实现 `JudgmentProvider` + `ModelProvider` 协议，返回确定性桩数据；**E3 重试语义按架构 §I.2 INV-5 / G-06：重试收敛在 ModelProvider 内、最多 1 次、重试写审计、失败转人工待办**（桩数据须可模拟 LLM 超时触发该路径） |
| 验收 | `pytest tests/adapters/` 全绿；**E3 超时桩：触发后重试恰 1 次、转人工、审计有记录** |

### 【B】W2 策略内核（零依赖纯函数）

#### T-B1 决策数据结构 + 规则 ID 枚举

| 项 | 值 |
|---|-----|
| 依赖 | T0.1 |
| 产出文件 | `src/policy/kernel.py` — `Decision` enum（ALLOW/REQUIRE_APPROVAL/DENY）、`RuleId` 枚举（**必须与附录D §4 登记表一致**） |
| 验收 | `python -c "from src.policy.kernel import Decision, RuleId; print(len(list(RuleId)))"` ≥ 9 |

#### T-B2 六条规则实现

| 项 | 值 |
|---|-----|
| 依赖 | T-B1, T0.4 |
| 产出文件 | `src/policy/rules.py`（规则 1–6）+ `src/policy/segment.py`（九宫格分层） |
| 规则 | ① 抑制红线 ② 全局频控 ③ 冲突窗口三键排序 ④ 层级分维度取判 ⑤ 权益净值 ⑥ 升级人工 |
| **必须人工设计 + 逐行 review** | 架构 v5.2 §12.2：`policy_kernel` 与路径断言**不建议交给 AI 全权生成** |
| **唯一实现点** | 六条规则的判定逻辑**只在本文件实现一次**。`src/decision/` 下的模块只做数据接入与调用，**不得复制判定逻辑**（否则出现两个真相来源，一致率门禁失效） |
| 验收 | `pytest tests/policy/` 全绿；DENY 分支全覆盖；**review 记录写入 PR** |

#### T-B3 规则加载与 fail-fast 校验（G-05）

| 项 | 值 |
|---|-----|
| 依赖 | T-B2, T0.4 |
| 产出文件 | `src/policy/config.py` — `load_and_validate()` |
| 验收 | ① 合法配置加载成功 ② 非法配置（红线缺字段/阈值越界）→ 抛 `RuleValidationError` |

#### T-B4 FastAPI lifespan fail-fast 接入（G-05）

| 项 | 值 |
|---|-----|
| 依赖 | T-B3, T0.3 |
| 产出文件 | `src/app.py` — lifespan 中调 `load_and_validate()`，失败即 raise |
| 验收 | 故意写错 `governance.yaml` → 启动进程退出（**不静默继续运行**） |

### 【C】W3 统一层

#### T-C1 身份匹配引擎（三档）

| 项 | 值 |
|---|-----|
| 依赖 | T0.2 |
| 产出文件 | `src/unification/identity.py` |
| 规格 | 高置信（`ext_key` 精确）→ 自动归集；中置信（手机号哈希+地址指纹）→ 人工复核队列（E4）；低置信 → 保留独立。**零 LLM** |
| 验收 | `pytest tests/unification/identity`；低置信样本**未被合并** |

#### T-C2 分层引擎 + 运营指标口径

| 项 | 值 |
|---|-----|
| 依赖 | T0.2 |
| 产出文件 | `src/unification/segmentation.py`, `src/metrics/` |
| 规格 | 九宫格分层（全局 + 平台局部各一套，裁决 #11）；运营指标四项口径冻结（采纳率 / 人均处理数 / 候选→发出时长 / 周期=任务批次，裁决 #21） |
| **唯一实现点** | 分层算法在 `src/unification/segmentation.py`（本任务）；T-B2 只**调用**它，不另写一份。`layer_score` 比较式见《附录D_冻结版》规则 3 |
| 验收 | `pytest tests/unification/segmentation` + `pytest tests/metrics` |

---

## 四、Wave 2：W4 决策层（依赖 B + C + 附录D冻结）

#### T-D1 冲突消解引擎

| 项 | 值 |
|---|-----|
| 依赖 | T-B2, T-C2 |
| 产出文件 | `src/decision/resolver.py` |
| 规格 | 规则 3 三键排序 + 规则 4 层级分维度；被抑制候选写 `suppression_record`，rule_id=`CONFLICT_WINDOW_72H` |
| 验收 | 同客户双平台 → 只放行 1 条；另一条 DENY 且留痕 |

#### T-D2 抑制与频控的数据接入（判定逻辑在 T-B2，不重复实现）

| 项 | 值 |
|---|-----|
| 依赖 | T-B2 |
| 产出文件 | `src/decision/suppression.py`, `src/decision/rate_limit.py` |
| **注意** | **不在此重复实现规则**——抑制/频控的判定逻辑唯一实现在 `src/policy/rules.py`（T-B2）；本任务只做**数据接入**（读 `customer`/`suppression_record`/`tool_call` 并喂给内核）。**唯一实现点**：`src/policy/rules.py`，`src/decision/` 下不得出现第二份抑制/频控判定逻辑 |
| 验收 | ① 投诉客户 → DENY ② 频控第 3 次 → DENY ③ 同通道 <72h → DENY |

#### T-D3 影响面计算

| 项 | 值 |
|---|-----|
| 依赖 | T-B1 |
| 产出文件 | `src/decision/impact.py` |
| 规格 | 按客户数计算，同一客户多条动作只计 1 次（G-02）；三档路由按**裁决 #25 原文**（≤10 负责人单签 / 11–100 负责人+管理员双签 / >100 双签+合规知会） |
| **唯一实现点** | 三档路由判定在 `src/decision/impact.py`（本任务）；T-E2 的审批模块只**调用**它，不另写一份 |
| 验收 | `pytest tests/decision/impact` |

---

## 五、Wave 3：W5 + W6 执行链与治理（依赖 Wave 2）

#### T-E1 LangGraph 执行图（4 步 + confirm_plan）

| 项 | 值 |
|---|-----|
| 依赖 | T-D1, T-D2, T-D3 |
| 产出文件 | `src/graph/workflow.py` |
| 节点 | `segment → decision → **confirm_plan** → gate(interrupt) → send(mock)` |
| **硬约束** | `confirm_plan` **必须保留**（ADR-035：置于 entry 后、validate 前，纳入 `REQUIRED_NODES`，所有自主度等级必需）。**没有它，路径断言演示不成立** |
| 验收 | `python -m src.graph.workflow --dry-run` 走通全图 |

#### T-E2 治理闸门 + 审批 interrupt

| 项 | 值 |
|---|-----|
| 依赖 | T-E1 |
| 产出文件 | `src/graph/gate.py`, `src/approval/router.py` |
| 内容 | DENY → 终止；REQUIRE_APPROVAL → `interrupt()` 挂起 → 审批 → `Command(resume=)` 恢复；**恢复后以当前 rule_version 重新判定**（ADR-030） |
| 验收 | ① DENY 客户进 execute → 闸门拒绝 ② REQUIRE_APPROVAL → 挂起，审批后恢复 ③ 审批三档路由正确 |

#### T-E3 审计 append-only + 触发器 + 哈希链

| 项 | 值 |
|---|-----|
| 依赖 | T0.2 |
| 产出文件 | `src/audit/hash_chain.py`（触发器迁移 `migrations/002_audit_triggers.sql` **归 T0.2 批次**——Wave 0 一次性建全部表与触发器，避免同一 wave 两个分支各写迁移） |
| 验收 | ① **先插入至少 2 条真实记录**（空表 UPDATE 不触发触发器——验收无意义）② `UPDATE audit_log ...` → 触发器拒绝 ③ 手动改一条 `prev_hash` → `python -m src.audit.hash_chain --verify` 报错并定位到第 N 条 |

#### T-E4 路径完整性断言

| 项 | 值 |
|---|-----|
| 依赖 | T-E1 |
| 产出文件 | `src/graph/path_assert.py` |
| 规格 | `visited_nodes ⊇ REQUIRED_NODES`，`REQUIRED_NODES` 含 `confirm_plan`；缺失即 DENY + 告警（E14） |
| **必须人工设计** | 架构 v5.2 §12.2 同 `policy_kernel` |
| 验收 | 端到端：构造"跳过 `confirm_plan`"的图 → 断言 DENY |

#### T-E5 RBAC 4 角色 + 权限矩阵

| 项 | 值 |
|---|-----|
| 依赖 | T0.2 |
| 产出文件 | `src/auth/rbac.py`, `src/auth/roles.py` |
| 规格 | 4 角色（管理员/运营负责人/运营专员/合规，裁决 #23）；按 PRD 十一章矩阵 + ⚠️ 脚注 a1–a10（G-12 整包采纳）实现；**管理员不自动蕴含下级权限**（ADR-044 / K-M3） |
| 验收 | ① 管理员对 `SUP_HIGH_VALUE` 客户强制放行 → **被拒** ② 越权调用被拒并审计 |

#### T-E6 熔断（一键停）

| 项 | 值 |
|---|-----|
| 依赖 | T-E2 |
| 产出文件 | `src/graph/circuit_breaker.py` |
| 规格 | 全局开关；停止派发 + 挂起排队；**恢复 = 挂起动作以当前 rule_version 重新判定**（ADR-030 / FR-28） |
| 验收 | 熔断后新动作不派发；恢复后重判且写审计 |

#### T-E7 位点表（G-04）

| 项 | 值 |
|---|-----|
| 依赖 | T0.2, T-E1 |
| 产出文件 | `migrations/003_task_checkpoint.sql`（**Wave 3 内唯一的迁移文件**——T-E3 的触发器迁移归 T0.2 批次，见下） |
| 规格 | `task_checkpoint` 存**审计级位点索引**（`checkpoint_id`/`tenant_id`/`task_id`/`node_name`/`checkpoint_at`/`decision_id`/`langgraph_checkpoint_id`）；**不存 `graph_state_snapshot`**——恢复用 LangGraph checkpointer，审计用本表 |
| 验收 | 查询接口 `GET /api/v1/tasks/{task_id}/checkpoints` 返回位点列表（FR-14） |

---

## 六、Wave 4：API 层 + 前端 + 评测 + 文档

### 【E】W9a 后端 API 实现（依赖 Wave 3）

#### T-F1 API 契约冻结 + 统一响应信封

| 项 | 值 |
|---|-----|
| 依赖 | T-E2, T-E5, T-E7 |
| 产出文件 | `src/api/routes/`, `src/api/schemas/`, `openapi.json` |
| 内容 | 统一响应信封携带 `trace_id` + 四版本号快照；错误响应携带 `error.code` + `rule_id` + `retryable`；**每个 error.code 映射到某条 E，或显式登记为非业务错误码**（FR-38，PRD 附录 J.6 口径） |
| 验收 | `openapi.json` 快照入库；CI 快照比对通过 |

#### T-F2 契约测试七项门禁（FR-39）

| 项 | 值 |
|---|-----|
| 依赖 | T-F1 |
| 产出文件 | `tests/contract/` |
| 七项 | ① OpenAPI 快照一致 ② 契约测试全绿 ③ 错误码完备性 ④ 所有写端点写审计 ⑤ 对外触达端点过闸门 ⑥ **真 PG 集成测试进 CI** ⑦ **必经节点不可 opt-in**（CI 静态扫描确认无跳过分支） |
| 验收 | 七项全部进 CI，未过则阻断合并 |

### 【F】W12 评测集 + CI 回放（可与 E 并行）

#### T-G1 评测标注基准

| 项 | 值 |
|---|-----|
| 依赖 | T0.4, 附录D 冻结版 |
| 产出文件 | `tests/evaluation/benchmark.yaml`（源自 `docs/specs/评测标注基准_v1.2.yaml`）+ `tests/evaluation/path_assertions.yaml` |
| 规格 | ① **策略判定 20 条**（15 简单 + 5 边界）→ 一致率分母；② **路径断言 5 条**（跳过各必经节点的用例，单独文件）→ **独立验收，不计入一致率分母**。rule_id 全部取自附录D §4 登记表 |
| **对照口径** | CI 比对三要素全部一致：① `conclusion` 完全相同 ② `hit_rule_ids` 集合相同 ③ `suppressed_rule_ids` 集合相同 + `suppressed_candidates` 数量相同 |
| **纪律** | **标注先于内核入库**——本任务必须在 T-B2 之前完成，否则一致率是循环论证 |
| 验收 | `pytest tests/evaluation/test_benchmark_schema` 加载成功；5 条路径断言样本齐备 |

#### T-G2 CI 回放对照

| 项 | 值 |
|---|-----|
| 依赖 | T-G1, T-B2 |
| 产出文件 | `tests/evaluation/test_regression.py` |
| 验收 | CI 跑回放 → 策略命中一致率 100%（20 条策略样本）；**路径断言 5 条独立计**（不是策略样本，不计入一致率分母） |

### 【G】W9b 前端 5 屏（依赖 FE 层，Mock 模式）

> **前端技术决策见《开发实施计划》§五之二**（FD-1~FD-7）。要点：**React + TS + Vite + AntD + ECharts + TanStack Query**；**MOCK 数据 + 真实交互**；`ApiClient` TS 接口 + 双实现 + `VITE_API_MODE` env 开关；真实登录 + session（token 存 localStorage）。

#### T-FE1 工程脚手架

| 项 | 值 |
|---|-----|
| 依赖 | 无 |
| 产出文件 | `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`, `web/.env.example` |
| 内容 | Vite + React 18 + TS + Ant Design + ECharts + TanStack Query + ESLint + Prettier |
| 验收命令 | `cd web && npm run build` 通过 |

#### T-FE2 ApiClient 接口与类型

| 项 | 值 |
|---|-----|
| 依赖 | T-FE1 |
| 产出文件 | `web/src/api/types.ts` — 5 屏全部数据类型 + `ApiClient` 接口 |
| 约束 | 类型须与架构 v5.3 §F.2 响应信封 / §F.3 端点清单一致 |
| 验收 | `npm run typecheck` 通过；接口方法与 §F.3 端点逐一对应（每个方法标注对应端点路径，覆盖 5 屏） |

#### T-FE3 Mock 数据生成脚本

| 项 | 值 |
|---|-----|
| 依赖 | T-FE1 |
| 产出文件 | `web/scripts/generate-mock.ts`, `web/src/mocks/snapshot.json` |
| 内容 | 固定 seed（可复现）生成 2000 客户 + 决策结果 + 审计哈希链 + 审批批次 + 抑制记录 + 运营指标 |
| 验收命令 | `npm run mock:generate` 幂等（两次生成产物一致） |

#### T-FE4 MockApiClient 实现

| 项 | 值 |
|---|-----|
| 依赖 | T-FE2, T-FE3 |
| 产出文件 | `web/src/api/mock/MockApiClient.ts`, `web/src/api/mock/state.ts` |
| **关键约束** | **必须实现真实状态变更语义**（审批签署后列表状态变化、强制放行被拒并返回错误信封、审计篡改报错、哈希链校验定位）。**Mock 是数据源替换，不是交互降级** |
| 错误格式 | 与 `RealApiClient` 完全一致（统一信封 + `error.code` + `rule_id` + `trace_id`） |
| 验收 | `npm run test:mock` —— 覆盖：登录、审批签署、强制放行拒绝、审计篡改、哈希链校验 |

#### T-FE5 登录页 + 权限路由

| 项 | 值 |
|---|-----|
| 依赖 | T-FE4 |
| 产出文件 | `web/src/pages/Login.tsx`, `web/src/auth/`（token 存储、守卫、4 角色路由） |
| 演示账号 | 4 角色账号（admin / owner / operator / compliance）；**口令由种子脚本生成并写入本地 `.env`，不入库** |
| 验收 | ① 4 账号均可登录 ② token 存 localStorage + 请求带 Authorization 头 ③ 未登录访问受保护路由被重定向 |

#### T-FE6 布局与导航

| 项 | 值 |
|---|-----|
| 依赖 | T-FE5 |
| 产出文件 | `web/src/layouts/MainLayout.tsx`, `web/src/router.tsx` |
| 内容 | 5 屏路由 + 顶部 trace_id + 当前用户/角色 + 按角色隐藏无权入口 |
| 验收 | 5 屏路由可达；运营专员看不到系统配置入口 |

#### T-FE7 屏 1：任务下达 + 计划确认

| 项 | 值 |
|---|-----|
| 依赖 | T-FE6 |
| 产出文件 | `web/src/pages/TaskCreate.tsx`, `web/src/pages/PlanConfirm.tsx` |
| 验收 | ① 自然语言下达任务 → 生成计划 ② 计划详情展示步骤/工具/影响范围 ③ **「确认计划」按钮**可用（`confirm_plan` 是 ADR-035 必经人工节点，跳过它后续路径断言会 DENY） ④ 展示双平台冲突客户的两个候选 |

#### T-FE8 屏 2：任务级批量审批

| 项 | 值 |
|---|-----|
| 依赖 | T-FE6 |
| 产出文件 | `web/src/pages/ApprovalList.tsx`, `web/src/pages/ApprovalDetail.tsx` |
| 验收 | ① 待审批批次列表（按影响面档位筛选） ② 批次详情含候选名单快照 + 证据包 + 影响面统计 ③ 签署/驳回可用 ④ **「强制放行」按钮对 `SUP_HIGH_VALUE` 批次 → 被拒并展示错误信封**（核心爆点） |

#### T-FE9 屏 3：决策回放

| 项 | 值 |
|---|-----|
| 依赖 | T-FE6 |
| 产出文件 | `web/src/pages/DecisionReplay.tsx` |
| 验收 | ① 判定详情（命中规则 ID + 四版本号 + 输入快照） ② **3 步还原**展开完整决策链 ③ **「尝试修改」审计记录 → 报错（append-only）** ④ **「校验哈希链」→ 报错并定位到第 N 条** |

#### T-FE10 屏 4：治理看板

| 项 | 值 |
|---|-----|
| 依赖 | T-FE6 |
| 产出文件 | `web/src/pages/GovernanceBoard.tsx`, `web/src/pages/SuppressionList.tsx` |
| 验收 | ① ECharts 饼图：按命中规则 ID 分布 ② 未触达列表（按 rule_id/时间/客户分组） ③ 运营效率四项指标 ④ 「评测集」标签页显示一致率 100% |

#### T-FE11 屏 5：系统配置

| 项 | 值 |
|---|-----|
| 依赖 | T-FE6 |
| 产出文件 | `web/src/pages/SystemConfig.tsx` |
| 验收 | ① provider 列表 + capabilities ② 切换活跃 provider（仅运营负责人/管理员可见可操作） ③ **出境开关只读展示**（展示当前 `third_party_inference.enabled` 值 + 角色可见性差异；**写操作与三层管控逻辑 v0.2 才实现**，见 §1.2 X2） |

#### T-FE12 RealApiClient 骨架

| 项 | 值 |
|---|-----|
| 依赖 | T-FE2 |
| 产出文件 | `web/src/api/real/RealApiClient.ts` |
| 范围 | **只保证结构与 Mock 对齐**（方法签名一致、错误信封一致），**不实现真 fetch**（真接入属 v0.7） |
| 价值 | **适配层结构在 MVP 就冻结**，避免后续接真 API 时大改 |
| 验收 | `RealApiClient` 与 `MockApiClient` 通过同一份 `ApiClient` 类型检查 |

#### T-FE13 演示脚本走查

| 项 | 值 |
|---|-----|
| 依赖 | T-FE7..T-FE11 |
| 验收 | 《开发实施计划》§5.2 逐屏点击脚本**全通**，含 4 个演示账号的角色切换 |

### 【H】W13 文档

#### T-I1 README + 演示脚本 + ADR

| 项 | 值 |
|---|-----|
| 依赖 | T-FE13 |
| 产出文件 | `README.md`, `docs/demo/演示脚本.md`, `docs/adr/` |
| 规格 | 演示脚本 §5.2 逐屏点击版（FD-7 修订；G-17 冻结的 4 步叙述版已作废）；README 口径须与 PRD v5.3 一致（自主度语义 L1、C1–C15、16 类异常、路线图含 v0.3/v0.4）；**README 不宣称"越用越强"**（G-18：MVP 无蒸馏产出逻辑） |
| 验收 | 演示脚本 §5.2 逐屏点击全流程通过（5 屏） |

---

## 七、依赖图

```
Wave 0   T0.1 ──┬── T0.3
                ├── T0.4
                └── T0.2
                    │
Wave 1  ┌──────────┼──────────┐
        ▼          ▼          ▼
      【A】      【B】      【C】
      T-A1      T-B1        T-C1
      T-A2      T-B2  ◄── T-G1(标注先于内核!)
                T-B3        T-C2
                T-B4
        │          │          │
        └──────────┼──────────┘
                   ▼
Wave 2    ┌──────┴──────┬──────────┐
          ▼             ▼          ▼
       T-D1          T-D2       T-D3
    (依赖T-B2,      (依赖T-B2)  (依赖T-B1)
     T-C2)              │          │
          └──────┬──────┴──────────┘
                 ▼
Wave 3        T-E1 ──→ T-E2
              │         ├──→ T-E6 (熔断)
              │         └──→ T-E7 (位点表, 依赖T-E1)
              ├──→ T-E4 (路径断言, 依赖T-E1)
              ├──→ T-E3 (审计, 依赖T0.2)
              └──→ T-E5 (RBAC, 依赖T0.2)
                 │
    ┌────────────┼──────────────────────┐
    ▼            ▼                      ▼
Wave 4  【E】   【F】              【G】前端 (Mock模式)
    T-F1       T-G1→T-G2          T-FE1 ──┬──→ T-FE2 ──→ T-FE4 ──→ T-FE5 ──→ T-FE6
    T-F2         │                T-FE3 ──┘                              │
      │          │                                                   ▼
      │          │                              T-FE7 / T-FE8 / T-FE9 (三路并行)
      │          │                                                   │
      │          │                              T-FE10 / T-FE11 (两路并行)
      │          │                                                   ▼
      │          │                                          T-FE12 ──→ T-FE13
      ▼          ▼                                                │
   【H】文档  T-I1 (依赖T-FE13) ◄───────────────────────────────────┘
```

**关键并行约束**：
- **T-G1（标注）必须在 T-B2（内核）之前**——否则"一致率 100%"是循环论证
- T-A/T-B/T-C 三路真独立（T-B 是纯函数零依赖）
- **Wave 2 的 T-D1/T-D2/T-D3 三路可并行**（各自依赖 T-B2/T-C2/T-B1，彼此无依赖）
- **Wave 3 的 T-E3/T-E4/T-E5/T-E6/T-E7 可并行**（T-E4/T-E6/T-E7 依赖 T-E1；T-E3/T-E5 只依赖 T0.2）
- **前端（【G】）在 Mock 模式下与后端并行**——只依赖 `ApiClient` 接口定义（T-FE2），不依赖后端实现；T-FE12 保证后续接真 API 时零改动
- **T-FE3（Mock 数据生成）可与 T-FE2 并行**（T-FE3 只依赖 T-FE1）
- **T-I1（文档）依赖 T-FE13**（README 要写演示脚本，脚本要等前端走查完才能定稿）

---

## 八、封板验收清单

| # | 验收项 | 验证方式 | 任务 |
|---|--------|---------|------|
| 1 | 策略命中一致率 **100%**（20 条策略样本回放对照） | CI `test_regression.py` | T-G2 |
| 2 | 路径断言缺失节点 DENY 率 **100%**（5 条，含跳过 `confirm_plan`） | E2E 测试 | T-E4 |
| 3 | 审计 UPDATE/DELETE **被触发器拒绝** | psql 手动验证（表内有 ≥2 条真实记录） | T-E3 |
| 4 | 哈希链篡改后校验脚本 **报错并定位到第 N 条** | `python -m src.audit.hash_chain --verify` | T-E3 |
| 5 | 管理员强制放行 DENY 客户 **被拒** | E2E 测试 | T-E5 |
| 6 | 无授权客户（`NO_CONSENT`）触达 **0** | SQL 统计（发送前校验，规则 1b） | **T-E2**（发送阶段 consent 校验）+ T-D2（数据接入） |
| 7 | 跨平台强行合并 **0**（低置信保留独立） | 数据查询 | T-C1 |
| 8 | **演示脚本 §5.2 逐屏点击全通（5 屏）** | 逐屏点击 + 4 账号角色切换 | **T-FE13** |
| 9 | 契约门禁七项全过 | CI | T-F2 |
| 10 | 零供应商依赖扫描无命中 | CI | T0.3 |
| 11 | **README 口径与 PRD v5.3 一致**（自主度 L1 语义、C1–C15、16 类异常、路线图含 v0.3/v0.4；**不宣称"越用越强"**） | 人工 review | T-I1 |

---

## 九、两周排期（10 工作日）

| 日 | 主线 | 并行 | 封板检查 |
| --- | --- | --- | --- |
| **D1** | Wave 0：骨架 + 表 + CI + governance.yaml | — | 环境一键起；`make check` 通过 |
| **D2** | **T-G1 标注基准冻结**（必须在内核前） | T-A1 数据生成器 | 标注入库；数据可查 |
| **D3** | T-B1/B2 规则实现 | T-A2 适配器；T-C1 身份匹配 | `pytest tests/policy` 全绿；**内核逐行 review** |
| **D4** | T-B3/B4 fail-fast；T-C2 分层 | T-E5 RBAC | 规则校验生效；RBAC 4 角色就位 |
| **D5** | **T-D1/T-D2/T-D3 决策层** | T-E3 审计+哈希链 | 冲突消解 + 三档路由正确；审计触发器就位 |
| **D6** | **T-E1/T-E2 执行图 + 闸门 + 审批** | T-E6 熔断；T-E7 位点表 | **5 节点图跑通**（含 confirm_plan）；挂起/恢复通过 |
| **D7** | T-F1 API 契约冻结；**T-E4 路径断言**（依赖 T-E1 已完成） | T-G2 CI 回放对照；**T-FE1~FE5 脚手架+接口+Mock数据+MockApi+登录** | 一致率 100%；契约快照入库；**前端可登录**；跳节点 DENY 验证通过 |
| **D8** | T-F2 契约测试七项 | **T-FE6 布局 + FE7/FE8/FE9 屏1/2/3** | 门禁全过；**屏 1–3 可走** |
| **D9** | **T-FE10/FE11 屏 4/5**；T-FE12 RealApiClient 骨架 | 缓冲 | **5 屏全部可走** |
| **D10** | **T-FE13 演示脚本走查** → **T-I1 文档**（依赖 FE13） + 缓冲 + 封板检查 | 缓冲 | **演示脚本逐屏点击全通**；封板清单 11 项全过 |

**D10 若未通 —— 保命预案**（按此顺序砍，保演示闭环）：
1. 屏 5 系统配置 → 只读展示，不做 provider 切换与出境开关写入
2. 屏 4 ECharts 图表 → 只出数字不出图（表格兜底）
3. 熔断恢复重判语义 → 只保留全局开关（一键停）
4. 契约门禁第 ⑥/⑦ 项 → 降级为人工执行（**不删，标为"待补"**）

**不可砍**（砍了就没有差异化）：策略内核 + 审计哈希链 + 路径断言（含 `confirm_plan`）+ 管理员强制放行被拒 + 审批挂起恢复 + **屏 2 / 屏 3**（演示核心爆点全在这两屏）。

---

> 本拆解基于 G-15 拍板结论：禁止按技术层切，三路并行仅限真独立项（W1/W2/W3），W4→W5→W9 强依赖串行。拆解结果受 git 跟踪，供多 worktree 协同。