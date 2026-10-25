# 依赖许可核对台账（LICENSES）

| 项 | 内容 |
| --- | --- |
| 文档性质 | **依赖准入台账**：新增任何第三方库前必须先在此留一行结论 |
| 适用范围 | `pyproject.toml` 中 `runtime` / `dev` / `evaluation` 三档依赖组的**全部直接依赖** |
| 版本口径 | 下表「版本」列为 `uv.lock` 锁定版本（`uv sync` 后的实际解析结果） |
| 核对基准 | 各发行包在 PyPI 元数据中声明的许可 + 上游仓库 LICENSE 文件 |
| 纪律来源 | 架构 v5.2 §十五「合规纪律」；开发实施计划 §7.1「依赖准入」；PRD 第十五章 |

> **合规纪律（动工前必须完成核对）**
>
> 1. **引入前核对**：任何第三方库进入 `pyproject.toml` 之前，先在本表补一行「组件 / 版本 / 许可 / 来源 / 结论」；未留结论的库**不得进入代码库**。
> 2. **清单外禁止 import**：依赖白名单制。`pyproject.toml` 之外的第三方库一律禁止 import，由 `ruff.toml`（TID251）+ `scripts/check_dependencies.py`（全量扫描，`make scan-deps`）双重阻断。
> 3. **拒绝修改版商业条款组件**：不引入「修改版 Apache-2.0 且带多租户商业条款」的组件；引入前须核对 LICENSE **全文**，不能只看 SPDX 标识。
> 4. **只借鉴不复制**：外部项目仅参考设计思路与接口契约，**不复制源码**（开发实施计划 §7.2）。
> 5. **正式商用前复核**：本表结论基于当前锁定版本的公开许可信息；**商用前须核对 LICENSE 全文并请法务复核**（架构 v5.2 §十六 免责声明）。
> 6. **供应商 SDK 的额外约束**：业务层一律不得直接依赖供应商 SDK（红线⑤）。`litellm` 仅允许在 `src/providers/` 适配层内 import（N4 拍板 B）。

---

## 一、runtime 依赖组

| 组件 | 版本 | 许可 | 来源 | 结论 |
| --- | --- | --- | --- | --- |
| FastAPI | 0.162.0 | MIT | https://github.com/fastapi/fastapi | ✅ 准入。API 框架（架构 v5.2 §3.7）；无附加商业条款 |
| uvicorn | 0.54.0 | BSD-3-Clause | https://github.com/encode/uvicorn | ✅ 准入。ASGI 服务器，宽松许可 |
| Pydantic | 2.14.1 | MIT | https://github.com/pydantic/pydantic | ✅ 准入。数据模型与类型注解 |
| pydantic-settings | 2.16.1 | MIT | https://github.com/pydantic/pydantic-settings | ✅ 准入。配置与开关读取（含出境开关默认值） |
| SQLAlchemy | 2.1.6 | MIT | https://github.com/sqlalchemy/sqlalchemy | ✅ 准入。仓储层 ORM；**迁移仍走原始 SQL**，不用 ORM 自动迁移 |
| psycopg (+ psycopg-binary) | 3.3.6 | LGPL-3.0-only（wheel 内置 libpq 为 PostgreSQL License） | https://github.com/psycopg/psycopg | ✅ 准入。copyleft 仅约束**未修改的 psycopg 本身**，我们不改其源码、不静态链接进自有代码；binary wheel 内附 libpq 为宽松许可。**分发时须随附 LGPL 与 PostgreSQL License 文本** |
| pgvector (Python 绑定) | 0.5.1 | MIT | https://github.com/pgvector/pgvector-python | ✅ 准入。服务端扩展许可为 PostgreSQL License；复用 PG，**不引独立向量库** |
| LangGraph | 1.2.17 | MIT | https://github.com/langchain-ai/langgraph | ✅ 准入。执行循环编排；**LangSmith 商业托管服务不在依赖范围内**，本项目自建成本表 |
| langchain-core | 1.7.0 | MIT | https://github.com/langchain-ai/langchain | ✅ 准入。仅取模型/工具基础类型，不引入 LangChain 商业组件 |
| litellm | 1.106.3 | MIT | https://github.com/BerriAI/litellm | ✅ 准入（**限定 `src/providers/` 内 import**，N4 拍板 B）。传递依赖含 `openai` 等 SDK，**业务层不得直接 import 它们** |
| PyYAML | 6.0.3 | MIT | https://github.com/yaml/pyyaml | ✅ 准入。读取 `governance.yaml`（策略参数唯一来源） |
| httpx | 0.28.1 | BSD-3-Clause | https://github.com/encode/httpx | ✅ 准入。对外 HTTP 客户端 |

