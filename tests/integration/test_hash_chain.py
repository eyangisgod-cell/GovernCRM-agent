"""哈希链在**真 PostgreSQL** 上的端到端验证。

标记 `pg`：哈希链依赖 PG 的 jsonb 规范化与 sha256，**不得用 SQLite 或内存库替代**
（`.trellis/spec/backend/database-guidelines.md`）。

覆盖《GovernCRM_开发实施计划.md:43》点名的高危点：
「改一条记录的 hash → 校验脚本报错并定位到第 N 条」必须通过。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.types.json import Jsonb

from src.audit.hash_chain import (
    GENESIS_HASH,
    HASHED_FIELDS,
    canonical_sql,
    insert_statement_sql,
    verify_chain,
    verify_query_sql,
)

pytestmark = pytest.mark.pg


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env` 并 `docker compose up -d`，再跑 `make pg-test`。"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        yield connection


def _insert_chain_row(
    conn: psycopg.Connection[tuple[object, ...]],
    tenant_id: uuid.UUID,
    prev_hash: str,
    trace_id: str,
    action_type: str = "chain_test",
    object_id: str | None = None,
) -> tuple[str, str]:
    """按链口径写入一条审计记录，返回 (audit_id, record_hash)。"""
    sql = insert_statement_sql()
    values: list[object] = [
        tenant_id,  # tenant_id
        trace_id,  # trace_id
        None,  # task_id
        None,  # worker_id
        None,  # decision_id
        None,  # approval_id
        "system",  # actor_type
        None,  # actor_id
        None,  # actor_role
        action_type,  # action_type
        "customer",  # object_type
        object_id or str(uuid.uuid4()),  # object_id
        [],  # hit_rule_ids
        "m@test",  # model_version
        "p@test",  # prompt_version
        "s@test",  # skill_version
        "r@test",  # rule_version
        Jsonb({}),  # input_snapshot
        Jsonb({}),  # evidence
        Jsonb({"n": 1}),  # payload
        "sha256",  # hash_algo
        prev_hash,
    ]
    with conn.cursor() as cur:
        cur.execute(sql, values)
        row = cur.fetchone()
    assert row is not None
    return str(row[0]), str(row[2])


def _fetch_chain(
    conn: psycopg.Connection[tuple[object, ...]], tenant_id: uuid.UUID
) -> list[dict[str, object]]:
    with conn.cursor() as cur:
        cur.execute(verify_query_sql(f"'{tenant_id}'"))
        description = cur.description
        assert description is not None
        columns = [desc.name for desc in description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def test_chain_is_intact_after_normal_writes(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """正常写入的三条记录，链必须完整。"""
    tenant_id = uuid.uuid4()
    prev = GENESIS_HASH
    for index in range(3):
        _, prev = _insert_chain_row(conn, tenant_id, prev, f"chain-intact-{index}")

    result = verify_chain(_fetch_chain(conn, tenant_id))
    assert result.ok, f"链应完整，实际断裂：{[b.describe() for b in result.breaks]}"
    assert result.total == 3


def test_tampered_hash_is_detected_and_located_to_nth_record() -> None:
    """**核心负向用例**：改一条记录的 hash → 校验报错并定位到第 N 条。

    append-only 触发器会拦住 UPDATE，所以这里用「禁用触发器」模拟 DBA 级攻击
    （绕过应用层直接改库）——这正是哈希链要防的东西。

    全程在一个事务里，末尾回滚：篡改**不落库**。audit_log 是 append-only，
    落库的破坏没法清理干净，会把整条链永久弄脏。
    """
    with psycopg.connect(_database_url()) as conn:
        tenant_id = uuid.uuid4()
        prev = GENESIS_HASH
        for index in range(4):
            _, prev = _insert_chain_row(conn, tenant_id, prev, f"chain-tamper-{index}")
        conn.commit()  # 先落库：后面的回滚只应撤销篡改，不应撤销这些记录

        rows = _fetch_chain(conn, tenant_id)
        assert verify_chain(rows).ok, "篡改前链应完整"

        tamper_sql = (
            "update audit_log set record_hash = %s "
            "where tenant_id = %s and record_seq = ("
            "  select record_seq from audit_log where tenant_id = %s order by record_seq "
            "  offset 2 limit 1"
            ")"
        )

        # 触发器应当拦住普通 UPDATE
        with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
            cur.execute(tamper_sql, ("f" * 64, tenant_id, tenant_id))
        assert "append-only" in str(excinfo.value)
        conn.rollback()

        # 绕过触发器直接改（模拟 DBA 级攻击）
        with conn.cursor() as cur:
            cur.execute("alter table audit_log disable trigger trg_audit_log_append_only")
            cur.execute(tamper_sql, ("f" * 64, tenant_id, tenant_id))
            assert cur.rowcount == 1

        result = verify_chain(_fetch_chain(conn, tenant_id))
        assert not result.ok, "篡改后链必须报断裂"
        first = result.first_break
        assert first is not None
        assert first.index == 3, f"应定位到第 3 条，实际第 {first.index} 条"
        assert "内容哈希不符" in first.reason
        assert first.actual == "f" * 64

        conn.rollback()  # 篡改不落库（连同 disable trigger 一起回滚）

    # 回滚后链必须恢复完整，且没有留下破坏
    with psycopg.connect(_database_url()) as conn:
        assert verify_chain(_fetch_chain(conn, tenant_id)).ok, "回滚后链应恢复完整"


def test_canonical_content_is_stable_across_writes(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """同一份内容写两次，规范化结果必须完全一致（否则链会莫名其妙断）。

    键序由 jsonb 自身规范化决定（按键长、再按字节序），**不是**字典序——
    所以断言的是「同内容恒等」，不是「按字母排」。
    """
    tenant_id = uuid.uuid4()
    fixed_object = str(uuid.uuid4())
    _, first_hash = _insert_chain_row(
        conn, tenant_id, GENESIS_HASH, "chain-stable", object_id=fixed_object
    )
    _, _second_hash = _insert_chain_row(
        conn, tenant_id, first_hash, "chain-stable", object_id=fixed_object
    )

    with conn.cursor() as cur:
        cur.execute(
            f"select {canonical_sql('a')} as canonical from audit_log a "
            "where tenant_id = %s order by record_seq",
            (tenant_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 2
    canonicals = [str(row[0]) for row in rows]
    assert canonicals[0] == canonicals[1], "同内容的规范化结果必须一致"
    assert "action_type" in canonicals[0]
    assert "tenant_id" in canonicals[0]
    # prev_hash 不参与规范化——它只是链的输入，不在规范化内容里
    assert "prev_hash" not in canonicals[0]


def test_hashed_fields_match_live_table_columns(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """HASHED_FIELDS + ENVELOPE_FIELDS 必须等于真库的 audit_log 列全集。

    漏加一列 → 那一列的改动永远抓不到（哈希链形同虚设）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns "
            "where table_schema = 'public' and table_name = 'audit_log'"
        )
        live = {str(row[0]) for row in cur.fetchall()}
    covered = set(HASHED_FIELDS) | {
        "audit_id",
        "record_seq",
        "created_at",
        "prev_hash",
        "record_hash",
    }
    assert live - covered == set(), f"真库多出的列未纳入哈希口径：{sorted(live - covered)}"
    assert covered - live == set(), f"口径里多出的列在真库不存在：{sorted(covered - live)}"
