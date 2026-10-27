"""审计哈希链的原子性守卫（红线③：哈希链不可断）。

**缺陷背景**（人工 QA 复核发现）：审计写入原为「取链尾（SELECT）→ 插入（INSERT）」
两步，且写入方用 ``autocommit``——SELECT 立即提交、INSERT 另起事务。同租户两个
写入方并发时读到**同一链尾**，两条记录挂同一个 ``prev_hash`` → 哈希链断裂、
``verify_chain`` 校验失败（红线③被破）。

**两层防线**（本文件逐层断言）：

1. **应用层**：取链尾与插入落在**同一事务**内，且同租户写入经
   ``pg_advisory_xact_lock`` 串行——**锁必须在取链尾之前拿**
   （实现点唯一：``src/audit/chain.py``）。
2. **DB 层**：唯一索引 ``idx_audit_log_tenant_prev_hash``（迁移 004）把
   「同一 ``prev_hash`` 被写两次」变成**写失败**——静默断链 → 显式阻断。

纯单元层用假连接断言「执行序列 + 事务嵌套深度」；真 PG 层并发写入后校验链完整
（真 PG 用例标 ``@pytest.mark.pg``，默认门禁 ``-m "not pg"`` 不跑，显式
``make pg-test`` 才跑——**不 skip、不伪造通过**）。
"""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg.types.json import Jsonb

from src.audit.chain import (
    CHAIN_LOCK_SQL,
    CHAIN_TIP_SQL,
    ChainWriteConnection,
    chain_tip,
    chain_write_transaction,
    retry_on_chain_conflict,
)
from src.audit.events import AuditEvent, write_audit_event
from src.audit.hash_chain import GENESIS_HASH, insert_statement_sql, verify_chain, verify_query_sql

TENANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
MIGRATION_004 = (
    Path(__file__).resolve().parents[2] / "migrations" / "004_audit_log_chain_unique.sql"
)


# ---------------------------------------------------------------------------
# 假连接：记录执行序列与事务嵌套深度
# ---------------------------------------------------------------------------


class _FakeTransaction:
    """假事务上下文：维护事务嵌套深度（断言「取链尾与插入同事务」用）。"""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def __enter__(self) -> _FakeTransaction:
        self._connection.transaction_depth += 1
        self._connection.events.append("tx_begin")
        return self

    def __exit__(self, *exc: object) -> None:
        self._connection.transaction_depth -= 1
        self._connection.events.append("tx_end")
        return None


class _FakeCursor:
    """假游标：记录执行的 SQL 及其所在的事务深度。"""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def execute(self, query: str, params: Any = None) -> None:
        self._connection.statements.append(query)
        self._connection.statement_depths.append(self._connection.transaction_depth)
        self._connection.last_query = query

    def fetchone(self) -> Any:
        if self._connection.last_query.startswith("insert into audit_log"):
            self._connection.insert_count += 1
            return (
                f"audit-{self._connection.insert_count}",
                self._connection.insert_count,
                "c" * 64,
            )
        if self._connection.last_query.startswith("select record_hash from audit_log"):
            return self._connection.tip_row
        return None

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _FakeConnection:
    """假连接：只记录 SQL 与事务深度，不连库。"""

    def __init__(self, tip_row: Any = None) -> None:
        self.statements: list[str] = []
        self.statement_depths: list[int] = []
        self.events: list[str] = []
        self.transaction_depth = 0
        self.insert_count = 0
        self.tip_row = tip_row
        self.last_query = ""

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)


def _fake(conn: _FakeConnection) -> Any:
    """把假连接伪装成 psycopg 连接（测试只走协议，不碰驱动）。"""
    return cast(Any, conn)


# ---------------------------------------------------------------------------
# 纯单元层（零 DB 依赖）
# ---------------------------------------------------------------------------


def test_lock_is_transaction_scoped_advisory_lock() -> None:
    """锁必须是**事务级** advisory 锁（``pg_advisory_xact_lock``）。

    会话级锁（``pg_advisory_lock``）会在连接归还前一直占着；事务级锁随事务结束
    自动释放，且**只有与插入同事务**才真正起互斥作用。
    """
    assert "pg_advisory_xact_lock" in CHAIN_LOCK_SQL
    assert "pg_advisory_lock(" not in CHAIN_LOCK_SQL.replace("pg_advisory_xact_lock(", "")


def test_lock_is_executed_inside_the_transaction() -> None:
    """写锁必须在事务内拿：autocommit 下事务级锁会立刻释放 = 等于没锁。"""
    conn = _FakeConnection()
    with chain_write_transaction(cast(ChainWriteConnection, _fake(conn)), TENANT_ID):
        pass

    assert conn.statements == [CHAIN_LOCK_SQL]
    assert conn.statement_depths == [1], "advisory 锁必须在事务内执行"
    assert conn.events == ["tx_begin", "tx_end"]


def test_lock_precedes_chain_tip_select() -> None:
    """**锁在取链尾之前**——否则锁住的是已经读到的旧链尾，互斥无意义。"""
    conn = _FakeConnection()
    with chain_write_transaction(cast(ChainWriteConnection, _fake(conn)), TENANT_ID):
        chain_tip(_fake(conn), TENANT_ID)

    lock_index = conn.statements.index(CHAIN_LOCK_SQL)
    tip_index = conn.statements.index(CHAIN_TIP_SQL)
    assert lock_index < tip_index, f"执行顺序错：{conn.statements}"


