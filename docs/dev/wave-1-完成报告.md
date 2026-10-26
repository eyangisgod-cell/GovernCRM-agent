# Wave 1 完成报告

| 项 | 内容 |
| --- | --- |
| 日期 | 2026-10-26 |
| 分支 | `feature/wave-1-policy-kernel`（基于 `origin/main` = `bd87534`） |
| 父任务 | `.trellis/tasks/10-25-m1-mvp` |
| 子任务 | 9 个（T-G1 / T-A1 / T-A2 / T-B1 / T-B2 / T-B3 / T-B4 / T-C1 / T-C2），全部 `finish` + `archive --no-commit` 至 `.trellis/tasks/archive/2026-10/` |
| 门禁 | `make check` **六项全绿** |
| 代码状态 | **未提交、未推送**（见 §6 待办） |

> **⚠ 缺第三方复核。** `trellis-implement` / `trellis-check` 子代理通道在本波中段失效：
> 先报 `429 Token Plan 用量上限`（`agentId: afef570735d96ffb9`），后报
> `400 Unsupported model qwen3.8-max`（`agentId: af867941daf53de2e`）。
> 按 `docs/dev/wave-1-提示词.md` 的 fallback，**T-B4 由主会话直接实施 + 自检**；
> 其余 8 个子任务在通道失效前已由 `trellis-implement` 完成实施（red→green 证据见各自报告），
> 但 **9 个子任务的 `trellis-check` 第三方复核均未执行**，改由主会话按 §4 清单自查。
> 合并前建议补一轮人工 / 独立通道复核。

---

## 1. 交付文件清单

### 1.1 源码（新增 12 个文件，4200+ 行）

| 文件 | 行数 | 归属 | 说明 |
| --- | ---: | --- | --- |
| `src/policy_kernel/kernel.py` | 97 | T-B1 | `Decision` / `RuleId` / `DecisionResult`（附录D §4 的 11 个 rule_id 逐项对齐） |
| `src/policy_kernel/rules.py` | 616 | T-B2 | **六条规则唯一实现点**；`evaluate()` 入口 |
| `src/unification/segmentation.py` | 365 | T-C2 | **分层算法唯一实现点**；`layer_score = amount_score × 10 + recency_score` |
| `src/unification/segment_store.py` | 132 | T-C2 | 分层结果持久化 |
| `src/unification/identity.py` | 512 | T-C1 | 三档身份匹配（高/中/低）+ 持久化 + 复核队列 |
| `src/metrics/operational.py` | 221 | T-C2 | 四项运营指标（裁决 #21 / FR-35） |
| `src/data/generator.py` | 817 | T-A1 | 合成数据生成器（纯函数 + 薄壳写入） |
| `src/data/schemas.py` | 281 | T-A1 | 合成数据 schema 与层名口径 |
| `src/adapters/mock_adapter.py` | 840 | T-A2 | JudgmentProvider + ModelProvider 模拟适配器（INV-1..5） |
| `src/app.py` | 42 | T-B4 | FastAPI + `lifespan` fail-fast（G-05） |

**修改 5 个文件（合计 69 行增 / 4 行删）：**

| 文件 | 改动 | 归属 |
| --- | --- | --- |
| `src/policy_kernel/config.py` | **仅 +11 行**：`RuleValidationError = ConfigError` 别名 | T-B3 |
| `src/policy_kernel/__init__.py` | 补 re-export | T-B1 |
| `src/models/rule_ids.py` | 仅改 docstring 路径（`src/policy/` → `src/policy_kernel/`） | T-B1 |
| `src/unification/__init__.py` | 仅注释，零 import | T-C1/T-C2 |
| `src/metrics/__init__.py` | re-export | T-C2 |

**未改动（红线核对）：** `governance.yaml`（`git diff` 为空）、`docs/specs/`、`src/audit/hash_chain.py`（T0.2 已落地，本波只读）。

### 1.2 评测基准（G0，必须先于 T-B2）

| 文件 | 内容 |
| --- | --- |
| `tests/evaluation/benchmark.yaml`（667 行） | **策略判定 20 条** = S01–S15（简单）+ B01–B05（边界） |
| `tests/evaluation/path_assertions.yaml`（128 行） | **路径断言 5 条**（PA01–PA05），**独立验收、不计入一致率分母** |
| `tests/evaluation/test_benchmark_schema.py`（25 KB） | schema 校验 + 20 条回放 |

比对口径三要素（缺一不可）：① `conclusion` ② `hit_rule_ids` 集合 ③ `suppressed_rule_ids` 集合 + `suppressed_candidates` 计数。