## 二、dev 依赖组

| 组件 | 版本 | 许可 | 来源 | 结论 |
| --- | --- | --- | --- | --- |
| ruff | 0.17.2 | MIT | https://github.com/astral-sh/ruff | ✅ 准入。lint + format + 依赖白名单（banned-api） |
| mypy | 2.4.0 | MIT | https://github.com/python/mypy | ✅ 准入。类型门禁 |
| pytest | 9.1.1 | MIT | https://github.com/pytest-dev/pytest | ✅ 准入。测试框架 |
| pytest-cov | 7.1.0 | MIT | https://github.com/pytest-dev/pytest-cov | ✅ 准入。覆盖率 |
| pytest-asyncio | 1.4.0 | Apache-2.0 | https://github.com/pytest-dev/pytest-asyncio | ✅ 准入。标准 Apache-2.0，**无多租户商业条款** |
| types-PyYAML | 6.0.12.20260906 | Apache-2.0 | https://github.com/python/typeshed | ✅ 准入。类型存根 |

## 三、evaluation 依赖组

| 组件 | 版本 | 许可 | 来源 | 结论 |
| --- | --- | --- | --- | --- |
| inspect-ai | 0.3.284 | MIT | https://github.com/UKGovernmentBEIS/inspect_ai | ✅ 准入（dev-only，评测/CI 用）。**锁版本**；其 `fastapi>=0.119.0` + `click` 排他区间约束由 CI 断言（开发实施计划 §7.1） |

## 四、容器镜像与基础设施

| 组件 | 版本 | 许可 | 来源 | 结论 |
| --- | --- | --- | --- | --- |
| pgvector/pgvector（PostgreSQL 16 + vector 扩展） | pg16 | PostgreSQL License | https://hub.docker.com/r/pgvector/pgvector | ✅ 准入。三合一存储底座（关系 + `SKIP LOCKED` 队列 + 向量） |
| Docker Compose | v5.1.4（宿主工具链） | Apache-2.0 | https://github.com/docker/compose | ✅ 准入。本地/CI 环境编排 |

## 五、明确**不引入**的组件（附理由，避免后续重复评估）

| 组件 | 许可 | 结论 | 理由 |
| --- | --- | --- | --- |
| Redis | BSD-3 | ⛔ 不引入 | 架构 v5.2 §3.7：缓存 = PG 表 + 进程内 LRU；队列用 PG `SKIP LOCKED`。少一个容器 |
| 独立向量库（Milvus / Qdrant / Weaviate 等） | 各自不同 | ⛔ 不引入 | 复用 PostgreSQL + pgvector，不新增中间件 |
| LlamaIndex | MIT | ⛔ 暂不引入 | MVP 明确裁剪 RAG 检索（任务拆解 §1.2 X4），v0.5 引入时再补本表 |
| fastmcp | MIT | ⛔ 暂不引入 | MVP 裁剪 MCP Server 对外暴露（§1.2 X5），v0.4 引入时再补本表 |
| RAGAS | Apache-2.0 | ⛔ 暂不引入 | 随 RAG 裁剪一并后置（RAG 部分评测才需要） |
| opik | Apache-2.0 | ⛔ 暂不引入 | 开发实施计划 §7.1：**推迟到第 2 周**；其 `litellm<1.106` 约束与 runtime 档 `litellm>=1.106` 冲突，引入即锁死 |
| promptfoo | MIT（npm） | ⛔ 暂不引入 | 非 Python 依赖，走 npm devDep；引入时须显式指定境内/本地模型，避免评测本身成为出境通道 |
| 低代码平台 / 通用 Agent 护栏 / 多 Agent 框架 / 记忆框架 / 工作流引擎 | 各自不同 | ⛔ 不引入 | 架构 v5.2 §3.x：通用护栏不重做，治理语义层自研；多 Agent 协作属 v0.4 |
| 策略引擎类 / RAG 评测类 / REST→MCP 暴露类候选 | 各自不同 | ⛔ 已否决 | 开发实施计划 §7.1「已否决」清单 |

---

## 六、传递依赖说明

- `litellm` 会传递引入若干供应商 SDK（如 `openai`）。**它们不在本台账的直接依赖范围内，也不在白名单内**：业务层 import 即被 `scripts/check_dependencies.py` 阻断；确需使用只能经 `src/providers/` 适配层。
- `inspect-ai` 传递引入 `click`、`httpx2` 等。本项目不直接依赖 `httpx2`（由 `inspect-ai` 固定版本约束带入），**不得在业务代码中直接 import**。
- 传递依赖的许可随上游变更，本台账只覆盖直接依赖；正式商用前以 `uv.lock` 全量清单 + LICENSE 全文复核为准。
