【任务】执行 GovernCRM M1 MVP 的 Wave 4（API 层 + 前端 + 评测 + 文档），在 F:\my-project\market-auto-agent，
严格按 Trellis 流程，主会话派子代理实施。

当前状态：Wave 3 已完成并归档，branch=feature/wave-3-execution-chain（或已合并后的 main），make check 全绿。
父任务 .trellis/tasks/10-25-m1-mvp 状态 in_progress。

## 执行方式
1. `python ./.trellis/scripts/task.py current`
2. 按 `docs/specs/MVP_任务拆解.md:293` 起「六、Wave 4」建子任务。四个阶段（**阶段间串行，阶段内并行**）：
   - Phase 1（后端 API）：T-F1 API 契约冻结 + 统一响应信封（依赖 T-E2/T-E5/T-E7）→ T-F2 契约测试七项门禁（依赖 T-F1）
   - Phase 2（评测，可与 Phase 1 并行）：T-G1 收尾（统一 path_assertions.yaml 与 src/graph/path_assert.py 的 REQUIRED_NODES 词表，见 Wave 3 遗留 #3）→ T-G2 CI 回放对照（依赖 T-G1 + T-B2）
   - Phase 3（前端，依赖 T-F1；Mock 模式）：T-FE1 脚手架 → T-FE2 ApiClient 接口 →（T-FE3 Mock 数据 ∥ T-FE12 RealApiClient 骨架）→ T-FE4 MockApiClient → T-FE5 登录 → T-FE6 布局 → T-FE7..T-FE11 五屏（可并行）→ T-FE13 演示走查
   - Phase 4（文档）：T-I1 README + 演示脚本 + ADR
3. 每个子任务：`task.py start <slug>` → 派 trellis-implement → 回来后派 trellis-check
   → 通过则 `task.py finish` + `archive <slug> --no-commit`
4. 子代理 prompt 第一行必须是：`Active task: .trellis/tasks/<task-path>`（取自 task.py current）
   正文只给三件事：① 目标 = 子任务 prd.md 的产出文件与验收命令；
   ② 读法 = 子任务 implement.md 的「读法」表（精确 file:line）；
   ③ 红线 = .trellis/spec/backend/ 下的 5 条红线。
5. 若子代理通道报 429 / Unsupported model：回退「主会话直接实施 + 自检」，
   并在报告里注明「缺第三方复核」。

## 各子任务验收命令（写进各子任务 prd.md，抄自 MVP_任务拆解.md，不自创）
- T-F1：`openapi.json` 快照入库；CI 快照比对通过
- T-F2：七项门禁全进 CI（① OpenAPI 快照一致 ② 契约测试全绿 ③ 错误码完备性
  ④ 所有写端点写审计 ⑤ 对外触达端点过闸门 ⑥ 真 PG 集成测试进 CI ⑦ 必经节点不可 opt-in）
- T-G1：`pytest tests/evaluation/test_benchmark_schema` 加载成功；5 条路径断言样本齐备
- T-G2：CI 跑回放 → 策略命中一致率 100%（20 条策略样本）；路径断言 5 条独立计（不计入一致率分母）
- T-FE1：`cd web && npm run build` 通过
- T-FE2：`npm run typecheck` 通过；接口方法与架构 v5.3 §F.3 端点逐一对应
- T-FE3：`npm run mock:generate` 幂等（两次生成产物一致）
- T-FE4：`npm run test:mock` 覆盖：登录、审批签署、强制放行拒绝、审计篡改、哈希链校验
- T-FE5：4 账号均可登录；token 存 localStorage + Authorization 头；未登录重定向
- T-FE6：5 屏路由可达；运营专员看不到系统配置入口
- T-FE7：确认计划按钮可用（confirm_plan 是 ADR-035 必经节点）；双平台冲突客户两个候选展示
- T-FE8：SUP_HIGH_VALUE 批次「强制放行」→ 被拒并展示错误信封（核心爆点）
- T-FE9：3 步还原决策链；改审计记录报错（append-only）；校验哈希链报错并定位第 N 条
- T-FE10：规则命中分布饼图；未触达列表；四项指标；一致率 100% 标签
- T-FE11：provider 切换（限运营负责人/管理员）；出境开关只读展示
- T-FE12：RealApiClient 与 MockApiClient 通过同一份 ApiClient 类型检查
- T-FE13：《开发实施计划》§5.2 逐屏点击脚本全通（含 4 角色切换）
- T-I1：演示脚本 §5.2 逐屏点击全流程通过（5 屏）

## 【TDD 纪律】—— 强制
每个行为走红→绿→重构循环，**先看到测试失败，才允许写实现代码**：
  1. 从子任务 prd.md 的「验收标准」直接翻译成 assert 写测试
  2. 跑 pytest/npm test 确认**红**，且红的理由必须对（断言不成立，不是 ImportError / 收集错误）
  3. 写让测试通过的**最少**实现
  4. 确认**绿**；重构，保持绿；下一个行为
