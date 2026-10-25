# 上下文与 Token 优化方案（主会话 + 子代理）

| 项 | 内容 |
| --- | --- |
| 文档性质 | **工程效率规范**：如何在长文档 / 多文档协作中把 token 烧在刀刃上 |
| 编写日期 | 2026-10-25 |
| 适用范围 | **全局所有项目**（方案落地在 `~/.claude/`，跨项目复用） |
| 关联产物 | `~/.claude/CLAUDE.md`（全局纪律）、`~/.claude/agents/doc-review.md`（审阅子代理）、`~/.claude/hooks/guard-large-read.py`（拦截 hook） |

---

## 一、先搞清楚 token 烧在哪

### 1.1 三层开销

| 层 | 来源 | 量级 | 可否消除 |
| --- | --- | --- | --- |
| **A. 启动固定开销** | System prompt + 工具定义 + skills 清单 + MCP instructions + 各 hook 注入 | 20–28K | 可大幅裁剪，无法归零 |
| **B. 每轮追加** | `UserPromptSubmit` hook、task 提醒等 | 200–300 / 轮 | 部分可裁 |
| **C. 会话内放大** | **整读大文件、长 Write、反复读同一文件、失败的 subagent** | 可达总消耗的 60–70% | **这是主战场** |

### 1.2 核心认知

> **原文一旦进了上下文，就会一直占着直到 compaction。压缩不能免费回收。**
>
> 所以正解是**阻止原文进入**，而不是进完再压。压一次，钱已经烧了。

### 1.3 主会话 vs 子代理的本质区别

| | 主会话 | 子代理 |
| --- | --- | --- |
| 上下文窗口 | 共享，累积 | **独立，用完即弃** |
| 烧在这里的成本 | **高**（持续占用 + 影响后续决策） | 低（结束后释放） |
| 加载 CLAUDE.md | ✅ | ❌ |
| 生效 Hook | ✅ | ⚠️ 不确定，别依赖 |
| 主要杠杆 | Hook + CLAUDE.md + 工具选择 | **派活时的 prompt** |

**结论**：读取类工作尽量丢给子代理——但**必须约束它的输出**，否则它会把原文贴回来。

---

## 二、主会话优化（三层）

### 2.1 第一层：Hook 硬拦截（强制力最强）

**文件**：`~/.claude/hooks/guard-large-read.py`

**作用**：
- `Read` 超 150 行的 `.md/.yaml` 且未给 `offset/limit` → **自动把 limit 注入为 150**（`updatedInput`），并注入提示改用便宜路径
- `cat/head/tail` 大文档（命令位置，不含引号内文本）→ **直接拒绝**

**为什么用 Hook 不用 Skill**：Skill 靠我记得调用（一定会忘），Hook 由 harness 强制执行。

**技术要点（踩过的坑）**：

