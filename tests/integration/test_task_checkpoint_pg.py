"""位点表 ``task_checkpoint`` 在**真 PostgreSQL** 上的行为验证（T-E7 / G-04）。

标记 ``pg``：CHECK 约束 / 幂等迁移**不得用 SQLite 或内存库替代**
（`.trellis/spec/backend/database-guidelines.md`）。

覆盖 prd.md 验收标准：

- 执行 003 后表存在且列集合 == 期望 7 列，**无 graph_state_snapshot 类大字段**；
- 迁移脚本幂等（重复执行不报错）；
- ``record_checkpoint`` 写入 + ``GET /api/v1/tasks/{task_id}/checkpoints``
  返回按 ``checkpoint_at`` 升序的完整字段列表（FR-14）；
- 接口只提供写入+查询：路由表中不存在该路径的 UPDATE/DELETE 方法。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from src.app import app
from src.db.checkpoints import list_checkpoints, record_checkpoint

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = ROOT / "migrations" / "003_task_checkpoint.sql"

EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "checkpoint_id",
        "tenant_id",
        "task_id",
        "node_name",
        "checkpoint_at",
        "decision_id",
        "langgraph_checkpoint_id",
    }
)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # 不用 skip：可跳过的门等于没有门（v5.3 §F.4 ⑥⑦）。
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env`，再跑 `make pg-test`。"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """先执行 003 迁移（幂等），再给出 autocommit 连接——与 test_core_tables 同风格。"""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute(sql)
        yield connection


def test_003_creates_table_with_expected_columns_and_no_snapshot(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns "
            "where table_schema = 'public' and table_name = 'task_checkpoint'"
        )
        columns = {str(row[0]) for row in cur.fetchall()}
    assert columns == EXPECTED_COLUMNS, f"列集合不符：{sorted(columns)}"
    forbidden = [c for c in columns if "snapshot" in c or "state" in c]
    assert not forbidden, f"不得存大字段快照：{forbidden}"


def test_003_is_idempotent() -> None:
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    with (
        psycopg.connect(_database_url(), autocommit=True) as connection,
        connection.cursor() as cur,
    ):
        cur.execute(sql)
        cur.execute(sql)


def test_003_index_and_check_constraints(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "select indexname from pg_indexes "
            "where schemaname = 'public' and tablename = 'task_checkpoint'"
        )
        indexes = {row[0] for row in cur.fetchall()}
    assert "idx_task_checkpoint_tenant_task_at" in indexes

    # NOT NULL / CHECK：node_name 空串与非法格式被拒
    tenant_id = uuid.uuid4()
    task_id = uuid.uuid4()
    with conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "insert into task_checkpoint (tenant_id, task_id, node_name) values (%s, %s, %s)",
            (tenant_id, task_id, "1-bad-name!"),
        )


def test_record_checkpoint_and_list_sorted(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    tenant_id = uuid.uuid4()
    task_id = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(minutes=5)
    first = record_checkpoint(
        conn,
        tenant_id=tenant_id,
        task_id=task_id,
        node_name="load_customers",
        checkpoint_at=base,
    )
    second = record_checkpoint(
        conn,
        tenant_id=tenant_id,
        task_id=task_id,
        node_name="policy_judge",
        checkpoint_at=base + timedelta(minutes=1),
        decision_id=uuid.uuid4(),
        langgraph_checkpoint_id="lg-abc-123",
    )
    assert first.checkpoint_id is not None
    assert second.langgraph_checkpoint_id == "lg-abc-123"

    rows = list_checkpoints(conn, task_id=task_id)
    assert [r.node_name for r in rows] == ["load_customers", "policy_judge"]
    assert rows[0].checkpoint_at <= rows[1].checkpoint_at
    assert rows[1].decision_id is not None

    # 其他 task 的位点不串味
    assert list_checkpoints(conn, task_id=uuid.uuid4()) == []


def test_api_returns_checkpoints_sorted(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    tenant_id = uuid.uuid4()
    task_id = uuid.uuid4()
    base = datetime.now(UTC) - timedelta(minutes=2)
    record_checkpoint(
        conn,
        tenant_id=tenant_id,
        task_id=task_id,
        node_name="collect_candidates",
        checkpoint_at=base,
    )
    record_checkpoint(
        conn,
        tenant_id=tenant_id,
        task_id=task_id,
        node_name="confirm_plan",
        checkpoint_at=base + timedelta(seconds=30),
        decision_id=uuid.uuid4(),
        langgraph_checkpoint_id="lg-xyz-789",
    )

    with TestClient(app) as client:
        response = client.get(f"/api/v1/tasks/{task_id}/checkpoints")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert [item["node_name"] for item in payload] == [
        "collect_candidates",
        "confirm_plan",
    ]
    for item in payload:
        assert set(item) == EXPECTED_COLUMNS
    assert payload[1]["langgraph_checkpoint_id"] == "lg-xyz-789"
    assert payload[1]["decision_id"] is not None


def test_api_has_no_update_or_delete_route() -> None:
    """只提供写入+查询：不存在指向 checkpoints 的 PATCH/PUT/DELETE 路由。"""
    forbidden_methods = {"PATCH", "PUT", "DELETE"}
    found = [
        (method, str(getattr(route, "path", "")))
        for route in app.routes
        for method in getattr(route, "methods", set())
        if "checkpoints" in getattr(route, "path", "") and method in forbidden_methods
    ]
    assert found == [], f"不得提供更新/删除接口：{found}"
