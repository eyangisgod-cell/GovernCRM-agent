# GovernCRM 技术架构方案 v5.3 补充章节

| 项 | 内容 |
| --- | --- |
| 文档名称 | GovernCRM 技术架构方案 v5.3 补充章节 |
| 版本 | **v5.3**（补充章节，非完整独立文档） |
| 编写日期 | 2026-10-22 |
| 文档性质 | **对 v5.2 的增量补充**。v5.2 原件零修改，本文档与之并存生效 |
| 上游 | 《GovernCRM 技术架构方案 v5.2》《GovernCRM 产品需求文档 PRD v5.2》《GovernCRM 需求裁决表 v5.2 输入》《provider-abstraction 任务 design.md》 |
| 本版主题 | ① Provider 抽象层（JudgmentProvider / ModelProvider / EgressGate）契约落地；② **数字员工实体与配置化**（补 v5.2 数据模型缺口）；③ **API 接口层**（补 v5.2 无前后端契约缺口）；④ **数字员工 / 智能体 / 多智能体协作演进路线**（含一次性短任务智能体定义）；⑤ 开源二开选型与落点 |

> **版本纪律**：v5.2 与其 v5.1/v5.0 原件**零修改**。本补充章节以「附录 E / F / G / H / I」形式挂载到 v5.2 文档尾部，与 v5.2 正文构成完整架构规格。文档间冲突时，**本补充章节优先**（除明确标注"v5.2 保留原义"处）。

> **本补充章节解决的 v5.2 缺口**（详见附录 J 缺口对照）：

| # | v5.2 缺口 | 本章节落点 |
| - | --- | --- |
| G-A | 十三章数据模型 14 张表**无数字员工实体表**（`worker` 仅作为 `worker_id` 字段与 `worker_metrics` 表出现），PRD FR-01「数字员工创建与配置」无数据载体 | 附录 E §E.2 |
| G-B | **无 API / REST 接口层设计**，W9 前端 3.5–5 人日却无接口契约 | 附录 F |
| G-C | **数字员工配置界面完全不在 MVP**（W9 无此项，W14 后置 v0.2），与 PRD「可定制数字员工平台」定位存在落差 | 附录 G |
| G-D | 「智能体」概念在 PRD 与 v5.2 中均未定义（v5.2 3.1 明确排除多智能体自由群聊） | 附录 G §G.2 术语定义 |
| G-E | 员工间冲突规则未定义（v5.2 L13） | 附录 G §G.5 |
| G-F | Provider 抽象层仅有架构 v5.2 3.2/3.3 一句话描述，无接口契约、无降级链、无 C9 联动 | 附录 I（源自 design.md） |
| G-H | 开源二开选型无评估（十五章仅清单） | 附录 H（H.1–H.2.9，含引入清单、LICENSE 核实纪律与排除原则） |
| G-I | **无同领域真实生产项目的工程实践参照** | 附录 K（真实工程实践对照的**四条反向约束 K-M1–K-M4 + 三个工程陷阱**；对照源项目明细不入库） |
| G-J | CI 门禁清单未含 PG 特性集成测试 | 附录 F §F.4 第 ⑥ 项（K-M1） |
| G-K | 闸门完备性未禁止"必经节点 opt-in" | 附录 F §F.4 第 ⑦ 项（K-M2） |
| G-L | RBAC 未说明管理员是否蕴含下级权限 | 附录 E §E.2 第 4 条（K-M3） |
| G-M | 未登记"Prompt 被当作唯一合规手段"的风险 | 附录 I §I.5 补充纪律（K-M4） |

---

## 附录 E：数字员工实体与配置化模型

### E.1 为什么必须补

v5.2 十三章列出 14 张核心表，`worker` 从未作为实体出现——它只以 `worker_id`（GraphState 字段）与 `worker_metrics`（执行指标聚合表）的形式存在。后果是 PRD FR-01（P0）「数字员工创建与配置（角色/技能包/策略包）」**在数据模型层没有任何落点**：

- PRD 7.1 明确「一个数字员工 = 角色定义 + 技能包 + 策略包 + 记忆空间 + 自主度等级」五要素，v5.2 无一要素有对应字段或表
- 记忆空间要求「按租户 + 数字员工隔离」（PRD 7.1，G-01 拍板修正），v5.2 `memory_episode` 表无员工维度字段
- 策略包要求「分层规则、抑制规则、频控、品类周期、合规规则包」可绑定到员工，v5.2 `governance.yaml` 是**单份全局文件**，无按员工覆盖的机制
- PRD 7.3「内置数字员工模板（2 个）」隐含模板/实例二分，v5.2 无模板表

本附录补齐这些载体。**表结构在 MVP 阶段即可建（低成本），但只有「沉睡唤醒专员」一个可运行实例（裁决 #3 保持不变）**——见 §E.6 演进路径。

### E.2 核心表：`worker`（数字员工实例）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `worker_id` | UUID PK | | 员工实例 ID |
| `tenant_id` | UUID | NOT NULL, FK | 租户隔离 |
| `template_id` | UUID | NULL, FK→`worker_template` | 来源模板；`NULL` = 用户自创新员工 |
| `name` | TEXT | NOT NULL | 员工显示名（如"沉睡唤醒专员-华东组"） |
| `role_definition` | JSONB | NOT NULL | **角色定义**：职责、服务对象、语气风格、禁止事项（PRD 7.1） |
| `autonomy_level` | TEXT | NOT NULL, CHECK IN ('L0','L1','L2','L3') | 自主度等级；**MVP 有效值仅 L0/L1**（裁决 #5），L2/L3 开放随 v0.2 |
| `skill_binding` | JSONB | NOT NULL DEFAULT '{}' | **技能包绑定**：技能卡片 ID → 版本号 的映射；含工具集引用 |
| `strategy_binding` | JSONB | NOT NULL DEFAULT '{}' | **策略包绑定**：对 `governance.yaml` 种子参数的**按员工覆盖项**（未覆盖项继承全局） |
| `memory_scope_key` | TEXT | NOT NULL | **记忆空间键**：格式 `{tenant_id}:{worker_id}`；`memory_episode.scope_key` 引用此列 |
| `status` | TEXT | NOT NULL, CHECK IN ('draft','active','suspended','archived') | 生命周期；`draft` = 不可接任务 |
| `priority` | INTEGER | NOT NULL DEFAULT 100 | **员工优先级**（数值小者优先），多员工冲突消解用（§E.5） |
| `version` | INTEGER | NOT NULL DEFAULT 1 | 配置版本号，变更递增（用于 diff 与审计） |
| `created_by` | UUID | NOT NULL | 创建人 |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |

**关键约束**：

1. **`skill_binding` 与 `strategy_binding` 只存"绑定关系 + 版本号"，不存技能内容与规则内容**。技能内容在 `skill_card` 表（版本化），规则内容在 `governance.yaml`（版本化）。员工配置只引用版本 → 保证四版本号（`skill_version`/`rule_version`）可回放。
2. **`strategy_binding` 是"覆盖"不是"替换"**。`governance.yaml` 提供全局种子参数，员工只声明差量（`{"freq_control": {"window_days": 14}}`）。**理由**：策略内核的 DENY 规则是全局红线（抑制 4 条 + NO_CONSENT），若允许员工级覆盖，红线可被绕过——违反 ADR-034「核心策略参数全局可配，变更走审批 + 回归门禁」与 PRD「DENY 对所有角色不可绕过」。**实现上：`strategy_binding` 中任何试图覆盖抑制红线 / NO_CONSENT 的键，在写入时直接拒绝（校验期 DENY）**。
3. **`priority` 数值小者优先**，与 §E.5 冲突消解一致。
4. **RBAC 的继承方向单向**（v5.3 增补，见 K-M3）：**管理员不自动蕴含下级权限**。管理员角色**不能绕过 REQUIRE_APPROVAL，更不能绕过 DENY**——它只是权限矩阵中的一行，不是闸门的例外分支。此原则与第 2 条同源：**红线不可通过任何角色或配置路径绕过**。

### E.3 核心表：`worker_template`（内置数字员工模板）

| 列 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `template_id` | UUID PK | | 模板 ID |
| `tenant_id` | UUID | NULL | `NULL` = 平台内置模板；非空 = 租户自建模板（用户"创新新数字员工"的载体） |
| `code` | TEXT | NOT NULL | 稳定标识，内置模板如 `SLEEPING_WAKEUP_SPECIALIST`；**进审计与版本号** |
| `name` | TEXT | NOT NULL | 模板名 |
| `role_definition` | JSONB | NOT NULL | 角色定义模板 |
| `default_skill_binding` | JSONB | NOT NULL | 默认技能包绑定 |
| `default_strategy_binding` | JSONB | NOT NULL DEFAULT '{}' | 默认策略覆盖项 |
| `default_autonomy_level` | TEXT | NOT NULL | 新建员工默认自主度（PRD 7.2：**L0 为新建默认**） |
| `runnable` | BOOLEAN | NOT NULL | **是否可运行**。内置「售后关怀专员」`runnable=false`（PRD 7.3 口径：可创建、不可运行） |
| `is_builtin` | BOOLEAN | NOT NULL | 是否平台内置（内置模板不可删除，只能停用） |
| `version` | INTEGER | NOT NULL DEFAULT 1 | 模板版本号 |

**内置模板（对齐 PRD 7.3，MVP 不新增）**：

| code | 名称 | MVP | 说明 |
| --- | --- | --- | --- |
| `SLEEPING_WAKEUP_SPECIALIST` | 沉睡唤醒专员 | ✅ 可运行 | **MVP 唯一可运行员工**（裁决 #3） |
| `AFTER_SALE_CARE_SPECIALIST` | 售后关怀专员 | ⚠️ `runnable=false` | 仅存配置模板：可创建实例，不可派发任务 |

### E.4 关联表的员工维度补齐

| 表 | 补齐内容 |
| --- | --- |
| `memory_episode` | 新增 `scope_key TEXT NOT NULL`（引用 `worker.memory_scope_key`）；**原「按租户 + 角色隔离」升级为「按租户 + 员工隔离」**——多员工场景下不同员工的记忆不得互相污染 |
| `skill_card` | 新增 `owner_worker_id UUID NULL`（`NULL` = 平台级共享技能卡；非空 = 员工私有）；与 `worker.skill_binding` 的版本引用构成多对多 |
| `cost_record` | 已含"按员工"维度（v5.2 十章 O8），补 `worker_id` 显式外键 |
| `worker_metrics` | 已存在（v5.2 十三章），补 `worker_id` 外键与 `autonomy_level` 快照列 |

### E.5 多员工冲突消解（补 v5.2 L13）

**触发条件**：同一客户、同一时间窗口、多个员工的动作候选同时进入派发。

**规则（与附录 D 跨平台冲突消解 6 条并列，优先级更高）**：

```
① 员工间冲突优先于跨平台冲突
   同一客户同一时间窗口内，若存在来自不同 worker_id 的候选：
   → 只放行 priority 数值最小（优先级最高）的那 1 条
   → 其余 DENY，rule_id = MULTI_WORKER_CONFLICT，进 suppression_record

② 同优先级平局 → 升级人工
   priority 相同 → 不默认放行，rule_id = ESCALATE_HUMAN（同附录 D 规则 6）

③ 员工优先级可配
   worker.priority 由运营负责人配置，变更走审批 + 写审计
   —— 员工优先级是业务决策，不是技术默认值，不设"默认最高优先级"特权
```