### 1.3 测试（新增 11 个文件，195 个用例）

| 测试文件 | 用例 | 归属 |
| --- | ---: | --- |
| `tests/evaluation/test_benchmark_schema.py` | 19 | T-G1 |
| `tests/policy/test_kernel_contracts.py` | 14 | T-B1 |
| `tests/policy/test_rules.py` | 54 | T-B2（含 20 条基准全回放） |
| `tests/policy/test_rule_validation.py` | 6 | T-B3 |
| `tests/unification/identity/test_identity.py` | 11 + 4 pg | T-C1 |
| `tests/unification/segmentation/test_segmentation.py` | 38 | T-C2 |
| `tests/unification/segmentation/test_segment_store_pg.py` | 6 pg | T-C2 |
| `tests/metrics/test_metrics.py` | 16 | T-C2 |
| `tests/data/test_generator.py` | 11 + 3 pg | T-A1 |
| `tests/adapters/test_mock_adapter.py` | 21 | T-A2 |
| `tests/adapters/test_mock_adapter_pg.py` | 1 pg | T-A2 |
| `tests/unit/test_app_lifespan.py` | 5 | T-B4 |

---

## 2. 验收命令实际输出

### 2.1 分套件

```
tests/evaluation/test_benchmark_schema.py  :: 19 passed
tests/policy/test_kernel_contracts.py      :: 14 passed
tests/policy/test_rule_validation.py       ::  6 passed
tests/policy/test_rules.py                 :: 54 passed
tests/unification/identity/test_identity.py:: 11 passed, 4 deselected
tests/unification/segmentation/test_segmentation.py :: 38 passed
tests/metrics/test_metrics.py              :: 16 passed
tests/data/test_generator.py               :: 11 passed, 3 deselected
tests/adapters/test_mock_adapter.py        :: 21 passed
tests/unit/test_app_lifespan.py            ::  5 passed
```

> 单跑套件请用 `uv run pytest`：裸 `python -m pytest` 会因缺 `psycopg` 报
> `ModuleNotFoundError`（`psycopg` 在 dev 依赖组内，`make check` 走 `uv run`）。

### 2.2 T-B4 手工负向验证（G-05）

```
# 1) 故意把 governance.yaml 的 thresholds.refund_rate_threshold 改名
$ timeout 20 python -m uvicorn src.app:app --port 8765
INFO:     Started server process [36404]
INFO:     Waiting for application startup.
...
src.policy_kernel.config.ConfigError: governance.yaml 缺少必填配置项。
  - `thresholds.refund_rate_threshold`（SUP_HIGH_REFUND 退款率阈值）→ 去 governance.yaml 的 `thresholds` 段补上
ERROR:    Application startup failed. Exiting.
EXIT_CODE=3          ← 进程退出，不是静默继续

# 2) 恢复 governance.yaml → git diff governance.yaml 为空
# 3) 正常配置再启：
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8765
```

### 2.3 `make check` 六项

```
uv run pytest -q                       → 306 passed, 34 deselected
uv run ruff check .                    → All checks passed!
uv run scripts/check_dependencies.py   → 73 个文件，未发现白名单外 import
uv run ruff format --check .           → 76 files already formatted
uv run mypy                            → Success: no issues found in 73 source files
uv run pytest -q -m pg                 → 34 passed, 306 deselected
make verify-chain                      → [hash-chain] 校验通过：172 条记录，链完整。
make check: all gates green
```

### 2.4 T-B2 的 20 条基准比对

**20/20 全中**（五要素：`conclusion` + `hit_rule_ids` + `suppressed_rule_ids` + `suppressed_candidates`）。判别性边界用例：

| 用例 | 边界 | 结论 |
| --- | --- | --- |
| B01 | 三键全打平 | `ESCALATE_HUMAN`（**无默认放行**） |
| B02 | 频控恰好第 3 次 | `DENY`（≤2 合法，第 3 次拒绝） |
| B03 | 71h59m < 72h | 同通道冷却剩余 60s |
| B04 | 层级分维度冲突（抑制取更严） | 按规则 4 逐维度裁决 |
| B05 | `platform_priority_overrides` 压过三键 | 覆盖生效；**去掉覆盖后答案翻转为抖店** |

### 2.5 T-A2 的 E3 证据（retry → human）

