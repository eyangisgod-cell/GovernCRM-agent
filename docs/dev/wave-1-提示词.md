【任务】执行 GovernCRM M1 MVP 的 Wave 1（三路并行 A/B/C），在 F:\my-project\market-auto-agent，
严格按 Trellis 流程，主会话派子代理实施。

当前状态：Wave 0（T0.1–T0.4）+ T0.4 评审修复已完成并归档，branch=feature/governance，
工作区干净，make check 全绿（111 unit + 20 pg + 哈希链校验）。
父任务 10-25-m1-mvp 状态 in_progress [4/4 done]。

## 执行方式
1. python ./.trellis/scripts/task.py current
2. 按 docs/specs/MVP_任务拆解.md:110 起「Wave 1：三路并行」建子任务：
   - 【A】T-A1 合成数据生成器 / T-A2 模拟适配器
   - 【B】T-B1 决策数据结构+rule_id 枚举 / T-B2 六条规则 / T-B3 规则加载 fail-fast / T-B4 FastAPI lifespan
   - 【C】T-C1 身份匹配引擎（三档）/ T-C2 分层引擎 + 运营指标口径
   - **【G0】T-G1 评测标注基准**（见下方「T-G1 提前」段，必须先于 T-B2）
3. 每个子任务：task.py start <slug> → 派 trellis-implement → 回来后派 trellis-check
   → 通过则 task.py finish + archive <slug> --no-commit
4. 子代理 prompt 第一行必须是：Active task: .trellis/tasks/<task-path>（取自 task.py current）
   正文只给三件事：① 目标 = 子任务 prd.md 的产出文件与验收命令；
   ② 读法 = 子任务 implement.md 的「读法」表（精确 file:line）；
   ③ 红线 = .trellis/spec/backend/ 下的 5 条红线。
5. 若子代理通道报 429 / Unsupported model：回退「主会话直接实施 + 自检」，
   并在报告里注明「缺第三方复核」。

## 【T-G1 提前】—— 循环论证防线
`MVP_任务拆解.md:512` 明确：**T-G1（标注）必须在 T-B2（内核）之前**，
否则「一致率 100%」是自我循环。因此 T-G1（拆解文档里排在 Wave 4 的【F】路）
**在本波提前执行**，且必须早于 T-B2 开工。

T-G1 产出：
- `tests/evaluation/benchmark.yaml`（源自 `docs/specs/评测标注基准_v1.3.yaml`）
  —— 策略判定 20 条（15 简单 + 5 边界）= 一致率分母
- `tests/evaluation/path_assertions.yaml` —— 路径断言 5 条（跳过各必经节点），
  **独立验收，不计入一致率分母**
- rule_id 全部取自附录D §4 登记表（`src/models/rule_ids.py` 的 `REGISTERED_RULE_IDS`，
  共 11 个）
- 对照口径三要素全一致：① `conclusion` ② `hit_rule_ids` 集合 ③ `suppressed_rule_ids` 集合
  + `suppressed_candidates` 数量
- 验收：`pytest tests/evaluation/test_benchmark_schema` 加载成功；5 条路径断言样本齐备

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
    （如「附录D 规则 2」「PRD FR-01b」），让人能反查
  - **负向用例不许 skip**：环境缺失用 pytest.fail（沿用「可跳过的门等于没有门」）
  - 子代理须把「红→绿」的 pytest 输出贴进报告
TDD 适用范围（纯函数，标准红绿重构）：
  T-B2 六条规则 / T-C1 身份匹配 / T-C2 分层 / T-A1 合成数据生成器 / T-G1 标注基准。
  这些正是红线①覆盖区，TDD 等价于「规则语义逐条对附录D 验收」——
  **每个规则一条测试**，docstring 写明依据条款。
允许先搭骨架的：T-B4（FastAPI lifespan）、T-A2（适配器接线）——
  但**负向路径（fail-fast / E3 重试转人工）仍须先有测试**。

## 硬约束
- 规则语义只认 docs/specs/GovernCRM_附录D_冻结版.md；
  PRD v5.2 / 架构 v5.2 的附录 D 草案已作废，禁止按草案实现。
- 5 条红线不可破：policy_kernel 纯函数零 LLM / DENY 不可绕过（含管理员）/
  审计 append-only + 哈希链且写失败即阻断业务 / 路径断言节点不可 opt-in /
  业务层零供应商依赖（import litellm 仅允许 src/providers/）。
- **抑制/频控判定唯一实现点 = src/policy_kernel/rules.py（T-B2）**，
  src/decision/ 不得出现第二份；只做数据接入的子任务不重复实现。