**与附录 D 的关系**：附录 D 6 条规则处理**跨平台**冲突（同一员工、同一客户、多平台）；本节处理**跨员工**冲突（同一客户、不同员工、可能同平台）。两层叠加：先判跨员工（§E.5），再判跨平台（附录 D）。

**落地时点**：v0.2（与裁决 #3 一致）。**但数据模型（`worker.priority`）在 MVP 建表时就落地**，避免 v0.2 改表。

### E.6 演进路径（数字员工配置化）

| 阶段 | 可创建 | 可配置 | 可运行 | 配置界面 |
| --- | --- | --- | --- | --- |
| **MVP（M1）** | 仅内置「沉睡唤醒专员」 | 角色定义、名称；策略/技能只读种子值 | 仅「沉睡唤醒专员」 | ❌ 不做 |
| **v0.2（M2）** | 内置 2 个模板均可运行 + 租户自建模板 | 全五要素可配（走审批） | 多员工并行 + §E.5 冲突规则 | ✅ 员工配置页 |
| **v0.3+** | 同上 | 同上 + 员工间记忆可见性策略 | 同上 | ✅ |

> **MVP 不做配置界面的理由**（沿用 v5.2 WBS 纪律）：MVP 的目标是**完整闭环可演示**（15 步 SOP + 任务级批量审批 + 决策回放 + 治理看板 + 轻量蒸馏），配置界面不参与闭环验证。**但表结构必须先建**，否则 v0.2 要做数据迁移。**表是 MVP 的，界面是 v0.2 的**。

---

## 附录 F：API 接口层（补 v5.2 缺口 G-B）

### F.1 设计原则

1. **REST 风格，资源名复数**；路径以 `/api/v1` 前缀
2. **前端 5 屏与后端资源一一对应**，无"前端聚合后端"隐式行为
3. **所有写操作必须产生审计**（PRD C1、FR-27）
4. **所有写操作的响应携带 `version` 四版本号快照**，保证前端能展示"这次决策基于哪个版本"
5. **错误响应统一信封**，携带 `rule_id`（若来自策略内核）与 `trace_id`

### F.2 通用响应信封

```jsonc
// 成功
{
  "data": { ... },
  "meta": {
    "trace_id": "uuid",
    "version": {                    // 四版本号（4.2）
      "model_version": "deepseek/deepseek-chat/20250101@adapter-1.2.0",
      "prompt_version": "git:abc1234",
      "skill_version": "git:def5678",
      "rule_version": "governance.yaml@3"
    }
  }
}

// 错误
{
  "error": {
    "code": "E15_PROVIDER_UNSPECIFIED",
    "message": "未指定生成模型 provider。请在 config/providers.yaml 的 active_provider 指定，或在前端切换。可选：[deepseek, qwen, doubao, claude, gpt]",
    "rule_id": null,                // 若来自策略内核则为命中规则 ID
    "retryable": false
  },
  "meta": { "trace_id": "uuid" }
}
```

### F.3 端点清单（MVP）

#### 任务与计划（对应前端屏 1：任务下达 + 计划确认）

| 方法 | 路径 | 说明 | 闸门 |
| --- | --- | --- | --- |
| POST | `/api/v1/tasks` | 自然语言下达任务（`worker_id`, `text`, `provider_id?`） | 计划确认前不执行 |
| GET | `/api/v1/tasks/{task_id}` | 任务状态 + 当前 `plan` | — |
| GET | `/api/v1/tasks/{task_id}/plan` | 执行计划详情（步骤/工具/影响范围） | — |
| POST | `/api/v1/tasks/{task_id}/confirm-plan` | **确认计划**（ADR-035 必经人工节点）；确认 = 批准影响面 | 写审计 |
| POST | `/api/v1/tasks/{task_id}/cancel` | 取消任务 | 写审计 |

#### 审批（对应前端屏 2：任务级批量审批）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/approvals` | 待审批次列表（按影响面档位筛选） |
| GET | `/api/v1/approvals/{batch_id}` | 批次详情：计划 + 候选名单快照 + 证据包 + 影响面统计 |
| POST | `/api/v1/approvals/{batch_id}/sign` | 签署（按 §E 裁决 #25 三档路由校验签署人资格） |
| POST | `/api/v1/approvals/{batch_id}/reject` | 驳回（须填理由） |
| POST | `/api/v1/approvals/{batch_id}/force-release` | **强制放行**：仅管理员可调。**命中抑制红线（`SUP_*`）或 `NO_CONSENT` 的候选一律拒绝**（ADR-044 / 十一章），返回 `E_DENY_IRREVERSIBLE` |

> **批次快照不可变**：`batch_id` 关联的候选名单快照一经生成不可改（责任链绑定批次快照，ADR-032）。
> **`force-release` 是 D2 差异化的可演示接口**——管理员对 `SUP_HIGH_VALUE` 客户强制放行必须被拒，且拒绝须写审计。**这是屏 2 的核心爆点，端点不得缺失**。

#### 决策回放（对应前端屏 3）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/decisions/{decision_id}` | 判定详情：命中规则 ID + 四版本号 + 输入快照 |
| GET | `/api/v1/decisions/{decision_id}/evidence` | 证据链（RAG 片段引用、工具返回引用） |
| GET | `/api/v1/decisions/{decision_id}/replay` | **3 步还原**：返回完整决策链 |
| GET | `/api/v1/audit/records` | **审计记录列表**（分页 + 按 task_id / rule_id 筛选）——屏 3 "尝试修改"的前置数据源 |
| GET | `/api/v1/audit/verify` | **哈希链校验**：全链校验 + 结果 |
| POST | `/api/v1/audit/records/{record_id}/tamper` | **演示用篡改端点**：仅演示模式可用，模拟直接 UPDATE 被 append-only 触发器拒绝 |

> `GET /api/v1/audit/verify` 是 D2 差异化的可演示接口——现场调用即可证明审计未被篡改。
> **屏 3 的两个可演示动作需端点支撑**（原端点清单遗漏，本次补齐）：① 「尝试修改」→ `POST /audit/records/{id}/tamper`（返回触发器拒绝错误）② 审计记录列表 → `GET /audit/records`。

#### 治理看板（对应前端屏 4：治理看板 / 未触达分析）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/suppressions` | 未触达记录列表（按 rule_id / 时间 / 客户分组） |
| GET | `/api/v1/suppressions/summary` | 抑制统计：各 rule_id 命中量 + 客户数 |
| GET | `/api/v1/metrics/workers` | 运营效率四项指标（PRD 7.2 冻结口径：候选采纳率 / 人均处理数 / 候选→发出时长 / 周期） |
| GET | `/api/v1/metrics/cost` | 成本看板（按员工/任务/日；**FR-36 为 P1，看板 UI MVP 砍（W12），端点保留在契约内（R33）**；MVP 期间超预算以审计日志可查（裁决 #27）） |
| GET | `/api/v1/metrics/coverage` | **归因就绪度**：样本量、维度覆盖、是否达可开对照实验门槛（FR-34） |

#### 系统配置（对应前端屏 5）

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/v1/providers` | 已配置的 provider 列表 + capabilities | — |
| POST | `/api/v1/providers/{id}/activate` | **切换活跃 provider**（D4/D5） | 运营负责人/管理员（待权限矩阵脚注冻结） |
| GET/PUT | `/api/v1/config/egress` | 出境配置（`third_party_inference.enabled` + 白名单） | **开关仅管理员**（PRD a10） |
| GET | `/api/v1/config/compliance/egress-declaration` | 出境数据范围声明（C9 逐字段） | 合规可读 |

#### 熔断与降级

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/circuit-breaker` | **一键熔断**：停止派发 + 挂起排队 |
| POST | `/api/v1/circuit-breaker/restore` | **恢复**：挂起动作**以当前策略版本重新判定**（ADR-030，不沿用旧判定） |
| PUT | `/api/v1/workers/{worker_id}/autonomy` | 调整自主度（降级立即生效；晋级受 G1–G4 硬校验） |

#### 评测集与 CI（对应验收）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/evals/summary` | 评测集通过情况（CI 门禁结果）。**MVP 口径 = 20 条策略样本 + 5 条路径断言**（PRD 附录 J.7 FR-29 缩量）；70 条六维为全量口径，v0.2+ 补齐 |

#### 数字员工与智能体（**v0.2 开放**，MVP 返回 501）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/workers` | 员工列表（可按租户/状态/模板筛选） |
| POST | `/api/v1/workers` | **从模板创建员工实例**（FR-01） |
| GET/PATCH | `/api/v1/workers/{worker_id}` | 员工配置读写（PATCH 走审批） |
| POST | `/api/v1/worker-templates` | **创建自定义数字员工模板**（用户创新新员工） |
| GET | `/api/v1/agents` | 智能体（一次性短任务）列表 |
| POST | `/api/v1/agents` | **创建智能体**：配置工具 + 权限（见附录 G §G.2） |
| POST | `/api/v1/agents/{agent_id}/invoke` | **一次性调用**：执行完即销毁 |

### F.4 契约测试门禁

API 契约由 OpenAPI（FastAPI 自动生成）冻结，纳入 CI：

| # | 检查项 | 门禁 |
| --- | --- | --- |
| ① | OpenAPI schema 与快照一致（`openapi.json` 入库） | 变更须显式 review |
| ② | 契约测试套件（接口签名、字段、错误码、限流行为） | 全绿才可合并（对齐 ADR-038 契约测试定位） |
| ③ | **错误码完备性**：每个 `error.code` 有对应异常场景登记（E1–E16） | 新增错误码须同步登记 |
| ④ | **审计完备性**：所有 POST/PUT/PATCH 端点必须写审计 | CI 静态扫描 + 集成测试 |
| ⑤ | **闸门完备性**：所有 `external_send` 相关端点必须过治理闸门 | 同上 |
| ⑥ | **PG 特性集成测试进 CI**（v5.3 增补，见 K-M1） | **append-only 触发器、哈希链、分区表、CHECK 约束必须用真 PostgreSQL 验证**，不得只在 SQLite/内存中跑 |
| ⑦ | **必经节点不可 opt-in**（v5.3 增补，见 K-M2） | 路径完整性断言的检查点**不得设计成"调用方自觉调用"**；CI 静态扫描须确认发送/执行路径上无跳过分支 |

> **⑥⑦ 的来源**：真实工程实践对照（附录 K / K-M1、K-M2）——若配额/权限检查是 **opt-in 且不在发送路径上**，就会"必经节点可被静默跳过"：**可跳过的门等于没有门**。同理，CI 若不跑真 PostgreSQL 测试，分区表与 CHECK 约束就从未被验证。**这两条必须在本项目立项时就被门禁堵死。**

### F.5 与 v5.2 WBS 的关系

W9「前端」3.5–5.0 人日此前**无接口契约支撑**，是估算风险最高的一项。本附录 F 冻结接口后：