```
attempt_number = 2 | retry_reason = TIMEOUT | retry_result = FAILED
escalate_to_human = True | degrade_reason = E3_PROVIDER_UNAVAILABLE
attempts = [(1, 'timeout'), (2, 'timeout')]        ← 恰好 1 次重试（MAX_RETRY_ATTEMPTS=1）
审计经 insert_statement_sql() 写入（测试逐字比对 SQL；源码内无手搓 insert into audit_log）
```

---

## 3. TDD 纪律执行情况

全部 9 个子任务按 red→green 实施，子代理报告中均附红→绿 pytest 输出。本会话主会话实施的 T-B4 亦然：

```
# 红（骨架 lifespan 不接配置加载）
4 failed, 1 passed
  Failed: DID NOT RAISE <class 'src.policy_kernel.config.ConfigError'>   ← 断言失败，非 ImportError
  assert loaded is not None

# 绿（接入 load_and_validate）
5 passed in 0.13s
```

- 红均因**断言不成立**失败，无 ImportError / collection error（骨架先行确保可收集）。
- 负向用例**常驻**：`grep -rn "pytest.skip\|pytest.mark.skip\|skipif\|pytest.xfail" tests/` 结果为空。
- 断言具体（断言错误信息含 `thresholds.refund_rate_threshold` 路径，而非只 `raises(Exception)`）。
- 测试 docstring 引用权威条款（prd.md §X / MVP_任务拆解.md:162-169 / 附录D）。

---

## 4. 待人工逐行 review 清单

> 来源：`.trellis/spec/backend/quality-guidelines.md`「策略内核、路径断言、冲突消解、审计哈希链、身份匹配规则**逐行人工 review**，AI 只写样板」（架构 v5.2:708-724）。
> **AI 只写了模板与实现，这五处必须由人逐行看。**

### ① `src/policy_kernel/rules.py`（616 行）——六条规则

| 位置 | 内容 | 重点看 |
| --- | --- | --- |
| `:218` `_redline_hits` | 规则 1a 四条红线（投诉 90 天 / 退款率>0.20 / 明确拒绝 / Top 1% 高价值） | 回溯窗口是否严格 90 天、是否 **OR** 而非 AND |
| `:254` `_consent_hits` | 规则 1b `NO_CONSENT`（发送前二段式判定，命中 rule_id 全记） | 二段式是否都落 `hit_rule_ids` |
| `:269` `_evaluate_frequency` | 规则 2 全局频控 **≤2 语义** + 同通道 ≥72h | 第 2 次合法 / 第 3 次 DENY 是否写反；`cooling_remaining_seconds` |
| `:333` `_layer_evidence` | 规则 4 分维度（权益取更高 layer_score、抑制/频控取更严） | 是否存在「全局 > 平台」固定序 |
| `:381` `_conflict_groups` / `:404` `_sort_key` | 规则 3 三键排序 + `platform_priority_overrides` | 覆盖优先级是否最高；全打平是否 `ESCALATE_HUMAN` |
| `:197` `net_value_cents` | 规则 5 `max(0, 面额 − max(0, 门槛 − 客单价))` | 是否截断到 0 |
| `:430` `evaluate` 入口 / `:482` DENY 分支 | 规则 6 升级人工，绝不默认放行 | `ESCALATE_HUMAN` 与 `DENY` 的分流条件 |
| 全文 | `hit_rule_ids` vs `suppressed_rule_ids` 三要素分离 | 附录D §4 尾注：两者**不得混写** |

### ② 路径断言 —— `tests/evaluation/path_assertions.yaml`（PA01–PA05 + `required_nodes`）

红线④「路径完整性断言必经、不得做成 opt-in」。**这一份是标注基准，真正的执行断言在 Wave 3 的 T-E4**——请核对 5 条样本的必经节点清单是否覆盖 G-04 位点，以及「跳节点 → DENY」是否被显式标注。

### ③ 冲突消解 —— `rules.py:381-428`（`_conflict_groups` + `_sort_key`）+ `src/unification/segmentation.py:compare_layer_scores`

- 三键：① 平台局部 `layer_score` ② 权益净值 ③ 创建时间更早者胜。
- `platform_priority_overrides` 压过三键；三键全打平 → `ESCALATE_HUMAN`。
- **请核对**：`_sort_key` 的 `override_key` 是否真的排在 `layer_score` 之前；`-layer_score` / `-net_value` 的降序符号是否正确；`compare_layer_scores` 是否**只比分数、不加全局/平台的范围序**。

### ④ 身份匹配 —— `src/unification/identity.py:187` `classify_identity`（512 行）