- **分层算法唯一实现点 = src/unification/segmentation.py（T-C2）**，
  T-B2 只**调用**它，不另写一份。
- **写 audit_log 必须走 src/audit/hash_chain.insert_statement_sql()**，
  record_hash 由数据库算；不要自己拼 INSERT，否则哈希链断裂，
  make verify-chain 会红。给 audit_log 加列必须同步改 HASHED_FIELDS。
- layer_score = amount_score × 10 + recency_score，取值 11..33；
  三键排序 ① 平台局部 layer_score ② 权益净值 ③ 创建时间；
  platform_priority_overrides 最高优先；三键全平 → ESCALATE_HUMAN，不默认放行。
- 依赖白名单制：pyproject.toml 清单外的第三方库禁止 import。
- 真 PG 验证：触发器/哈希链/CHECK 约束不得用 SQLite 或内存库。
- 迁移单一写者：**本波不写迁移**（001_core_tables.sql 已由 T0.2 落地），
  如需新迁移先停下来报告。
- 目录命名统一用 **src/policy_kernel/**（已按权威文档对齐：验收命令 + v5.1:246 +
  v5.2:207 + 附录D:179 + PRD FR-29 四处一致）。
  ⚠ 拆解文档 MVP_任务拆解.md 里写的 `src/policy/*.py` 一律理解为 `src/policy_kernel/*.py`，
  勿建 `src/policy/` 目录。
- 上下文纪律：文档超 150 行禁整读，Grep -n 拿行号 → Read(offset, limit)；
  大文件用 mcp__context-compress__execute_file 摘要。
- 不改 docs/specs/ 下任何文档；不提交 .trellis/（已 gitignore）；不 push。
- 遇与权威文档冲突时**停下来报告，不要自行拍板**。

## 已完成的部分（不要重做）
- **T0.4 已落地 `src/policy_kernel/config.py`**，含 `load_and_validate()` /
  `apply_overrides()` / `ConfigError` / `REDLINE_RULE_IDS` / `REDLINE_PATHS`。
  → **T-B3 的校验逻辑已在 T0.4 完成**，本波 T-B3 只做：① 核对 T-B3 验收项
  ② 补 `RuleValidationError` 兼容别名（若拆解文档要求该名字）③ 写 T-B3 的验收测试。
  **不要重写 config.py 的校验逻辑。**
- `src/models/rule_ids.py` 已有 `REGISTERED_RULE_IDS`（11 个，附录D §4 顺序）。
  → T-B1 的 `RuleId` 枚举必须与它**逐项一致**（写契约测试比对），
  但**policy 内核不得 import src/models**（会带入 SQLAlchemy）。
- `src/audit/hash_chain.py` 已有哈希链口径 + `make verify-chain` 门禁。

## 各路验收命令（写进各子任务 prd.md）
- T-A1：`python -m src.data.generator --count 2000` 且
  `docker compose exec db psql -U governcrm -d governcrm -c "SELECT count(*) FROM customer WHERE _synthetic=true"`
  → 2000；五类风控样本（高投诉/高退款/明确拒绝/无授权/Top1%）各类 ≥10
- T-A2：`pytest tests/adapters/` 全绿；**E3 超时桩：触发后重试恰 1 次、转人工、审计有记录**
- T-G1：`pytest tests/evaluation/test_benchmark_schema` 加载成功；5 条路径断言样本齐备
- T-B1：`python -c "from src.policy_kernel.kernel import Decision, RuleId; print(len(list(RuleId)))"` ≥ 9
  （实际应为 11）+ 与 `src/models/rule_ids.py` 一致性测试
- T-B2：`pytest tests/policy/` 全绿；DENY 分覆盖全；**review 记录写入 PR**
- T-B3：① 合法配置加载成功 ② 非法配置（红线缺字段/阈值越界）→ 抛可操作错误
- T-B4：故意写错 governance.yaml → 启动进程退出（**不静默继续运行**）
- T-C1：`pytest tests/unification/identity`；低置信样本**未被合并**
- T-C2：`pytest tests/unification/segmentation` + `pytest tests/metrics`

## 完成标准
- 本波全部子任务验收过 + make check 全绿 + 各 checklist 全勾
- 输出《Wave 1 完成报告》：产出文件清单、验收命令实际输出、遗留问题
- **同时给出一份「待人工逐行 review 清单」**：
  src/policy_kernel/rules.py（六条规则）、路径断言、冲突消解、身份匹配、
  审计哈希链写入路径
  （quality-guidelines 要求这五处人工 review，AI 只写样板）
- 收尾时给出 Wave 2 的会话提示词
- **本波做完即停**，不要顺手做 Wave 2