def test_write_audit_event_reads_tip_and_inserts_in_one_transaction() -> None:
    """**取链尾与插入必须同事务**（修前是 autocommit 两步 → 竞态窗口 → 断链）。"""
    conn = _FakeConnection()
    event = AuditEvent(
        tenant_id=TENANT_ID,
        trace_id="trace-atomicity",
        action_type="unit_test",
        rule_version="rule-v1",
    )
    write_audit_event(event, conn=cast(Any, _fake(conn)))

    tip_index = conn.statements.index(CHAIN_TIP_SQL)
    insert_index = next(
        index
        for index, statement in enumerate(conn.statements)
        if statement.startswith("insert into audit_log")
    )
    assert conn.statement_depths[tip_index] == 1, "取链尾必须在事务内"
    assert conn.statement_depths[insert_index] == 1, "插入必须在同一事务内"
    assert conn.events == ["tx_begin", "tx_end"], "一次写入只开一个事务"
    assert conn.statements.index(CHAIN_LOCK_SQL) < tip_index


def test_write_audit_event_uses_the_shared_chain_tip_helper() -> None:
    """写入方不得自抄一份取链尾 SQL（口径只有一份）。"""
    conn = _FakeConnection()
    event = AuditEvent(
        tenant_id=TENANT_ID,
        trace_id="trace-shared-tip",
        action_type="unit_test",
        rule_version="rule-v1",
    )
    write_audit_event(event, conn=cast(Any, _fake(conn)))

    assert CHAIN_TIP_SQL in conn.statements


def test_retry_on_chain_conflict_retries_once() -> None:
    """唯一约束冲突（链尾被抢占）→ **重试一次**（重读链尾 + 重写）。"""
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise psycopg.errors.UniqueViolation("duplicate key value")
        return "written"

    assert retry_on_chain_conflict(operation) == "written"
    assert len(calls) == 2, "只重试一次"


def test_retry_on_chain_conflict_does_not_swallow_failure() -> None:
    """重试仍失败 → 异常上抛：**审计写入失败不得被静默吞掉**（红线③）。"""
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        raise psycopg.errors.UniqueViolation("duplicate key value")

    with pytest.raises(psycopg.errors.UniqueViolation):
        retry_on_chain_conflict(operation)
    assert len(calls) == 2, "只重试一次；第二次失败必须上抛"


# ---------------------------------------------------------------------------
# 真 PG 层（并发写入 + DB 级不变量）
# ---------------------------------------------------------------------------


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env` + `docker compose up -d`，再跑 `make pg-test`。"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def pg_conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """先幂等应用迁移 004，再给出连接（与 tests/integration 既有风格一致）。"""
    sql = MIGRATION_004.read_text(encoding="utf-8")
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        with connection.cursor() as cur:
            cur.execute(sql)
        yield connection


def _insert_raw_row(
    conn: psycopg.Connection[tuple[object, ...]],
    tenant_id: uuid.UUID,
    prev_hash: str,
    trace_id: str,
) -> None:
    """按链口径直接写一条记录（用于构造 DB 级不变量的负向用例）。"""
    values: list[object] = [
        tenant_id,
        trace_id,
        None,
        None,
        None,
        None,
        "system",
        None,
        None,
        "chain_unique_test",
        "customer",
        str(uuid.uuid4()),
        [],
        "m@test",
        "p@test",
        "s@test",
        "r@test",
        Jsonb({}),
        Jsonb({}),
        Jsonb({}),
        "sha256",
        prev_hash,
    ]
    with conn.cursor() as cur:
        cur.execute(insert_statement_sql(), values)


@pytest.mark.pg
def test_duplicate_prev_hash_is_rejected_by_unique_index(
    pg_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """DB 级不变量：同一 ``prev_hash`` 写两次 → **写失败**（静默断链 → 显式阻断）。"""
    tenant_id = uuid.uuid4()
    _insert_raw_row(pg_conn, tenant_id, GENESIS_HASH, "unique-1")

    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_raw_row(pg_conn, tenant_id, GENESIS_HASH, "unique-2")


@pytest.mark.pg
def test_concurrent_writes_to_same_tenant_keep_chain_intact(
    pg_conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """真 PG：同租户两个连接**并发**写审计 → 链必须完整（修前会分叉断链）。"""
    tenant_id = uuid.uuid4()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(tag: str) -> None:
        try:
            with psycopg.connect(_database_url()) as conn:
                barrier.wait(timeout=30)
                for index in range(5):
                    write_audit_event(
                        AuditEvent(
                            tenant_id=tenant_id,
                            trace_id=f"concurrent-{tag}-{index}",
                            action_type="concurrency_test",
                            rule_version="rule-v1",
                        ),
                        conn=conn,
                    )
        except BaseException as exc:
            # 测试要把失败原因带回主线程（不让线程里的异常被静默吞掉）
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"并发写入失败：{errors}"

    with pg_conn.cursor() as cur:
        cur.execute(verify_query_sql(f"'{tenant_id}'"))
        description = cur.description
        assert description is not None
        columns = [desc.name for desc in description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    result = verify_chain(rows)
    assert result.ok, f"链必须完整，实际断裂：{[b.describe() for b in result.breaks]}"
    assert result.total == 10, f"应有 10 条记录，实际 {result.total}"