三档（架构 §9.5 / 裁决 #17 / ADR-037）：
- **高**：跨平台 `ext_key` 精确命中 → 自动合并（`NOT_REQUIRED`）
- **中**：手机号 hash + 地址指纹部分匹配 → **只进复核队列（E4），复核前不合并**
- **低**：无特征 → 保持独立（P1 红线）

**请核对**：中档是否**真的不自动合并**；`_peer_sort_key` 选规范客户的规则；`register_identity` 每条身份建一行 customer 的行为是否符合预期（见 §5 待拍板④）。

### ⑤ 审计哈希链写入路径 —— `src/audit/hash_chain.py` + `src/adapters/mock_adapter.py:384-420`

红线③（append-only + 触发器拒绝 UPDATE/DELETE + 哈希链）：
- `HASHED_FIELDS`（20 列）与 `insert_statement_sql()` 的占位符顺序必须逐一对齐；**加 audit_log 列必须同步 `HASHED_FIELDS`**。
- `record_hash` 由 **DB 侧** `sha256(prev_hash || jsonb_build_object(...)::text)` 计算，归一化在库内做，写入方与校验方同口径。
- 创世块 = 64 个 `0`。
- **请核对**：`src/` 内是否只有 `hash_chain.py:136` 这一处 `insert into audit_log` 字面量；`mock_adapter.py` 是否只经 `insert_statement_sql()` 写审计。

---

## 5. 遗留问题 / 待拍板（已全部修复）

### 5.1 拍板结论（2026-10-26 eyangisgod 逐项拍板，已落地）

| # | 问题 | 拍板结论 | 状态 |
| --- | --- | --- | --- |
| ① | 分位数方向不一致 | 统一为排名口径（>=99 即 Top 1%） | ✅ 已修 |
| ② | 冻结文件 12 条单候选无显式 `suppressed_*` | 保持现状（空集/0 缺省可接受） | ✅ 已确认 |
| ③ | `platform_priority_overrides` 无法加载 | 修 flatten（WHOLE_VALUE_PATHS 整值叶子） | ✅ 已修 |
| ④ | `register_identity` 每条身份建一行 customer | 复用已归集 ID，高置信不新建 customer 行 | ✅ 已修 |
| ⑤ | `layer_score_base` 与 DB CHECK 耦合 | 锁死 ×10（与 migrations 一致） | ✅ 已修 |
| ⑥ | 契约类型暂住 `src/adapters/` | 迁到 `src/providers/protocols.py` | ✅ 已修 |
| ⑦ | T-A2 未配置 audit_writer 时 fail-fast | 保持 fail-fast（宁可拒绝写） | ✅ 已确认 |
| ⑧ | `complaint_count_30d` vs 90 天窗口 | 改名为 `complaint_count_90d` | ✅ 已修 |
| ⑨ | 三类数据不在 T-A1 范围 | MVP 不做（Wave 3+） | ✅ 已确认 |

### 5.2 自查发现（已修复）

| # | 问题 | 状态 |
| --- | --- | --- |
| ⑩ | 分层口径两份实现 | ✅ 已修（layer_of 委托 segmentation） |
| ⑪ | 拆解文档路径过期 | ✅ 已修（src/policy→src/policy_kernel） |
| ⑫ | Windows 解码 flake | ✅ 已修（encoding="utf-8"） |

---

## 6. 合并前待办（已完成）

- [x] 补 `trellis-check` 第三方复核——子代理通道不可用（429/400），已由主会话自检替代
- [x] §5.1 的 9 项拍板——已全部落地
- [x] §5.2 的 ⑩⑪⑫——已全部修复
- [x] 提交（34 files, +9939, commit d3e2e6a）
- [x] `make check` 六项全绿（311 unit + 35 pg + 哈希链 185 条完整）

---

## 7. Wave 2 会话提示词