| 变更 | 影响 |
| --- | --- |
| W9 拆分 | W9a「后端 API 实现 + 契约测试」+ W9b「前端 **5 屏**」（任务下达+计划确认 / 批量审批 / 决策回放 / 治理看板 / 系统配置，对齐 §F.3 端点清单） |
| WBS 新增 | **W6c「数字员工/智能体数据模型（表结构 + 仓储层 + 种子模板）」（MVP 内）** |
| MVP 合计 | 架构 v5.2 §14.1 记 **约 22–27 人日**；本表 W9 拆分后为 **24–30 人日** |

> **⚠️ 2026-10-24 修订**：**人日不作为开发门禁**（AI 生成代码，人日不构成约束）。上表数字仅作历史估算留档，**范围口径以《MVP_任务拆解.md》§1.1（MVP 必须完成的 13 项 + 明确不做的 13 项）为准**。范围纪律：裁决 #1「非当前阶段功能一律做架构与数据模型级预留，不可丢弃；裁剪只作用于实现时序」。

> **注意**：W6c 只建表 + 仓储层 + 种子模板（**不建前端界面**），是"表是 MVP 的，界面是 v0.2 的"的落地。

---

## 附录 G：数字员工 / 智能体 / 多智能体协作演进路线

### G.1 术语定义（补 v5.2 缺口 G-D）

v5.2 3.1 明确「本项目是 **single-agent + 强控制平面**，不是多智能体自由群聊」，同时 PRD 通篇只用"数字员工"一词，从未定义"智能体"。为避免后续设计歧义，本节冻结术语：

| 术语 | 定义 | 生命周期 | 典型用途 | MVP |
| --- | --- | --- | --- | --- |
| **数字员工（Digital Employee）** | **长期存在、可被反复派发任务的角色实例**。身份 = 角色定义 + 技能包 + 策略包 + 记忆空间 + 自主度等级（PRD 7.1）。有记忆、会积累经验、会晋级自主度 | 长（周/月） | 沉睡客户唤醒、复购运营、售后关怀、直播推广 | ✅ 单实例 |
| **智能体（Agent / Task Agent）** | **一次性、临时的短任务执行体**。不持有长期记忆、不积累经验、不参与自主度晋级。**按"使用工具 + 权限"配置**，用完即销毁 | 短（单任务） | 临时分析、一次性数据核查、即时话术生成 | ❌ |
| **多智能体协作（Multi-Agent Orchestration）** | 多个数字员工 / 智能体按确定性规则协同完成一个复杂目标。**不是自由群聊**——必须有明确的协作拓扑、责任归属与冲突裁决 | — | 如"唤醒专员"产出候选 → "合规专员"复核 → "直播专员"执行 | ❌ v0.5+ |

**关键区分**：数字员工是**"岗位"**，智能体是**"临时工"**。

### G.2 智能体（一次性短任务）设计

#### G.2.1 数据模型：`agent_task` + `agent_profile`

```sql
-- agent_profile：智能体配置模板（可复用的"临时工岗位描述"）
CREATE TABLE agent_profile (
    agent_profile_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    code TEXT NOT NULL,                -- 稳定标识，进审计
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,             -- 用途描述
    tool_allowlist TEXT[] NOT NULL,    -- ★ 允许使用的工具白名单（工具 ID 列表）
    permission_scope JSONB NOT NULL,   -- ★ 权限范围：可读的资源维度 / 可写的数据范围
    memory_policy TEXT NOT NULL CHECK (memory_policy IN ('none','task_scoped','write_through')),
    timeout_seconds INT NOT NULL DEFAULT 60,
    max_tool_calls INT NOT NULL DEFAULT 10,
    max_tokens INT NOT NULL DEFAULT 50000,
    provider_id TEXT NULL,             -- D5：无默认值，未指定即 E15
    version INT NOT NULL DEFAULT 1,
    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- agent_task：一次性任务实例（用完即销毁，保留审计）
CREATE TABLE agent_task (
    agent_task_id UUID PRIMARY KEY,
    agent_profile_id UUID NOT NULL REFERENCES agent_profile(agent_profile_id),
    worker_id UUID NULL REFERENCES worker(worker_id),  -- 若由某数字员工发起
    task_id UUID NULL,                 -- 关联主任务
    input JSONB NOT NULL,
    output JSONB NULL,
    status TEXT NOT NULL CHECK (status IN ('running','succeeded','failed','denied','timeout')),
    tool_calls JSONB NOT NULL DEFAULT '[]',
    audit_ref UUID NOT NULL,           -- 关联 audit_log
    created_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NULL
);
```

#### G.2.2 智能体的硬边界（与数字员工的区别）

| 维度 | 数字员工 | 智能体（一次性） |
| --- | --- | --- |
| 记忆 | 有（`scope_key` 隔离，长期积累） | **默认无**（`memory_policy='none'`）；`task_scoped` = 仅本次任务内；`write_through` = 允许写回主员工记忆（须显式授权 + 走记忆门禁） |
| 自主度 | L0–L3，参与晋级 | **不参与**。智能体继承发起者的自主度约束，不独立升级 |
| 工具权限 | 技能包绑定（经审批） | **`tool_allowlist` + `permission_scope` 双白名单**，创建时冻结，调用时只减不增 |
| 生命周期 | 长期 | **单次调用即销毁**；无 `agent_task` 残留（除审计记录） |
| 治理 | 走完整闸门 | **走完整闸门**——智能体不是后门。`external_send` 类工具同样必过闸门 |

> **不可让步的一条**：智能体虽然"一次性"，但**其产出的任何对外动作仍必须过策略内核**。一次性不等于豁免治理。理由与 ADR-015/ADR-020 一致——治理闸门内嵌执行循环是本项目的立身之本，任何"临时"通道都是绕过路径。

#### G.2.3 智能体的"配置工具及权限"如何落到既有模型

智能体的权限配置复用既有治理构件，不新造：

| 智能体配置项 | 落到既有构件 | 约束 |
| --- | --- | --- |
| 允许的工具 | MCP 工具的 `caller_auth`/`tenant_scope` 声明（v5.2 3.6） | 取交集：智能体 allowlist ∩ 工具自身声明 |
| 可读资源范围 | RBAC 4 角色（PRD 十一章） | **不得超过发起者权限**（智能体不得放大权限） |
| 可写数据范围 | 治理闸门 `REQUIRE_APPROVAL` 规则 | 写操作默认仍需审批；`external_send` 必过闸门 |
| 预算 | 单任务预算（50 万 Token / ¥20，裁决 #27） | 智能体单次调用不得超发起者剩余预算 |

> **权限放大禁令**：智能体不得拥有超过其发起者的权限。这条与 §E.2 的「员工级策略覆盖不得绕过抑制红线」同构——**红线不可通过任何配置路径绕过**。

### G.3 多智能体协作设计原则

**明确保留"single-agent + 强控制面"为默认形态**（v5.2 3.1）。多智能体协作是**受控编排**，不是自由群聊：

| 原则 | 说明 |
| --- | --- |
| **拓扑是配置，不是代码** | 协作关系声明式定义（谁在谁之后、谁可并行、冲突时谁优先），存 `collaboration_topology` 配置 |
| **责任唯一** | 每个对外动作**有且仅有一个责任数字员工**。协作的其他成员是顾问或执行助手，不是共同责任人 |
| **闸门不共享** | 每个数字员工各过各的闸门。协作不产生"合并后的统一判定" |
| **优先级沿用 §E.5** | 跨员工冲突用 `worker.priority` 裁决，不新增机制 |
| **审计链完整** | 协作产生的每个成员决策独立写审计，通过 `collaboration_id` 关联 |

### G.4 演进路线（写入版本计划，避免遗忘）

| 版本 | 数字员工 | 智能体 | 多智能体协作 | 对应 PRD 章节 |
| --- | --- | --- | --- | --- |
| **v0.1（M1）** | **1 个可运行实例**（沉睡唤醒专员）；数据模型（`worker`/`worker_template`）已建；**无配置界面** | ❌ | ❌ | PRD 7.3、FR-01 |
| **v0.2（M2）** | **多个内置员工可运行**（含售后关怀专员）+ **用户可创新新数字员工**（自建模板）；五要素全可配（走审批）；**员工配置界面上线**；§E.5 冲突规则生效；L2 自主度 + L1 白名单豁免 | ❌ | ❌ | PRD 十五章 v0.2 行、FR-03/FR-37 |
| **v0.3（M3）** | 员工框架稳定 | **✅ 智能体上线**：创建（配工具 + 权限）+ 一次性调用 + 完整闸门 | ❌ | 本补充章节新增 |
| **v0.4（M4）** | 同上 | 同上 | **✅ 多智能体协作**：声明式拓扑 + 责任唯一 + 独立闸门 | 本补充章节新增 |
| **v0.5+** | 场景包（直播推广员工等） | 智能体复用数字员工技能包 | 协作拓扑可视化编排 | 见附录 H 开源选型 |

> **本表已同步登记至 PRD v5.3 十五章版本路线**（见附录 J §J.3），两处保持一致，ADR-041 记录该决策。

### G.5 待裁决事项（本补充章节提出，需裁决人拍板）

| # | 议题 | 选项 | 建议 |
| --- | --- | --- | --- |
| G-Q1 | v0.2 是否开放**用户自建数字员工模板**（`tenant_id` 非空的 `worker_template`）？ | (a) 仅内置模板可实例化 (b) 内置 + 租户自建模板（租户内可见） (c) 内置 + 租户自建 + 跨租户市场 | **(b)**——租户自建是"创新新数字员工"的最小实现；(c) 需引入模板审核与市场机制，v0.2 过重 |
| G-Q2 | 智能体的 `memory_policy` 默认值？ | (a) `none` (b) `task_scoped` (c) `write_through` | **(a) none**——与"用完即销毁"语义一致；`write_through` 须显式授权 |
| G-Q3 | 智能体是否允许 `external_send` 类工具？ | (a) 禁止 (b) 允许但必过闸门 + 审批 | **(b)**——与数字员工同权；但需确认是否额外要求独立审批档位 |
| G-Q4 | 多智能体协作的拓扑粒度？ | (a) 线性流水线 (b) DAG + 条件分支 (c) 自由协商 | **(b)** DAG——确定性、可回放；不支持自由协商（违背 ADR-020） |
| G-Q5 | `worker.priority` 是否允许跨租户配置？ | (a) 仅租户内 (b) 全局 | **(a) 租户内**——优先级是业务运营决策，非平台策略 |
| G-Q6 | 智能体是否需要独立的自主度等级？ | (a) 不需要，继承发起者 (b) 需要独立 L0/L1 | **(a)**——避免新增一条晋级路径（晋级是权限爬升关口，ADR-031 口径须统一） |

---

## 附录 H：开源二开选型与落点（补 v5.2 十五章）

### H.1 研发口径（承自 v5.2/README，本节细化）

三档使用方式，**每档都有明确的许可证义务**：

| 档位 | 含义 | 许可证义务 |
| --- | --- | --- |
| **① 库依赖** | 作为依赖引入，生产代码中调用 | 核对许可证 + 记录结论到 v5.2 十五章清单 |
| **② 契约级借鉴** | 只借鉴设计思想与接口契约，**不复制源码** | 记录借鉴来源与契约要点 |
| **③ 只读参考** | 读文档/源码理解思路，自研实现 | 记录来源即可 |