| 坑 | 现象 | 正确做法 |
| --- | --- | --- |
| `updatedInput` 不能跨工具 | 想把 `Read` 自动改成 MCP 调用 → 做不到 | 只能改道到「截断的 Read」；真正的压缩路径靠 `additionalContext` 提示 |
| Windows GBK 编码 | payload 含中文路径 → JSON 解码失败 → 被 `except` 吞掉 → **hook 静默失效** | 用 `sys.stdin.buffer.read().decode("utf-8")` 读 stdin |
| Bash 正则太宽 | `python -c "print('cat x.md')"` 被误拦 | 正则限制在**命令边界**：`^\|[\n;&\|(`]\|\$\(` 之后才是 cat/head/tail |

**配置**（`~/.claude/settings.json`）：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Read|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python \"$HOME/.claude/hooks/guard-large-read.py\"",
            "timeout": 10,
            "statusMessage": "context-guard"
          }
        ]
      }
    ]
  }
}
```

**逃生口**：显式给 `limit` 即放行（含 `Read(file_path=..., limit=N)` 用于真需要全文时）；Bash 命令含 `--full` 也放行。

### 2.2 第二层：配置裁剪（降启动开销）

同在 `~/.claude/settings.json`：

```json
{
  "skillListingMaxDescChars": 300,
  "skillListingBudgetFraction": 0.005,
  "enabledPlugins": {
    "serena@claude-plugins-official": false
  }
}
```

| 项 | 作用 | 省多少 |
| --- | --- | --- |
| `skillListingMaxDescChars: 300` | 截短每个 skill 的描述 | ~1–2K |
| `skillListingBudgetFraction: 0.005` | skills 清单占上下文比例从 1% 降到 0.5% | ~2K |
| 禁用不用的插件（serena / pencil 等） | 少加载 MCP instructions + 工具定义 + skills | ~2–4K |
| **可选**：`skillOverrides` 把长尾 skill 标 `"name-only"` 或 `"off"` | 清单里只露名字或不露 | 按条数 |

> **保守原则**：优先截短描述、降预算，**不轻易 `off`**——误禁一个要用的 skill 得不偿失。

### 2.3 第三层：纪律约定（软约束，但覆盖面最广）

**全局** `~/.claude/CLAUDE.md`（所有项目加载）：

```markdown
# 上下文纪律（全局，所有项目生效）

目标：不把原文压进上下文。原文一旦进了就永远占着，压缩不能免费回收。

- 读大文件：先 `Grep -n "<关键词>" <file>` 拿行号 → `Read(offset, limit)`。超 150 行的 .md/.yaml 有 hook 自动截断。
- 要摘要别要原文：`mcp__context-compress__execute_file(path=...)`（只回摘要）／`search(queries=[...])`（按需取片段）。
- 审阅 / 调研 / 长文落盘：丢给 `Agent(subagent_type="doc-review")`，只要 ≤500 词结论。
- 同一文件只读一次：后续改动用 Edit 定位，别重读全文。
- 多命令合并到 `mcp__context-compress__batch_execute`，一轮做完。
- 子代理先试小的：一个可用再并行开多个（失败会连 prompt 一起浪费）。
- Bash 里别 `cat` 大文档：`sed -n '起,止p'` 取区间，或 `--full` 显式声明要全文。
```

**项目** `CLAUDE.md` 只放项目专项（文档位置、权威源、红线），**别放通用纪律**——那会重复占 token。

---

## 三、子代理优化

### 3.1 关键认知

子代理**不加载 CLAUDE.md**，也吃不到全局纪律。它的行为**只由派活 prompt 和它的 agent 定义决定**。

所以子代理优化 = **① 自定义 agent 定义（自动）+ ② 派活 prompt 纪律（每次）**。

### 3.2 自定义 agent（自动化纪律）

**文件**：`~/.claude/agents/doc-review.md`

**要点**：
- **工具白名单**：只给 `Grep / Glob / Read / mcp__context-compress__*`，**不给 Write/Edit**（读写分离，防误改）
- **系统提示写死纪律**：禁止整读、禁止粘贴原文、禁止复述结构、禁止写"文档良好"这类评价
- **输出硬上限**：≤500 词，格式固定（`file:line + 一句话 + 修法`）
- **常见陷阱清单**：数字加总不符、"已修复"实为未修、跨文档引用对不上、示例推不出结论

**frontmatter 格式**：

```markdown
---
name: doc-review
description: 交叉审查规格文档……只报问题、不复述内容、输出硬上限 500 词
tools: Grep, Glob, Read, mcp__context-compress__search, mcp__context-compress__execute_file, ...
---

（系统提示正文）
```

派活时只需一句：`用 doc-review agent 审 <文件>，检查清单：<X>`。

### 3.3 派活 prompt 纪律（每次都要写）

即使有 agent 定义，派活 prompt 仍应点名：

```
读文件一律用 mcp__context-compress__execute_file，禁止 Read 全文。
只看 <file> 第 100–200 行，不要全文。
输出 ≤500 词，只报问题（file:line + 一句话 + 修法），禁止复述文档内容。
```

### 3.4 并行派活的省钱法

| 反模式 | 代价 | 正解 |
| --- | --- | --- |
| 3 个 agent 各读 5 份文档 | 重复读 15 次 | **分文档子集**，互不重叠 |
| 一次开 3 个全失败 | prompt 全废（实测 ~8K 白烧） | **先试 1 个小的**，成功再并行 |
| 让 agent 自己找范围 | 它会全文扫 | **给行号范围** |
| 不限输出 | 把原文贴回来 | **硬上限 + 禁止复述** |

---

## 四、推荐流程：多文档交叉审阅

```
① 主会话：mcp__context-compress__index(path=docs/xxx/*.md)   ← 建索引，原文不进上下文
② 派 1 个 doc-review：检查清单 + 指定文档子集 + ≤500 词
③ 需要深挖的点：单独派 doc-review，只看那一文件那一段
④ 先试 1 个，成功再并行
```

**索引先行**是关键一步：建完索引后，子代理走 `search` 按需取片段，而不是全读。

---

## 五、验证方法

### 5.1 Hook 逻辑（pipe-test）

```bash
# 整读大文件 → 应自动截断 + 注入提示
echo '{"tool_name":"Read","tool_input":{"file_path":"<某大文档>"}}' \
  | python ~/.claude/hooks/guard-large-read.py

# 显式 limit → 应无输出（放行）
echo '{"tool_name":"Read","tool_input":{"file_path":"<同文件>","offset":1,"limit":50}}' \
  | python ~/.claude/hooks/guard-large-read.py
```

**完整用例**（应 6/6 通过）：

| # | 用例 | 期望 |
| --- | --- | --- |
| 1 | 整读大文件 | 自动截断 + 注入提示 |
| 2 | 显式 limit | 放行 |
| 3 | 小文件 | 放行 |
| 4 | `cat` 大文档 | 拒绝 |
| 5 | 引号内提及 `cat` | 放行（不误拦） |
| 6 | 小文件 `cat` | 放行 |

> ⚠️ Windows 下 Python 默认 GBK，测试脚本要用 `python -X utf8` 或在脚本内 `sys.stdout.reconfigure(encoding="utf-8")`，否则中文输出静默丢失。

### 5.2 配置生效

```bash
python -X utf8 -c "import json;d=json.load(open(r'<user>/.claude/settings.json',encoding='utf-8'));print(d['hooks']['PreToolUse'][0]['matcher'])"
# 期望：Read|Bash
```

### 5.3 端到端

Hook 由 harness 在会话启动时加载。**当前会话看不到效果**，重启后随便整读一份大文档，应看到自动截断提示。若没生效：打开一次 `/hooks` 菜单强制重载配置。

---

## 六、全局复用地图

| 产物 | 路径 | 作用 | 范围 |
| --- | --- | --- | --- |
| Hook 脚本 | `~/.claude/hooks/guard-large-read.py` | 拦整读 / 自动截断 | ✅ 全局 |
| Hook 配置 | `~/.claude/settings.json` → `hooks` | 挂载 hook | ✅ 全局 |
| Skills 截短 | 同上 → `skillListing*` | 降启动开销 | ✅ 全局 |
| 插件禁用 | 同上 → `enabledPlugins` | 关不用的 MCP / skills | ✅ 全局 |
| 上下文纪律 | `~/.claude/CLAUDE.md` | 软约束，自动加载 | ✅ 全局 |
| 审阅子代理 | `~/.claude/agents/doc-review.md` | 自动带纪律的审阅 | ✅ 全局 |
| 项目专项规则 | `<project>/CLAUDE.md` | 文档位置、红线、权威源 | ⚠️ 仅项目 |

**迁移到新机器 / 新环境**：把 `~/.claude/` 下这 5 类文件拷过去即可，无需重新调参。

---

## 七、常见问题排查

| 症状 | 可能原因 | 处理 |
| --- | --- | --- |
| Hook 没生效 | 会话启动时配置目录未被监视 | 打开一次 `/hooks` 或重启 |
| Hook 静默不工作 | Windows GBK 编码吞掉 JSON | 脚本内 `sys.stdin.buffer.read().decode("utf-8")` |
| Hook 误拦 | Bash 正则匹配到引号内文本 | 正则限制在命令边界（`^` / `;` / `&` / `|` / `(` / `` ` `` / `$(`） |
| Hook 误放 | 文件行数算错 / 阈值太高 | 调 `CLAUDE_CTX_GUARD_MAX_LINES` 环境变量 |
| 子代理仍整读 | 没给 agent 定义 / prompt 没点名 | 用 `doc-review` agent + prompt 点名 `execute_file` |
| 启动仍很贵 | 有大量插件未禁 | `enabledPlugins` 关掉不用的；`skillOverrides` 标 `name-only` |
| 写文档后没落盘 | Write/Edit 调用参数写错 | 落盘后 `ls` / `grep` 核验一次 |

---

## 八、本方案的适用边界（别过度使用）

| 场景 | 建议 |
| --- | --- |
| 读 100 行以内的小文件 | **直接整读**，别折腾 |
| 真的需要全文（如做逐行 diff） | 显式给 `limit=N` 放行，别拆成 10 次读 |
| 调试时反复读同一个文件 | 正常——省 token 不该以牺牲调试效率为代价 |
| 一次性脚本跑批 | 用 `batch_execute`，但输出量大时同样要截断 |
| **写长文档** | **省不了**——输出 token 是硬成本，只能靠"分段写"避免一次写崩 |