配套硬纪律：
  - 测试名描述**行为**不描述实现；断言**具体**（错误信封里的 error.code/rule_id，不是 raises(Exception)）
  - **测试即规格**：每个测试 docstring 写明依据的权威条款（FR-38/FR-39/ADR-035 等）
  - **负向用例不许 skip**：环境缺失用 pytest.fail
  - 子代理须把「红→绿」的 pytest/npm 输出贴进报告
TDD 适用范围：T-F1/T-F2/T-G1/T-G2（后端契约与评测 = 纯校验逻辑，全量 TDD）。
T-FE1 脚手架类允许先搭骨架，但 **T-FE4..T-FE11 的负向交互路径（强制放行被拒、篡改报错、哈希链定位）仍须先有测试**。

## 硬约束
- 规则语义只认 docs/specs/GovernCRM_附录D_冻结版.md；PRD/架构的原附录 D 草案已作废。
- 5 条红线不可破：policy_kernel 纯函数零 LLM / DENY 不可绕过（含管理员）/
  审计 append-only + 哈希链且写失败即阻断业务 / 路径断言节点不可 opt-in /
  业务层零供应商依赖（import litellm 仅允许 src/providers/）。
- **每个 error.code 必须映射到某条 E，或显式登记为非业务错误码**（FR-38，PRD 附录 J.6 口径）。
- **契约测试第⑦项（必经节点不可 opt-in）= CI 静态扫描确认无跳过分支**——这是红线④的 CI 化，不得弱化。
- **对外触达端点必须过闸门**（契约第⑤项，红线④）；**所有写端点写审计**（契约第④项，红线③）。
- 写 audit_log 必须走 `src/audit/hash_chain.insert_statement_sql()`；加列须同步 HASHED_FIELDS。
- **API 租户鉴权**（Wave 3 遗留 #4）：T-F1 阶段接 T-E5 的 RBAC——每个路由标注所需 Permission，
  中间件调 `src/auth/rbac.check_permission`；未带租户/角色的请求 401，越权 403 + `record_denial`。
- **评测标注先于规则内核**——T-G1 样本已冻结（Wave 1 入库），本波只做词表统一与 T-G2 回放，
  **不得改动 20 条策略样本的标注结论**。
- **T-FE4 Mock 是数据源替换，不是交互降级**：审批签署后状态变化、强制放行被拒、篡改报错、
  链校验定位——全部真实语义，错误信封与 RealApiClient 完全一致。
- 前端技术决策见《开发实施计划》§五之二（FD-1~FD-7）：React 18 + TS + Vite + AntD + ECharts +
  TanStack Query；`VITE_API_MODE` env 开关；演示口令由种子脚本写入本地 .env，**不入库**。
- G-18：README **不宣称「越用越强」**（MVP 无蒸馏产出逻辑）；演示脚本用 §5.2 逐屏点击版
  （FD-7 修订；G-17 的 4 步叙述版已作废）；README 口径与 PRD v5.3 一致（自主度 L1、C1–C15、16 类异常）。
- 依赖白名单制：pyproject.toml 清单外的 Python 库禁止 import；web/ 侧依赖须在报告中列明新增包。
- 真 PG 验证：契约第⑥项集成测试不得用 SQLite 或内存库。
- **本波不写 migrations/**（位点表等已由 Wave 3 T-E7 完成）；如确需新迁移先停下来报告。
- 上下文纪律：文档超 150 行禁整读，Grep -n 拿行号 → Read(offset, limit)；
  大文件用 mcp__context-compress__execute_file 摘要。
- 不改 docs/specs/ 下任何文档；不提交 .trellis/；不 push。
- 遇与权威文档冲突时**停下来报告，不要自行拍板**。

## 完成标准
- 本波全部子任务验收过 + `make check` 全绿 + `cd web && npm run build` / `npm run typecheck` / `npm run test:mock` 全绿
- 输出《Wave 4 完成报告》：产出文件清单、验收命令实际输出、遗留问题
- **同时给出一份「待人工逐行 review 清单」**，至少覆盖：
  ① 统一响应信封与错误码→E 映射（FR-38）② 契约七项门禁的 CI 配置（尤其第⑤⑦项）
  ③ API 层 RBAC 中间件接线（越权 403 + record_denial）④ T-G2 回放对照的三要素比对口径
  ⑤ 前端强制放行被拒 / 篡改报错 / 链校验定位三个爆点的 Mock 语义
- 收尾时给出下一波/收尾会话提示词（若有）
- **本波做完即停**，不要顺手做下一波
