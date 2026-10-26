"""真 PostgreSQL 连通性与扩展检查（T0.1 环境自检）。

标记 `pg`：**不得用 SQLite 或内存库替代**——触发器 / 哈希链 / CHECK 约束的行为必须
在真 PostgreSQL 上验证（见 `.trellis/spec/backend/database-guidelines.md`）。

本用例只验证"环境本身可用"，不涉及业务表（业务表由 T0.2 的迁移建）。
"""

from __future__ import annotations

import os

import psycopg
import pytest

pytestmark = pytest.mark.pg


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # 不用 skip：可跳过的门等于没有门（v5.3 §F.4 ⑥⑦）。门禁连不上即失败。
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env` 并 `docker compose up -d`，再跑 `make pg-test`。"
        )
    # SQLAlchemy 风格的 URL 转成 psycopg 能吃的形式
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def test_pg_is_reachable_and_has_vector_extension() -> None:
    with psycopg.connect(_database_url()) as conn, conn.cursor() as cur:
        cur.execute("select current_setting('server_version_num')::int")
        row = cur.fetchone()
        assert row is not None
        version_num = int(row[0])
        assert version_num >= 160000, f"期望 PostgreSQL 16+，实际 server_version_num={version_num}"

        cur.execute("select extname from pg_extension where extname = 'vector'")
        assert cur.fetchone() is not None, "未启用 vector 扩展（pgvector 未装或初始化脚本未跑）"


def test_pg_is_writable() -> None:
    """占位用例：append-only 触发器由 T-E3 落地后在此扩展断言。

    当前只验证"真库可写普通表"，防止把 SQLite 当成真库用。
    """
    with psycopg.connect(_database_url(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("create table if not exists _t01_pg_probe (id int primary key)")
        cur.execute("insert into _t01_pg_probe (id) values (1) on conflict do nothing")
        cur.execute("select count(*) from _t01_pg_probe")
        count_row = cur.fetchone()
        assert count_row is not None
        assert count_row[0] >= 1
        cur.execute("drop table if exists _t01_pg_probe")