> **纪律（承自 README）**：所有引入的第三方组件**动工前必须完成 LICENSE 核对并记录结论**；避免引入带多租户商业条款的修改版许可组件。**外部项目的选型调研与对照明细属内部资料，不入库**——本附录只保留本项目实际引入组件的许可与约束结论。

### H.2 选型评估

> **核实方式**：star 数与许可证取自 GitHub REST API（`stargazers_count` / `license.spdx_id` / `pushed_at`）；`NOASSERTION`（GitHub 无法识别）一律回读 raw LICENSE 原文人工判定。**未能确证者显式标注"未确证"，不猜测。**
> **调研时点**：2026-10-23。

#### H.2.1 最终引入清单（3 项，附成本实测）

> **裁决依据**：依赖引入成本评估（实查 PyPI 依赖树 + GitHub 元数据）。
> **准入原则**：**只引"自研成本 > 引入成本 + 维护成本"、且"耦合面收敛在 adapter 层"、且"只用 <20% 功能默认拒绝"的依赖。**
> **技术堆砌的防线**：本项目的架构立足点是「可解释 / 可否决 / 可回放 / 可替换 / 可自证」五条。**违反任一条的依赖，无论多先进都不引。**

| 用途 | 项目 | 版本 | 许可 | 运行时直接依赖 | 依赖组 | 关键约束 |
| --- | --- | --- | --- | --- | --- | --- |
| **CI 红队/注入评测** | `promptfoo/promptfoo` | npm 最新 | **MIT** | 0（Node CLI，独立进程） | npm devDep | **不 import 进 Python 依赖树，零冲突面**。⚠️ **评测用的 LLM 必须显式指定为境内/本地模型**——否则它本身就是一个出境通道 |
| **Agent 轨迹评测** | `UKGovernmentBEIS/inspect_ai` | 0.3.283 | **MIT** | 33 | `evaluation`（**严格 dev-only**） | ⚠️ 已知风险：`fastapi>=0.119.0` 抬高下界、`click` 有排他区间。**不拉 langchain**（langchain 相关全在 dev extra）。需写 ~30-50 行 adapter 把 `ModelProvider` 暴露成它的 Model API |
| **自托管评测追踪** | `Comet-ML/opik` | 2.2.119 | **Apache-2.0** | 23 | `evaluation`（dev/ops） | **建议推迟到第 2 周**（起栈 30-60 min，对一周时间盒是重负）。走 **OpenTelemetry exporter 路径**而非 SDK（可替换性最大化：换掉只需改一行 exporter 配置）。⚠️ **接受 `litellm<1.106` 约束**（opik 强制的），并加 ruff 规则禁止直接 import litellm |

**为什么只有 3 项**：其余候选依赖经成本评估后否决（**评估明细属内部资料，不入库**）。**本项目的核心资产（策略内核、审计链、记忆门禁、冲突消解）没有开源件能替代**——引入它们只会增加耦合面而不省核心工作量。

#### H.2.1b litellm 的否决理由与修订（N4 拍板为 B：引入）

| 项目 | 曾定位 | 否决理由（实测）与修订 |
| --- | --- | --- |
| **`BerriAI/litellm`** | LLM 成本/预算 + 出境白名单强制点 | **① 违反「可替换」**：核心 API 就是 `model="openai/gpt-4o"` 这种字符串里编码供应商的形式，供应商名会出现在业务代码字面量里，**「零供应商依赖」纪律当场作废**。**② 108 个运行时依赖**（含 `boto3` ~90MB、`tokenizers`、`huggingface-hub`），镜像从 ~200MB 涨到 ~1.2GB。**③ 出境白名单强制点自写 20 行 vs 读它 300 行源码**——且自写版本可读、可测试、可审计，这对"客户数据出境合规"是硬要求。**④ 违反「可解释」**：错误归一化让 LLM 出口变成最大单点故障，出问题时要在 litellm 的 issue tracker 里搜错误码。**⑤ 成本表冗余**：本项目 schema 本就需要 `llm_calls` 表（为了可回放），litellm 的 spend tracking 是第二份账本。<br><br>**⚠️ 2026-10-24 修订（N4 拍板为 B：引入）**：上述第 ① 条已被裁决人推翻——litellm **可作为独立的 provider 适配层**部署在 `providers/` 目录内，业务层仍只认 `ModelProvider` 协议（附录 I），供应商名不会出现在业务代码里。<br>**保留的约束**：② 依赖体积（镜像 ~1.2GB）仍需评估；③ 出境白名单仍**自写**（不依赖 litellm 的白名单实现）；④ 错误归一化不得成为唯一故障诊断路径；⑤ 成本表仍自建，**不用 litellm 的 spend tracking**。<br>**落地约束**：业务层目录禁 import litellm（ruff 按目录限定 + CI 扫描门禁，任务 T0.3） |

#### H.2.2 契约设计的来源

四版本号布局、审批信号契约、输入快照布局、DENY 语义模型、决策记录字段、配置分离等接口契约**均为本项目自主设计**（见附录 D/E/F/I 与 `policy_kernel.py` 类型定义）。曾做过的外部项目契约对照属内部资料，不入库。

#### H.2.3 明确排除的原则

> **排除结论比推荐结论更能保护"开源域可运行"的定位。** 凡属以下情形一律排除：

1. **非 OSI 许可**：AGPL / BSL / SSPL / ELv2 / 自定义社区许可 / 「附加条款 Apache-2.0」（禁多租户、禁去 LOGO、可单方修改）；
2. **与本项目定位重合的外部项目**：定位重合恰恰意味着最不能复制——AGPL 传染性与"可二次开发"根本冲突，**代码一行都不能复制**；
3. **维护停滞**（>180 天无提交）或活跃度不达标；
4. **生态语言不符**（C#/Java 生态与 Python 栈冲突）；
5. **与既定架构冲突**：多 agent 群聊导向、第二编排引擎、通用策略引擎运行时等。

具体评估明细不入库；许可准入 checklist 见 §H.2.9。

#### H.2.4 各类别选型结论

| 类别 | 处置 |
| --- | --- |
| **数字员工 / Agent 角色框架** | **全部不引入为运行时**——见 H.2.5 结论一；配置模型自研（附录 E） |
| **编排 / HITL** | **编排层单选 LangGraph**——见 H.2.5 结论二 |
| **数字人 / 直播** | v0.5 场景包按需评估中文开源生态（调研明细不入库） |
| **治理 / 审计 / 评测** | 评测自研 + promptfoo 注入评测 |
| **MCP 生态** | fastmcp 已在栈内 |
| **策略 / 授权** | **策略内核自研纯函数；RBAC 用纯 Python 字典（`dict[Role, frozenset[Permission]]`）实现** |

#### H.2.5 三条关键结论

**结论一：Agent 框架全部不引入为运行时。**

本项目已定 LangGraph + 单 Agent + 强控制面，而所有 Agent 框架的卖点恰好是"让模型自己编排"——这与「DENY 不可绕过」在架构上直接对立。**任何 Agent 框架都提供绕过中间件的直连调用路径，等于在治理闸门旁边开后门。**

正确用法：角色配置模型由本项目自己定义（附录 E 的 YAML/DB 表），运行时仍走 LangGraph。

**结论二：编排层单选 LangGraph，不引入第二个编排引擎。**

编排层与任何第二引擎（长周期工作流调度、流式管道引擎等）同时存在会产生**两个真相来源**，checkpoint 与 workflow history 互相污染。真正的缺口不是"挂起/恢复"（LangGraph 已覆盖），而是"**恢复后按新策略重新判定而非沿用旧判定**"——这需要补的是策略版本的重装载与重验，不是换编排器。

与 PG 事务边界争夺控制权的执行引擎，**MVP 期不要动**。

**结论三：多智能体协作不需要多 Agent 框架，需要的是配置化。**

`policy_kernel.py` 的纯函数契约天然支持多个受控 agent 并存——每个 agent 的角色/技能/策略是配置项，每次工具调用前**统一过同一闸门**。LangGraph 的 subgraph 机制足以表达协作，且所有子图节点可统一过同一套闸门。

而外部多智能体框架的协作路由由模型自主决定，**无法保证 DENY 不可绕过——这是架构层面的红线，不是工程优化能解决的**。

> 这也是本项目"开源域第一个参考实现"叙事的技术立足点：**别人做多 agent 群聊，我们做多 agent 各自的治理闸门。**

#### H.2.6 演进阶段的底座可用性

| 演进阶段 | 底座可用性 | 判断 |
| --- | --- | --- |
| **MVP 单数字员工** | ✅ 完全自研即可 | LangGraph + `policy_kernel.py` 已足够。**此阶段引入任何 Agent 框架都是负收益** |
| **多内置员工 + 用户创新新员工** | ⚠️ 无开箱即用底座 | 自建配置表 + 热加载（三层配置分离与角色字段 schema 由本项目自定义，附录 E）。**技能包热装配建议做 MCP Server 动态注册**（fastmcp 已在栈内），让"技能包"= 一组 MCP tool，而非 Python 代码动态导入 |
| **配置型智能体**（一次性短任务） | ✅ 演进阶段可评估引入 | 单 Agent/短任务形态的简框架原生提供 `guardrail` 钩子——正好是"一次性任务也要过闸门"的实现位置。**届时按 §H.2.9 checklist 核对许可后评估引入（评估明细不入库）** |
| **多智能体协作** | ⚠️ 与既定架构冲突 | 见结论三。**不引入多 agent 框架** |

#### H.2.8 许可证未确证项（动工前必须核对）

> 调研中发现的候选组件存在 GitHub `NOASSERTION`（许可无法机器识别）、或二开深度/API 稳定性未经验证的情形。**一律按"未确证不得动工"处理**——先回读 raw LICENSE 全文人工判定并记录结论，再决定是否引入；涉及具体外部平台接入的，先做 SPIKE 验证认证与调用链路可行性。明细不入库。

#### H.2.9 权重/模型许可风险（易被忽略）

**代码许可与权重许可是两回事。** 直播带货涉及商用，权重条款风险高于代码风险（TTS/ASR/数字人等模型权重的许可须逐项单独核对，明细不入库）。

> **依赖准入 checklist（新增纪律）**：① 代码 LICENSE 必须是 MIT/Apache-2.0/BSD 等 OSI 认可许可；② **禁用"附加条款 Apache-2.0"**（禁多租户/禁去 LOGO/可单方修改）；③ 禁用 AGPL/BSL/SSPL/ELv2/自定义社区许可；④ 引入任何模型权重前**必须单独核对权重许可**，不得以代码许可代替；⑤ 引入前在 v5.2 十五章清单登记核对结论。

### H.3 选型结论的登记位置

调研已完成，结论按以下格式登记到 v5.2 十五章清单（**v5.2 原件零修改，本附录即登记处**）：

```
| 组件 | 选型 | 许可 | 档位 | 核对结论 | 何时该换 |
```

### H.4 与既有 ADR 的关系