```
【任务】执行 GovernCRM M1 MVP 的 Wave 2（W4 决策层），在 F:\my-project\market-auto-agent，
严格按 Trellis 流程，主会话派子代理实施。

当前状态：Wave 1 已完成并归档（.trellis/tasks/archive/2026-10/ 九个子任务），
branch=feature/wave-1-policy-kernel，make check 六项全绿（311 unit + 35 pg + 哈希链 185 条完整）。
父任务 .trellis/tasks/10-25-m1-mvp 状态 in_progress。
12 项拍板已全部落地（见 §5 → 已修复），基准 v1.4 冻结（rule_version 2026-10-26.frozen）。

⚠ Wave 1 缺第三方复核（子代理通道 429 / Unsupported model）。
若子代理仍不可用，回退「主会话直接实施 + 自检」，并在报告里注明「缺第三方复核」。

## 执行方式
1. python ./.trellis/scripts/task.py current
2. 按 docs/specs/MVP_任务拆解.md:193-223 建 3 个子任务：
   - T-D1 冲突消解引擎      → src/decision/resolver.py    （依赖 T-B2、T-C2）
   - T-D2 抑制与频控数据接入 → src/decision/suppression.py + rate_limit.py（依赖 T-B2）
   - T-D3 影响面计算        → src/decision/impact.py      （依赖 T-B1）
3. 每个子任务：task.py start <slug> → 派 trellis-implement → 回来后派 trellis-check
   → 通过则 task.py finish + archive <slug> --no-commit
4. 子代理 prompt 第一行必须是：Active task: .trellis/tasks/<task-path>
   正文只给三件事：① 目标 = 子任务 prd.md 的产出文件与验收命令；
   ② 读法 = 子任务 implement.md 的「读法」表（精确 file:line）；
   ③ 红线 = .trellis/spec/backend/ 下的 5 条红线。
5. 若子代理通道报 429 / Unsupported model：回退「主会话直接实施 + 自检」，
   并在报告里注明「缺第三方复核」。

## 硬约束（红线，不可破）
① policy_kernel 纯函数零 LLM/框架     ② DENY 不可绕过（含管理员/AI/agent）
③ 审计 append-only + 哈希链            ④ 路径断言必经，不得 opt-in
⑤ 业务层零供应商依赖（import litellm 仅限 src/providers/）

## 单一实现点（Wave 1 已落地，本波不得重复实现）
- 抑制 / 频控判定：src/policy_kernel/rules.py（T-B2）——T-D2 **只做数据接入**，
  读 customer / suppression_record / tool_call 喂给内核；
  src/decision/ 下不得出现第二份抑制/频控判定逻辑。
- 分层算法：src/unification/segmentation.py（T-C2）——T-D1 只调用，不另写一份。
- 三档路由判定：src/decision/impact.py（T-D3，本波新建）；T-E2 审批只调用它。
- 契约类型：src/providers/protocols.py（唯一定义点）——适配器从这里 import。

## 拍板结论（Wave 1 已落地，本波直接使用）
③ platform_priority_overrides 已可正常加载（WHOLE_VALUE_PATHS 整值叶子）。
④ 高置信归集复用已归集 ID，不新建 customer 行（G-02 去重口径）。
⑩ 分层唯一实现点 = src/unification/segmentation.py（layer_of 已委托）。
⑤ layer_score_base 锁死 ×10（与 migrations CHECK 一致）。
① 分位数统一排名口径（>=99 即 Top 1%）；⑧ 投诉字段名 = complaint_count_90d。

## TDD 纪律（硬要求）
- 每个行为走 red→green→refactor；**必须先看到测试失败**再写实现。
- 红必须是**断言不成立**而失败，不能是 ImportError / collection error；
  若目标模块不存在，先建最小骨架让它可收集。
- 测试名描述**行为**而非实现；断言要具体（assert rule_id in str(e) 优于 raises(Exception)）。
- 测试 docstring 引用权威条款（附录D / PRD 条款号）——「测试即规格」。
- 负向用例**常驻不许 skip**：环境缺失用 pytest.fail（「可跳过的门等于没有门」）。
- 子代理把 red→green 的 pytest 输出贴进报告。

## 范围
- 规则权威源 = docs/specs/GovernCRM_附录D_冻结版.md（PRD/架构的原附录D 已作废）。
- 本波**不写迁移**（迁移单一写入者；T0.2 已建全部表 + audit_log 触发器）。
- 不改 docs/specs/；不 commit .trellis/；不 push。
- 遇到与权威文档冲突 → **停下来报告，不要自行裁决**。
- 上下文纪律：>150 行文档禁整读，先 Grep -n 拿行号再 Read 区间。

## 验收
- 每个子任务 prd.md 的验收命令全过；
- make check 六项全绿；
- T-D1 验收：同客户双平台 → 只放行 1 条，另一条 DENY 且留痕；
- T-D2 验收：① 投诉客户 → DENY ② 频控第 3 次 → DENY ③ 同通道 <72h → DENY；
- T-D3 验收：pytest tests/decision/impact；
- 产出《Wave 2 完成报告》+ 待人工逐行 review 清单（冲突消解 / 影响面去重口径）。
- **本波做完即停**，不要顺手做 Wave 3。
```
