# GovernCRM 开发命令入口
#
# 本地与 CI 使用**同一个** `make check`（六项门禁的聚合入口，见
# .trellis/spec/backend/quality-guidelines.md）。任一子项失败即整体失败。
#
# 本地环境三步（详见 docs/dev/本地环境.md）：
#   1) uv sync                —— 装依赖（runtime / dev / evaluation 三档）
#   2) cp .env.example .env   —— 本地变量（只含占位符，真实口令不进库）
#   3) docker compose up -d   —— 起 PostgreSQL 16 + pgvector
#
# 默认目标：help
#
# ⚠️ Windows/MSYS 注意：**配方行（tab 开头）里不要写非 ASCII 字符**。
#    MSYS 会按 ANSI 代码页转换命令行，中文序列可能被转义成反斜杠，
#    导致 sh 报 `unexpected EOF while looking for matching '"'`。
#    中文注释与 `## 帮助` 文本放在**规则行**（非配方行）里是安全的。

.DEFAULT_GOAL := help

UV ?= uv
COMPOSE ?= docker compose

# Windows 控制台默认按本地代码页输出，中文日志会花屏；统一强制 UTF-8
export PYTHONIOENCODING := utf-8
export PYTHONUTF8 := 1

# 本地数据库连接串（与 docker-compose.yml 的默认值一致；可用环境变量覆盖）
# 主机名用 127.0.0.1 而非 localhost：部分 Windows 环境下 localhost 先解析到 ::1，
# 而 compose 只发布 IPv4 端口，导致 TCP 连接被静默丢包、连接耗时 100+ 秒。
PG_USER ?= governcrm
PG_PASSWORD ?= governcrm_dev_only
PG_DB ?= governcrm
PG_PORT ?= 5432
DATABASE_URL ?= postgresql+psycopg://$(PG_USER):$(PG_PASSWORD)@127.0.0.1:$(PG_PORT)/$(PG_DB)

.PHONY: help sync up down logs db-shell migrate \
        test lint format-check format typecheck pg-test check scan-deps clean

## ---------------------------------------------------------------- 基础 ----

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*## .*$$' $(MAKEFILE_LIST) \
		| sed -e 's/:[^:]*## /|/' \
		| awk -F'|' '{printf "  %-14s %s\n", $$1, $$2}'

sync: ## 同步依赖（uv 三档依赖组）
	$(UV) sync

## ------------------------------------------------------------ 环境起停 ----

up: ## 启动 PostgreSQL 16 + pgvector
	$(COMPOSE) up -d
	$(COMPOSE) ps

down: ## 停止本地环境（保留数据卷）
	$(COMPOSE) down

logs: ## 跟踪数据库日志
	$(COMPOSE) logs -f db

db-shell: ## 进入 psql
	$(COMPOSE) exec db psql -U $(PG_USER) -d $(PG_DB)

migrate: ## 按序应用 migrations/*.sql（幂等；迁移唯一写者见 database-guidelines.md）
	@if ls migrations/*.sql >/dev/null 2>&1; then \
		for f in $$(ls migrations/*.sql | sort); do \
			echo "==> applying $$f"; \
			$(COMPOSE) exec -T db psql -v ON_ERROR_STOP=1 < $$f || exit 1; \
		done; \
	else \
		echo "migrations/ has no migration files yet (T0.2 owns migrations/) -- skipped"; \
	fi

## ------------------------------------------------------------- 门禁项 ----

test: ## ① pytest（默认排除需要真 PG 的用例）
	$(UV) run pytest -q || test $$? -eq 5

lint: ## ② ruff lint + 依赖白名单扫描
	$(UV) run ruff check .
	$(UV) run python scripts/check_dependencies.py

format-check: ## ③ ruff format --check
	$(UV) run ruff format --check .

format: ## ruff format（就地格式化）
	$(UV) run ruff format .

typecheck: ## ④ mypy 类型检查
	$(UV) run mypy

pg-test: ## ⑤ 真 PostgreSQL 集成测试（append-only 触发器 / 哈希链 / CHECK 约束）
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; \
	DATABASE_URL="$${DATABASE_URL:-$(DATABASE_URL)}" $(UV) run pytest -m pg -q || test $$? -eq 5

scan-deps: ## ⑥ 零供应商依赖 + 依赖白名单扫描（可单独执行）
	$(UV) run python scripts/check_dependencies.py --verbose

## --------------------------------------------------------------- 聚合 ----

check: test lint format-check typecheck pg-test ## 全量门禁（本地与 CI 同一入口）
	@echo "make check: all gates green"

clean: ## 清理缓存与构建产物
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