| 既有 ADR | 本附录的关系 |
| --- | --- |
| **ADR-016**（RAG 自研组合，不引入低代码平台） | **被本次调研强化**——低代码平台类组件的附加条款（禁多租户 + 禁去 LOGO + 可单方修改）经核实确证。**原决策理由从"复杂 PDF 场景"扩展为"许可证三重限制"** |
| **ADR-022**（通用护栏不自研，预留适配位） | 一致——通用护栏与零信任接入件均排除在"自研范围"外 |
| **ADR-017**（pgvector，不引入独立向量库） | 一致——未发现需要引入独立向量库的选型 |
| **ADR-019**（MCP Server + Function Calling 双轨） | 强化——外部 REST→MCP 暴露件仅作备选，fastmcp 已在栈内 |
| **ADR-014**（LangGraph 编排） | **强化并加约束**——第二编排引擎会产生两个真相来源，明确"编排层单选 LangGraph" |
| **ADR-020**（闸门内嵌执行循环） | **强化**——所有 Agent 框架均提供绕过中间件的直连路径，与本 ADR 直接对立，构成"Agent 框架全部不引入为运行时"的根本原因 |

---

## 附录 I：Provider 抽象层契约（源自 `provider-abstraction` 任务 design.md）

> 本附录是 v5.2 3.2/3.3 缺失的 Provider 抽象层完整契约，源自任务 `.trellis/tasks/10-22-provider-abstraction/design.md`（D1–D6 已裁决）。**v5.2 原件零修改**，本附录以补充形式生效。

### I.0 设计与信任边界

```
┌──────────────────────────────────────────────────────────────┐
│  业务层（LangGraph 节点 / 技能 / 工作流）                      │
│  ★ 红线：不得 import 任何供应商 SDK（见 §I.5 强制机制）          │
│  只认两个 Protocol + 一个 ProviderRegistry                    │
└───────────────┬──────────────────────────────────────────────┘
                │ 契约（Protocol）
┌───────────────▼──────────────────────────────────────────────┐
│  适配器层                                                      │
│  ┌──────────────────┐        ┌────────────────────────────┐  │
│  │ JudgmentProvider │        │ ModelProvider              │  │
│  │ ├ JevAdapter(MVP)│        │ ├ DomesticAdapter(国内)    │  │
│  │ ├ RuleRouter     │ 兜底   │ ├ OverseasAdapter(境外)    │  │
│  │ └ (未来其他模型)  │        │ └ PrivateAdapter(vLLM等)   │  │
│  └────────┬─────────┘        └──────────┬─────────────────┘  │
└───────────┼──────────────────────────────┼─────────────────┘
            │                              │
┌───────────▼──────────────────────────────▼─────────────────┐
│  合规闸门（EgressGate）—— 调供应商之前必须过                    │
└─────────────────────────────────────────────────────────────┘
```

**关键设计点**：EgressGate 放在**适配器层与供应商之间**，不在适配器内部。这样无论业务层走哪条路，"能不能出境"只有一个判定点，不可被适配器绕过。

**两条 Provider 的根本不对称（必须写死，否则开发会"顺手补上默认值"）**：

| | `JudgmentProvider` | `ModelProvider` |
| --- | --- | --- |
| MVP 默认 | **默认真连 Jev**（D2） | **无默认，报错拒绝**（D5） |
| 兜底 | ✅ 规则路由（纯代码，无模型可跑） | ❌ 生成能力没有纯代码实现 |
| 缺省行为 | 降级到规则路由，不阻塞 | 抛 **E15** 拒绝执行 |
| 为什么 | 判断有"纯规则"这条永远可跑的下界 | 生成没有下界；"猜一个"= 隐式选择供应商 |

### I.1 `JudgmentProvider` 契约

```python
class JudgmentProvider(Protocol):
    """判断模型统一契约。核心三值 + 可选扩展（D1 分层契约）。

    不变式（实现者必须保证）：
      INV-1: 返回 JudgmentResult，绝不返回裸 dict / 自由文本
      INV-2: choice / score / confidence 三个核心字段永不为 None
      INV-3: 永不抛出影响调用方的业务异常——不可用一律转为
             JudgmentResult(choice=ABSTAIN, confidence=0.0, degraded=True)
             （非静默：degraded=True + 审计留痕）
      INV-4: 任何情况下不得自行返回 ALLOW（ADR-015 信任边界，D6）
    """
    provider_id: str          # 稳定标识，进审计，如 "jev" / "rule_router"
    def judge(self, req: JudgmentRequest) -> JudgmentResult: ...
```

```python
# ── 核心层：所有实现必须支持，跨模型一致，可单测 ────────────────
class JudgmentRequest(TypedDict):
    task_id: str
    questions: list[Question]     # 支持并行提问，一次请求多问题
    locale: str                   # "zh-CN" — 影响阈值选择

class Question(TypedDict):
    qid: str                      # "intent" / "route" / "risk" / "need_llm"
    kind: Literal["Choice", "Score", "Noul"]
    choices: list[str]            # Choice 用；≤255
    score_range: tuple[int, int] | None

class JudgmentResult(TypedDict):
    # —— 核心必填（D1）——
    choice: str | None            # None = 不可用
    score: float | None           # 落在 score_range 内
    confidence: float             # 0.0–1.0，语义统一，见 §I.3
    # —— 状态位 ——
    degraded: bool                # True = 走了降级路径
    degrade_reason: str | None    # "JEV_TIMEOUT" / "LOW_CONFIDENCE" / "EGRESS_OFF"
    provider_id: str              # 实际产出该结果的 provider
    # —— 扩展层（可选，D1）——
    extensions: dict[str, Any]    # 白名单外字段在此
```

**扩展字段白名单**（D1"后续动作"）：`per_candidate_scores` / `rationale` / `margin`。

**校验规则**：① 白名单外字段直接拒绝（防适配器泄漏供应商特定结构）；② 适配器必须显式声明支持哪些扩展（`capabilities()`），业务层消费前先查 `supports_extension()`，不得盲读。**两层都要**：白名单防结构泄漏，显式声明防能力幻觉。

**不可变性**：`JudgmentRequest` 进入 Provider 后不可被修改（判断请求内容要进审计，可变输入无法举证）。适配器如需改写（如脱敏），必须返回新的 request 并标注 `input_transformed`。

### I.2 `ModelProvider` 契约

```python
class ModelProvider(Protocol):
    """生成模型统一契约。业务层只认这个，不认任何 SDK。

    不变式：
      INV-1: generate/stream 返回统一 ChatResponse，失败抛 ProviderError 子类
      INV-2: 每次调用（含失败与重试）都必须回填 model_version
      INV-3: 调用前必过 EgressGate，适配器无权绕过
      INV-4: 不做 provider 自动切换（D4：无智能路由）
    """
    provider_id: str
    endpoint_id: str          # ★ 端点标识，进审计
    def generate(self, req: GenerationRequest) -> ChatResponse: ...
    def stream(self, req: GenerationRequest) -> Iterator[str]: ...
    def capabilities(self) -> ModelCapabilities: ...
    def healthcheck(self) -> HealthStatus: ...   # 与 SystemAdapter 一致
```

```python
class GenerationRequest(TypedDict):
    task_id: str
    messages: list[Message]
    temperature: float
    max_tokens: int
    # ★ D5: 不设 provider_id 默认值。缺省时由 Registry 层抛 E15

class ChatResponse(TypedDict):
    content: str
    model_version: str       # ★ 四版本号之一
    provider_id: str
    endpoint_id: str         # ★ 可核查性
    egressed: bool           # ★ 本次调用是否出境，进审计
    usage: Usage             # input_tokens/output_tokens/provider
    degraded: bool

# model_version 格式：
# "<provider_id>/<model_name>/<model_revision>@<adapter_version>"
# 例: "deepseek/deepseek-chat/20250101@adapter-1.2.0"

class ModelCapabilities(TypedDict):
    provider_id: str
    model_name: str
    context_window: int
    supports_tools: bool
    supports_stream: bool
    supports_prefix_cache: bool
    # ★ 不含 cost_per_token —— 成本在 cost 模块按 endpoint 维护
```

### I.3 置信度与降级纪律

**判断模型输出的单向使用（D6 红线）**——两层保证：

1. **类型系统层面**：`JudgmentResult` 里根本没有 ALLOW 字段。它只能告诉策略内核"要不要降级"，说不出"放不放行"。
2. **策略内核层面**：`policy_kernel.evaluate()` 的 `judgment` 参数只流向"降级判定"分支，不参与 DENY/ALLOW 的规则计算。

**阈值语义统一**：`confidence` 必须归一到**该判断正确的概率估计**，而非模型自报值（不同供应商标定不同，适配器负责归一并标注原始值）。

| 档位 | 阈值（`governance.yaml` 可配，变更走审批 + 回归门禁） | 处理 |
| --- | --- | --- |
| 高 | ≥ 0.85 | 采纳 |
| 中 | 0.60 – 0.85 | 采纳 + `low_confidence` 标记 + 提高人工抽检比例 |
| 低 | < 0.60 | **降级**：LLM 重新判断 或 转人工 |

**中文场景阈值更低**：`locale=zh-CN` 时用 `zh_low_confidence_threshold`，默认 **0.70**（中文门槛更高 → 更多降级）。

**判断模型降级链**：

```
JevAdapter.judge()
  ↓ 不可用（超时/报错/已关闭/出境拒绝）
RuleRouter.judge()              ← 纯代码兜底，同样只降级不放行
  ↓ RuleRouter 也无匹配/低置信
JudgmentResult(degraded=True, confidence=0.0)
  ↓ 业务层据 degraded 决定 → 转 LLM 兜底 或 转人工
```

**粒度**：provider 级降级在 MVP **不实现**（不"换一个 Jev 试试"）。理由：违反 D4"不做智能路由"精神，且多 provider 配置复杂度与 MVP 目标有张力。未来加 provider 级降级时作为独立任务裁决。

**RuleRouter 同样只能降级**：在降级路径上，它的高置信只代表"规则很确定要降级"，绝不反向变成"可以放行"。实现上 RuleRouter 输出 `degraded=True` 恒为 True。

### I.4 C9 出境管控联动

**三层管控的分工（D3"白名单先行"）**：

> **开关控制"是否允许出境"，白名单控制"允许出境到哪"。两者不可混淆。**

| 层 | 机制 | 默认 | 位置 |
| --- | --- | --- | --- |
| L1 数据范围声明 | `compliance/egress.yaml` 逐字段声明出境什么 | 必填，未声明字段不得出境 | 合规清单，随版本管理 |
| L2 白名单 | 只有登记在 `egress.yaml` 的 endpoint 才可调用 | 默认全部拒绝 | EgressGate |
| L3 开关 | `third_party_inference.enabled` 一键关 | **默认 true 但受 L1/L2 约束**；私有化默认 false | 全局配置 |

**语义澄清（必须写死）**：
- 开关 `enabled=true` **不等于**"允许所有出境"，而是"允许 `egress.yaml` 里已声明的部分出境"。
- 开关 `enabled=false` 时，**所有**第三方推理走降级路径。

**EgressGate 判定流程**：

```
ModelProvider.generate() / JudgmentProvider.judge()
  ↓ 先过 EgressGate.check(provider_id, endpoint_id, payload)
  ├─ 1: 该 endpoint 是否在 egress.yaml 白名单？   否 → 拒绝(EGRESS_NOT_WHITELISTED)
  ├─ 2: third_party_inference.enabled？           否 → 拒绝(EGRESS_DISABLED)
  ├─ 3: payload 字段是否都在该 endpoint 声明范围内？否 → 拒绝(FIELD_NOT_DECLARED)
  ├─ 4: payload 是否含 PII？                       是 → 强制不出境(PII_FORBIDDEN)【红线】
  └─ 通过 → 放行，记录 egressed=True 进审计
```

**检查 4 是硬红线**：无论用户显式指定哪个 provider，含 PII 的请求强制不出境，优先于用户选择。这条**不可配置、不可审批绕过**。

**开关变更审计**：`third_party_inference.enabled` 变更、前端 provider 切换、`egress.yaml` 变更——三者全部写审计（谁改的/角色、改前改后值、时间、理由）。开/关出境仅管理员；provider 切换归运营负责人/管理员（待权限矩阵脚注冻结）。

### I.5 业务层零供应商依赖的强制机制（R3）

R3 是"用户明确的项目约束"，必须有**机制**而非仅"约定"。三层保障：

**结构层：单一入口**

```python
# 业务层唯一允许的 import 形式
from ....providers import JudgmentProvider, ModelProvider, ProviderRegistry
# 禁止：
# from ....providers.jev_adapter import JevAdapter     ❌
# import openai / anthropic / jev_sdk                   ❌
```

**测试层：依赖扫描（CI 阻断）**：扫描业务层目录（`nodes/`、`skills/`、`workflows/`）的 import，发现供应商 SDK 或具体适配器模块即**阻断**。让"零依赖"从口号变成门禁。

**契约层：适配器可替换性测试**：设计一组 `JudgmentProvider` / `ModelProvider` 的**契约测试套件**，任何新适配器接入必须全绿。

> **补充纪律（v5.3，见 K-M4）：Prompt 不是合规机制。**
>
> 真实工程实践对照（附录 K / K-M4）表明：把"覆盖策略""跟进频率"等规则**写在 prompt 字符串里**是不可接受的——**Prompt 是 LLM 的建议，不是强制**。Agent 可以不遵守，且无人能证明它遵守了。
>
> 因此本项目的一条硬纪律是：**任何合规规则、频率规则、抑制规则、权限规则，必须落在 Python 代码里（`policy_kernel.py` / `governance.yaml`），不得以 prompt 文本作为唯一实现手段。** prompt 可以"建议 Agent 怎么做"，但不能是"Agent 不做某事"的唯一保障——保障必须来自代码层的闸门。
>
> 这与 §I.5 的"零 LLM 判定"、ADR-020「闸门内嵌执行循环」是同一条纪律的三种表述。

### I.6 Provider 选择与配置

**无智能路由（D4）**：智能路由本身需要判断模型参与判断，反而把"哪个模型更可信"的问题重新引入。显式指定把选择权交给用户，行为可预测。

**优先级链**：

```
运行时显式选择（前端切换，持久化到 TaskConfig）  ← 最高
    ↓ 若无
TaskConfig 里的 provider 配置
    ↓ 若无
配置文件 default_provider  ← D5：若此处也未配置 → 抛 E15 拒绝执行
```

**最底层不设"猜一个"的兜底**。

**成本按 provider 分别建模**：成本表按 `endpoint_id` 分别维护单价，预算阈值按实际所用 provider 的单价换算（与 v5.2 3.8"成本可预估"、裁决 #27 一致）。超限行为不变（E11：停止 + 告警 + 审计）。

### I.7 新增异常场景

| 编号 | 场景 | 行为 | 依据 |
| --- | --- | --- | --- |
| **E15**（新增） | 生成模型 provider 未指定 | **拒绝执行 + 可操作报错 + 审计** | D5 |
| E1 | 判断模型/企业系统不可用 | 降级转人工/规则路由，记录原因，禁止静默失败 | PRD E1 |
| E11 | 预算超上限 | 停止当前任务 + 告警 + 审计 | PRD E11 |
| — | 判断模型低置信 | 降级（LLM 兜底或转人工），增加抽检 | §I.3 |
| — | 出境被拒 | 降级到合规路径；PII 请求直接不出境 | C9 / D4 |

**E15 报错信息要求**：须指明缺哪个配置项、去哪里配、有哪些可选值。例：`"未指定生成模型 provider。请在 config/providers.yaml 的 active_provider 指定，或在前端切换。可选：[deepseek, qwen, doubao, claude, gpt]"`。

**连带影响**：README 7 分钟演示动线需预留"首次配置 provider"环节，否则演示第一步即卡住。

---

## 附录 J：ADR 补充、缺口对照与修订记录

### J.1 新增 ADR（接续 v5.2 ADR-040）

| # | 决策 | 结论 | 理由 |
| --- | --- | --- | --- |
| **ADR-041**（v5.3） | 判断模型抽象 | **`JudgmentProvider` 分层契约**（核心三值 + 扩展白名单）+ **MVP 默认 Jev** + 规则路由兜底；**判断模型输出只能降级，永不产生 ALLOW** | 引入可配置的不同判断模型后，"最坏情况是多审批，而非不该放行的动作被放行"——换准确率更差的模型只降效率，不降安全性（D1/D2/D6） |
| **ADR-042**（v5.3） | 生成模型路由 | **无智能路由 + 用户显式指定 + 未指定即报错拒绝执行**（不设缺省、不做隐式回落） | 智能路由需判断模型参与判断，把"哪个模型更可信"的问题重新引入；显式指定行为可预测，且避免用户不知情把数据发到境外（D4/D5） |
| **ADR-043**（v5.3） | C9 出境三层管控 | **数据范围声明 / 白名单 / 开关三层分工**：开关控制"是否允许出境"，白名单控制"允许出境到哪"；**含 PII 的请求强制不出境（不可配置、不可审批绕过）** | 两层语义混淆会导致"开关打开=无限制出境"的误解；PII 红线必须高于用户选择（D3/R5） |
| **ADR-044**（v5.3） | 数字员工实体模型 | **新增 `worker` + `worker_template` 表；员工配置五要素（角色定义/技能包/策略包/记忆空间/自主度）全部有数据载体；策略包绑定为"按员工覆盖差量"，**覆盖抑制红线 / NO_CONSENT 的键在写入时直接拒绝**；**表在 MVP 建，界面在 v0.2** | 补 v5.2 数据模型缺口（G-A）；DENY 红线不可通过员工级配置绕过，与 ADR-034 一致 |
| **ADR-045**（v5.3） | 数字员工 / 智能体 / 多智能体分层 | **数字员工 = 长期角色实例（可被反复派发任务、有记忆、参与自主度晋级）；智能体 = 一次性短任务执行体（默认无记忆、不参与晋级、用完即销毁）；多智能体协作 = 受控编排（拓扑配置化、责任唯一、各过各的闸门）**；**智能体不是治理后门，其对外动作同样必过策略内核** | v5.2 3.1 明确排除多智能体自由群聊，PRD 未定义"智能体"，术语歧义会导致设计返工；治理闸门内嵌执行循环是立身之本，任何"临时"通道都是绕过路径 |
| **ADR-046**（v5.3） | API 接口层与契约门禁 | **冻结 MVP 端点清单 + 统一响应信封（错误码/四版本号/trace_id）+ 5 项契约测试门禁（OpenAPI 快照/契约测试/错误码完备性/审计完备性/闸门完备性）**；W9 拆分为 W9a（后端 + 契约测试）+ W9b（前端） | v5.2 无接口契约却估 3.5–5 人日前端，是估算风险最高项；契约门禁让"审计完备性""闸门完备性"从约定变成 CI 可验证项 |
| **ADR-047**（v5.3） | 多员工冲突消解 | **跨员工冲突优先于跨平台冲突**：同一客户同一时间窗口只放行 `worker.priority` 最小者，其余 DENY（`MULTI_WORKER_CONFLICT`）；平局升级人工（不默认放行）；优先级租户内可配 | 补 v5.2 L13；"默认最高优先级"特权会让优先级配置失去意义，优先级是业务运营决策 |
| **ADR-048**（v5.3） | 智能体权限放大禁令 | **智能体不得拥有超过发起者的权限**；工具权限取交集（allowlist ∩ 工具自身声明）；`memory_policy` 默认 `none`；智能体沿用发起者的预算与自主度约束 | 与 ADR-044「红线不可经配置绕过」同构——权限放大是同一类漏洞；独立晋级路径会与 ADR-031 的晋级硬标准口径分裂 |
| **ADR-049**（v5.3） | 工程实践反向约束与借鉴边界 | **① 从真实工程实践对照中提炼四条反向约束**（附录 K / K-M1–K-M4）：**对外动作必过闸门**（配额/权限检查不可 opt-in、必须位于发送路径上）；**判定必须落库**（审计 append-only + 哈希链，不得只打日志、不得依赖进程内事件总线）；**规则不得只写在 prompt**（合规/频率/抑制规则必须落在 Python 代码里）；**CI 必须跑真 PostgreSQL 集成测试**。另登记三个通用工程陷阱（日志不落库 / 认证旁路 / 迁移与启动耦合，附录 K §K.3）。**② 借鉴边界**：**不复制任何外部项目代码**（无论开源或私有）；外部项目的代码带着它自己的隐含假设（数据模型、边界划分、错误处理策略），治理内核的正确性必须能用本项目的验收标准证明——**只依据本项目需求与治理约束自主设计，外部对照明细不入库** | 「自动化执行 ≠ 治理」：执行能力强的 Agent 平台，若没有闸门、判定不落库、规则只在 prompt 里，治理能力为零——这既是 GovernCRM 的存在理由，也是差异化叙事的实证支撑。**私有/外部项目代码带着它自己的隐含假设**，只能参考设计判断，不能借鉴实现——这比版权理由更根本（附录 K） |

> **ADR-015 修订**（v5.3）：原表述"**Jev 前置，仅用于路由与降级，不用于放行**"扩为覆盖**所有** `JudgmentProvider` 实现的通用信任边界原则——「**判断模型输出只能作为降级信号，永不自行判定 ALLOW；DENY 权永远在规则内核**」。Jev 保留为 MVP 默认实现，但不再是架构前提。

### J.2 v5.2 缺口对照

| 缺口 | v5.2 现状 | v5.3 落点 | 状态 |
| --- | --- | --- | --- |
| G-A 数据模型无数字员工表 | 13 章 14 表无 `worker` | 附录 E §E.2/E.3/E.4 + ADR-044 | ✅ 补齐 |
| G-B 无 API 接口层 | 仅"FastAPI"选型词 | 附录 F + ADR-046 | ✅ 补齐 |
| G-C 数字员工配置界面不在 MVP | W9 无此项，W14 后置 v0.2 | 附录 E §E.6 + 附录 G §G.4 | ✅ 明确分期（表 MVP / 界面 v0.2） |
| G-D「智能体」未定义 | PRD 与 v5.2 均无 | 附录 G §G.1 + ADR-045 | ✅ 补齐 |
| G-E 员工间冲突规则未定义 | L13 已登记为局限 | 附录 E §E.5 + ADR-047 | ✅ 补齐 |
| G-F Provider 抽象层无契约 | 3.2/3.3 一句话 | 附录 I + ADR-041/042/043 | ✅ 补齐 |
| G-H 开源二开选型 | 十五章仅清单，无评估 | 附录 H（H.1–H.2.9 已完成，含 LICENSE 核实纪律与排除原则） | ✅ 补齐 |
| G-I 真实工程实践无参照 | 无同领域生产项目对照 | 附录 K（真实工程实践对照，四条反向约束 + 三个工程陷阱）+ ADR-049 | ✅ 补齐 |
| G-J CI 不验证 PG 特性 | 12.1 门禁清单未含 PG 集成测试 | 附录 F §F.4 第 ⑥ 项（K-M1） | ✅ 补齐 |
| G-K 必经节点可被 opt-in 跳过 | 12.1 闸门完备性未禁止 opt-in | 附录 F §F.4 第 ⑦ 项（K-M2） | ✅ 补齐 |
| G-L RBAC 继承方向未明 | 十一章矩阵未说明管理员是否蕴含下级权限 | 附录 E §E.2 第 4 条（K-M3） | ✅ 补齐 |
| G-M Prompt 被当作合规手段的风险 | 未登记 | 附录 I §I.5 补充纪律（K-M4） | ✅ 补齐 |

### J.3 与 PRD v5.3 的同步要求

| 本补充章节 | PRD v5.3 修订点 |
| --- | --- |
| 附录 E §E.2 | 新增 FR 条款：数字员工实体与配置（五要素）；**新增 FR 条款：策略包绑定不得覆盖抑制红线** |
| 附录 E §E.3 | 7.3 模板表补「模板 vs 实例」区分与 `runnable` 语义 |
| 附录 E §E.5 | 新增 FR 条款：跨员工冲突消解（优先级裁决） |
| 附录 F | 新增 FR 条款：API 契约与五项契约门禁 |
| 附录 G §G.1 | 新增术语定义章：数字员工 vs 智能体 vs 多智能体协作 |
| 附录 G §G.2 | 新增 FR 条款：智能体配置（工具 + 权限）与一次性生命周期；**新增异常场景 E16（智能体权限超限）** |
| 附录 G §G.4 | **十五章版本路线表新增 v0.3（智能体）/ v0.4（多智能体协作）两行** |
| 附录 I | 新增 E15；C9 拆分"开关/白名单"语义；7.2 自主度条款强化"判断模型永不产生 ALLOW" |
| 附录 H | 十五章开源清单补"核对结论"列 |

### J.4 本补充章节待裁决事项汇总

| # | 议题 | 建议 | 阻塞 |
| --- | --- | --- | --- |
| G-Q1 | v0.2 是否开放用户自建数字员工模板 | (b) 内置 + 租户自建 | v0.2 范围 |
| G-Q2 | 智能体 `memory_policy` 默认值 | (a) none | v0.3 范围 |
| G-Q3 | 智能体是否允许 `external_send` 工具 | (b) 允许但必过闸门 + 审批 | v0.3 范围 |
| G-Q4 | 多智能体协作拓扑粒度 | (b) DAG | v0.4 范围 |
| G-Q5 | `worker.priority` 是否跨租户配置 | (a) 租户内 | 权限模型 |
| G-Q6 | 智能体是否需独立自主度等级 | (a) 继承发起者 | ADR-048 |
| G-H1 | 附录 H 开源选型档位（依赖/借鉴/参考） | 已完成，见附录 H | 已解决 |

---

## 附录 K：真实工程实践对照（外部参照明细不入库）

> **性质**：本附录记录从真实工程实践对照中提炼的设计约束。**对照源项目的代码、文件行号、仓库地址等属外部资料，按内容纪律不入库**——此处只保留对 GovernCRM 有效的结论。ADR-049 登记于附录 J.1。

### K.1 为什么需要这个附录

「自动化执行 ≠ 治理」。执行能力强的 Agent 平台，如果没有闸门、判定不落库、规则只写在 prompt 里，治理能力就是零。本项目立项时即将这些反面经验固化为约束，逐条写进门禁——**这是 GovernCRM 存在的理由，也是"开源域第一个参考实现"的差异化立足点**。

### K.2 四条反向约束（K-M1–K-M4）

| # | 约束 | 本项目落点 |
| --- | --- | --- |
| **K-M1** | **CI 必须跑真 PostgreSQL 集成测试**——append-only 触发器、哈希链、分区表、CHECK 约束只在内存/SQLite 中跑等于没验证 | 附录 F §F.4 契约门禁第 ⑥ 项；PRD C13 |
| **K-M2** | **必经节点的检查不可 opt-in**——可跳过的门等于没有门；配额、权限、合规检查必须位于执行路径上 | 附录 F §F.4 第 ⑤ 项；§E.2 策略包红线保护；FR-39 ⑦ |
| **K-M3** | **管理员不自动蕴含下级权限**——system_admin 不能绕过 REQUIRE_APPROVAL，更不能绕过 DENY | 附录 E §E.2、PRD 十一章 RBAC、FR-01f |
| **K-M4** | **Prompt 不是合规机制**——任何合规/频率/抑制/权限规则必须落在 Python 代码里；prompt 只能"建议怎么做"，不能保障"不做某事" | ADR-020、§I.5、PRD C12 |

### K.3 三个通用工程陷阱（自查用）

1. **决策只打日志不落库**：审计必须 append-only 落库 + 哈希链；进程内事件总线在多副本部署下会静默丢事件（PRD C14）。
2. **认证旁路开关**：dev 态认证旁路必须带"生产环境检测到开启即拒绝启动"的硬断言（PRD C15）。
3. **迁移与应用启动耦合**：迁移独立执行、失败不半启动；调试类依赖不得进入主依赖树。

### K.4 设计确认（与本项目同构的正确做法）

以下做法经对照后在本项目设计中独立确认并落地：审批挂起点与工作流解耦；快路径预筛与审计分离；结构化日志三段式；测试按依赖分层；一次运行一个可追踪 ID；迁移与应用启动分离；大表索引组织前置设计。落点见附录 E/F/I 与 v5.2 四章、十二章相应章节。
---

> 本补充章节为技术设计参考，第三方组件的许可与合规请以 LICENSE 全文及法务意见为准。附录 D 已于 2026-10-24 冻结（见 `GovernCRM_附录D_冻结版.md`），PRD 十一章 ⚠️ 脚注 a1–a10 已整包采纳。

---

## 附录 L：Grilling 决策架构落盘（2026-10-24 拍板）

> 拍板人：eyangisgod。以下决策覆盖 `GovernCRM_待拍板决策总表` 中 G-02/G-04/G-05/G-06/G-07 五项架构侧落盘项。

### L.1 G-02 · 影响面定义

**决策**：(a) 影响面按**客户数**计算，同一客户多条动作只计 1 次。

**裁定范围说明**：G-02 只裁定「客户数 vs 动作数」这一维度口径，**不涉及审批人构成**。审批路由三档的审批人**保持裁决 #25 原文不变**。

**落点**：架构 v5.2 §10.1「审批路由（三档）」行（裁决 #25）：

| 档位 | 影响面（客户数） | 审批要求（裁决 #25 原文） |
|------|-----------------|----------------------|
| 小 | ≤10 客户 | **运营负责人单签** |
| 中 | 11–100 客户 | **运营负责人 + 管理员双签** |
| 大 | >100 客户 | **双签 + 合规知会**（知会 = 只读通知，不参与签署） |

> 计数规则：同一客户多条待执行动作合并计数为 1。与裁决 #25 一致。
> 超时参数同源：48h 自动失效（不默认通过，E9）、剩余 24h 升级提醒上一级。
> 落地约束：`governance.yaml` 可配，调整走审批 + 回归门禁（裁决 #25）。

### L.2 G-04 · 位点可回放的存储设计

**决策**：(a) MVP 加一张位点表 + 一条查询，不过度设计。

**与 LangGraph checkpointer 的边界**（避免两个真相来源，N3/ADR-014）：

| 职责 | 归属 | 说明 |
|------|------|------|
| 编排器内部状态恢复 | **LangGraph checkpointer**（框架自带） | 不重复存 `graph_state_snapshot` |
| 审计级位点回放 | **`task_checkpoint` 表**（本决策新增） | 只存"可被审计查询的位点索引 + 决策链引用" |

**落点**：在架构 v5.2 十三章数据模型中新增：

| 表 | 用途 | 关键字段 |
|---|------|---------|
| `task_checkpoint` | 审计级位点索引 | `checkpoint_id` (PK), `tenant_id` (FK), `task_id` (UUID, FK→`action_candidate.task_id`), `node_name`, `checkpoint_at`, `decision_id` (FK→`decision`, **可空**——非判定节点也会产生位点), `langgraph_checkpoint_id` (引用，不存快照) |

> **不存 `graph_state_snapshot`**：图状态由 LangGraph checkpointer 持有，本表只存"哪个 task 在哪个 node 产生了哪次决策"的索引。**恢复用 checkpointer，审计用本表**——两者职责不重叠。
> 查询接口：`GET /api/v1/tasks/{task_id}/checkpoints` → 返回位点列表，支持「可回放」审计需求（FR-14 P0）。**路径前缀必须带 `/api/v1`**（§F.1 原则 1）。不建模位点回放引擎——回放仅需从位点查审计流水还原，不需要额外回放基础设施。

### L.3 G-05 · E6 Fail-Fast 机制

**决策**：(a) 架构补 fail-fast，FastAPI lifespan 加载规则并校验。

**落点**：架构 v5.2 §4「治理闸门」所在章的**应用启动流程**新增（v5.2 §4.3 为「治理闸门」小节本身，启动流程挂在其后的服务装配段）：

```python
# FastAPI lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时加载并校验全部治理规则
    try:
        policy_kernel.load_and_validate("governance.yaml")
    except RuleValidationError as e:
        logger.critical(f"规则加载失败，拒绝启动: {e}")
        raise  # 进程退出，K8s 自动重启
    yield
```

```python
# FastAPI lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时加载并校验全部治理规则
    try:
        policy_kernel.load_and_validate("governance.yaml")
    except RuleValidationError as e:
        logger.critical(f"规则加载失败，拒绝启动: {e}")
        raise  # 进程退出，K8s 自动重启
    yield
```

**设计原则**：带病运行的治理系统比不启动的治理系统危险得多。启动失败可自动重启恢复；启动成功后带着错误规则运行产生的错误判定无法恢复。

### L.4 G-06 · 重试在 Provider 适配器层

**决策**：(a) 重试在 Provider 适配器内部，统一约束最多 1 次 + 必须写审计。

**落点**：架构 §I.2 ModelProvider 契约增加不变式 INV-5：

```
INV-5: 重试逻辑在适配器内部实现（非业务层），最多 1 次；
       重试必须写审计（attempt_number / retry_reason / retry_result）；
       重试失败 → 抛 ProviderError 交由调用方决定降级/转人工。
```

**设计原则**：重试是供应商特定的（超时语义/限流响应/重试建议头各不同），应由适配器就近处理。但统一约束"最多 1 次"对齐 E3，防止适配器无限重试。

### L.5 G-07 · E16 权限超限异常路径

**决策**：(a) 交集为空→抛 E16 拒绝+告警+审计。不静默降权。

**三方交集规格**（PRD v5.3 §F.2.1 要求的工具侧约束必须一并纳入，不能只做两方交集）：

```
有效权限 = 智能体 allowlist ∩ 发起者实际权限 ∩ 工具自身声明的 caller_auth/tenant_scope
```

**落点**：架构 §G.2.3（智能体权限校验）实现：

```python
def validate_agent_permissions(
    agent_allowlist: set[str],
    initiator_permissions: set[str],
    tool_declared_scope: set[str],
) -> set[str]:
    effective = agent_allowlist & initiator_permissions & tool_declared_scope
    if not effective:
        raise E16_AgentPermissionExceeded(
            requested=sorted(agent_allowlist),
            initiator_has=sorted(initiator_permissions),
            tool_scope=sorted(tool_declared_scope),
            missing_from_initiator=sorted(agent_allowlist - initiator_permissions),
            missing_from_tool=sorted((agent_allowlist & initiator_permissions) - tool_declared_scope),
        )
    return effective  # 交集为空绝不返回，取交集仅减不增
```

**三条补充规则**：

1. **空 allowlist 是配置错误，不是权限超限**：若智能体未声明任何工具权限，抛 `AgentConfigError`（配置缺失），**不抛 E16**（E16 语义是"申请了超限权限"）。
2. **拒绝时告警必带差集**：`missing_from_initiator` 与 `missing_from_tool` 分开记录，让运维能区分"发起者没给"与"工具本身不允许"。
3. **不得静默降权**：交集非空时静默截断会让用户以为拿到了完整能力。**必须显式告知生效范围**（返回的 effective 集合要回写智能体配置快照并写审计）。

---

## 附录 M：待裁决事项 G-H1/G-Q1–G-Q8 拍板结果

| # | 议题 | 拍板 | 理由摘要 |
|---|------|------|---------|
| G-H1 | 附录 H 开源选型档位 | 已完成 | 见附录 H，与 N1–N8 宪法一致 |
| G-Q1 | v0.2 用户自建数字员工模板 | **(b) 内置 + 租户自建** | 与 v0.2 定位一致 |
| G-Q2 | 智能体 memory_policy 默认值 | **(a) none** | 默认无记忆，write_through 须显式授权 |
| G-Q3 | 智能体 external_send 工具 | **(b) 允许但必过闸门+审批** | 闸门不可绕过 |
| G-Q4 | 多智能体协作拓扑粒度 | **(b) DAG + 条件分支** | 比线性灵活，比自由协商可控 |
| G-Q5 | worker.priority 跨租户 | **(a) 租户内** | 避免跨租户策略泄漏 |
| G-Q6 | 智能体独立自主度 | **(a) 继承发起者** | 不新增晋级路径 |
| G-Q7 | M1 工作量 24–30 人日 | **(a) 接受** | 前期不做返工更高。**2026-10-24 修订**：人日已取消门禁，见《开发实施计划》§9.1 |
| G-Q8 | MVP 配置界面后置 v0.2 | **(a) 确认后置** | 表 MVP 建，界面 v0.2 |

---

## 附录 N：前端实现契约（2026-10-24 拍板）

> 拍板人：eyangisgod。前提：MVP **必须有界面来演示**，前端承载完整业务闭环。
> 详细任务拆解见《MVP_任务拆解.md》T-FE1~T-FE13；本附录冻结**接口契约与屏清单**。

### N.1 决策清单

| # | 议题 | 拍板 | 落点 |
|---|------|------|------|
| FD-1 | 前端屏数 | **5 屏** | 与 §F.3 已冻结端点清单一一对应，**不缩减** |
| FD-2 | 实现形态 | **MOCK 数据 + 真实交互** | 页面走完整业务流程；数据源可换真 API，业务代码零改动 |
| FD-3 | 技术栈 | **React + TypeScript + Vite + Ant Design + ECharts + TanStack Query** | 补全 v5.2 §三 只定的 "React + TS" |
| FD-4 | 4 角色权限演示 | **真实登录 + session** | token 存 localStorage，请求带 `Authorization` 头 |
| FD-5 | MOCK 与真 API 共存 | **TS 接口 + 双实现 + env 开关** | `VITE_API_MODE=mock\|real` |
| FD-6 | MOCK 数据形态 | **预生成 JSON 快照 + 固定 seed** | 演示可复现 |
| FD-7 | 演示脚本 | **重写为逐屏点击脚本** | 《开发实施计划》§5.2 |

### N.2 5 屏 ↔ 端点 ↔ 演示映射

| 屏 | 名称 | 端点（§F.3） | 演示承载的硬验收 |
|---|------|-------------|-----------------|
| **1** | 任务下达 + 计划确认 | `POST /tasks`、`GET /tasks/{id}/plan`、`POST /tasks/{id}/confirm-plan` | 路径断言（含 `confirm_plan`）、冲突消解可视化 |
| **2** | 任务级批量审批 | `GET /approvals`、`GET /approvals/{batch_id}`、`POST /approvals/{batch_id}/sign\|reject`、`POST /approvals/{batch_id}/force-release` | **管理员强制放行被拒**、越权被拒并审计 |
| **3** | 决策回放 | `GET /decisions/{id}`、`GET /decisions/{id}/replay`、`GET /audit/records`、`GET /audit/verify`、`POST /audit/records/{id}/tamper` | 3 步还原、审计 append-only、哈希链篡改定位 |
| **4** | 治理看板 / 未触达分析 | `GET /suppressions`、`GET /suppressions/summary`、`GET /metrics/workers`、`GET /evals/summary` | 决策全貌（规则 ID 分布）、运营效率四项指标、一致率 |
| **5** | 系统配置 | `GET /providers`、`POST /providers/{id}/activate`、`GET/PUT /config/egress` | provider 切换、出境开关权限（仅管理员） |

### N.3 适配层契约（`ApiClient` 接口）

**分层原则**：前端业务组件**只 import `src/api`**，不得直接 `fetch`，不得直接 import mock 数据文件。

```typescript
// web/src/api/types.ts
export interface ApiClient {
  auth: {
    login(cred: Credentials): Promise<AuthToken>;
    logout(): Promise<void>;
    currentUser(): Promise<User>;
  };
  tasks: {
    create(req: CreateTaskRequest): Promise<Task>;
    plan(id: string): Promise<Plan>;
    confirmPlan(id: string): Promise<void>;
  };
  approvals: {
    list(filter?: ApprovalFilter): Promise<ApprovalBatch[]>;
    detail(batchId: string): Promise<ApprovalBatch>;
    sign(batchId: string, payload: SignPayload): Promise<void>;
    reject(batchId: string, reason: string): Promise<void>;
    /** 强制放行——命中抑制红线必被拒（D2 核心爆点） */
    forceRelease(batchId: string, candidateId: string): Promise<void>;
  };
  decisions: {
    detail(decisionId: string): Promise<Decision>;
    evidence(decisionId: string): Promise<EvidenceChain>;
    replay(decisionId: string): Promise<DecisionChain>;
  };
  audit: {
    list(filter?: AuditFilter): Promise<AuditRecord[]>;
    verify(): Promise<AuditVerifyResult>;
    /** 演示用：模拟直接 UPDATE 被 append-only 触发器拒绝 */
    tamper(recordId: string): Promise<void>;
  };
  suppressions: {
    list(filter?: SuppressionFilter): Promise<Suppression[]>;
    summary(filter?: SuppressionFilter): Promise<SuppressionSummary>;
  };
  metrics: {
    workers(filter?: MetricsFilter): Promise<WorkerMetrics>;
    cost(filter?: CostFilter): Promise<CostMetrics>;
    coverage(): Promise<CoverageReadiness>;
  };
  providers: {
    list(): Promise<Provider[]>;
    activate(id: string): Promise<void>;
  };
  config: {
    egress(): Promise<EgressConfig>;
    updateEgress(cfg: EgressConfig): Promise<void>;
    egressDeclaration(): Promise<EgressDeclaration>;
  };
  circuitBreaker: {
    trip(): Promise<void>;
    restore(): Promise<void>;
  };
  evals: { summary(): Promise<EvalSummary>; };
}

// web/src/api/index.ts —— 唯一出口
const client: ApiClient = import.meta.env.VITE_API_MODE === 'mock'
  ? new MockApiClient()
  : new RealApiClient();

export default client;
```

**三条硬约束**：

1. **`MockApiClient` 必须实现真实状态变更语义**——审批签署后列表状态变化、强制放行被拒并返回错误信封、审计篡改报错、哈希链校验定位到第 N 条。**Mock 是数据源替换，不是交互降级**。
2. **两套实现的错误格式完全一致**——统一信封 + `error.code` + `rule_id` + `retryable` + `meta.trace_id`（对齐 §F.2）。**保证切真 API 时前端零改动**。
3. **`RealApiClient` 在 MVP 只建骨架**（方法签名与 Mock 对齐，不实现真 fetch）——**适配层结构在 MVP 冻结**，真接入属 v0.7。

### N.4 Mock 数据契约

| 项 | 规格 |
|---|---|
| 生成方式 | `npm run mock:generate`，**固定 seed**（可复现，幂等） |
| 内容 | 2000 客户 + 决策结果 + 审计哈希链 + 审批批次 + 抑制记录 + 运营指标 + **provider 列表 + 出境配置 + 评测汇总 + 熔断初始状态** |
| 演示账号 | 4 角色账号（admin / owner / operator / compliance）；**口令由种子脚本生成并写入本地 `.env`，不入库**（Mock 模式校验种子账号；真 API 模式转发后端鉴权，前端不改） |
| 演示运行模式 | **MVP 演示固定跑 Mock 模式**（`VITE_API_MODE=mock`）。真实登录的**表单与 token 存取流程是真实的**（真表单 + 真 localStorage + 真 Authorization 头），仅**账号校验由 Mock 侧完成**。真 API 模式下同一套前端代码不改即可连真后端 |
| 与后端的关系 | **Mock 数据不等于后端不存在**——策略内核、策略一致性、审计链在后端**真实实现并由 CI 验证**；前端 Mock 只承载演示交互 |

> **README 必须声明**：MVP 演示数据为 MOCK 快照；策略内核与审计链为真实后端实现。**不得让听众误以为整个系统都是假数据**。
> **⚠️ 契约缺口**：§F.3 端点清单**没有 auth 相关端点**（无 `/login` / `/logout` / `/me`）。这是 FR-38 契约完备性的缺口——真 API 模式接入时需补。本 MVP 阶段不阻塞（演示跑 Mock 模式），但**须在 v0.7 真接入前补齐 auth 端点契约**。

### N.5 与前端相关的架构约束补强

| # | 约束 | 来源 |
|---|------|------|
| 1 | **前端屏与后端资源一一对应**，无"前端聚合后端"隐式行为（§F.1 原则 2 沿用） | §F.1 |
| 2 | **所有写操作响应携带四版本号快照**，前端必须展示"这次决策基于哪个版本" | §F.1 原则 4 |
| 3 | **错误响应的 `rule_id` 必须在前端可见**——决策回放页要能显示"因为哪条规则被拦" | §F.2 |
| 4 | **角色权限必须在 UI 层与 API 层双重校验**——前端隐藏无权入口**不等于**安全，后端仍须独立校验并审计越权 | FR-30 / 十一章 |
| 5 | **审计篡改与哈希链校验须有独立 UI 入口**——这是 D2 差异化的可演示接口（§F.3 `GET /audit/verify`） | §F.3 |
