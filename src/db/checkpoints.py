"""位点表 ``task_checkpoint`` 的写入与查询辅助（T-E7 · G-04 / FR-14）。

只增不改：本模块只提供 ``record_checkpoint``（写入）与 ``list_checkpoints``（查询），
不提供任何更新/删除入口（约定见 migrations/003 头注释）。

连接用法与 `src.audit.hash_chain` 一致：调用方传入 psycopg 连接（本表**不走哈希链**）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg


@dataclass(frozen=True)
class TaskCheckpoint:
    """一行位点记录（查询结果 / 写入回读）。"""

    checkpoint_id: uuid.UUID
    tenant_id: uuid.UUID
    task_id: uuid.UUID
    node_name: str
    checkpoint_at: datetime
    decision_id: uuid.UUID | None
    langgraph_checkpoint_id: str | None


def record_checkpoint(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
    node_name: str,
    checkpoint_at: datetime | None = None,
    decision_id: uuid.UUID | None = None,
    langgraph_checkpoint_id: str | None = None,
) -> TaskCheckpoint:
    """写入一条位点记录并回读。

    ``checkpoint_at`` 缺省取数据库 ``now()``（与表默认值一致，避免应用/库时钟口径分裂）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "insert into task_checkpoint "
            "(tenant_id, task_id, node_name, checkpoint_at, decision_id, "
            "langgraph_checkpoint_id) "
            "values (%s, %s, %s, COALESCE(%s, now()), %s, %s) "
            "returning checkpoint_id, tenant_id, task_id, node_name, "
            "checkpoint_at, decision_id, langgraph_checkpoint_id",
            (
                tenant_id,
                task_id,
                node_name,
                _as_utc(checkpoint_at) if checkpoint_at is not None else None,
                decision_id,
                langgraph_checkpoint_id,
            ),
        )
        row = cur.fetchone()
    assert row is not None, "record_checkpoint: INSERT ... RETURNING 未返回行"
    return _row_to_checkpoint(row)


def list_checkpoints(
    conn: psycopg.Connection[tuple[object, ...]],
    *,
    task_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
) -> list[TaskCheckpoint]:
    """查询某任务的位点列表，按 ``checkpoint_at`` 升序（FR-14）。"""
    sql = (
        "select checkpoint_id, tenant_id, task_id, node_name, checkpoint_at, "
        "decision_id, langgraph_checkpoint_id "
        "from task_checkpoint where task_id = %s"
    )
    params: list[object] = [task_id]
    if tenant_id is not None:
        sql += " and tenant_id = %s"
        params.append(tenant_id)
    sql += " order by checkpoint_at asc, checkpoint_id asc"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [_row_to_checkpoint(r) for r in rows]


def _row_to_checkpoint(row: tuple[object, ...]) -> TaskCheckpoint:
    """把 psycopg 行转成 ``TaskCheckpoint``（逐字段断言类型，过 mypy）。"""
    checkpoint_at = row[4]
    langgraph_checkpoint_id = row[6]
    assert isinstance(checkpoint_at, datetime)
    assert langgraph_checkpoint_id is None or isinstance(langgraph_checkpoint_id, str)
    return TaskCheckpoint(
        checkpoint_id=uuid.UUID(str(row[0])),
        tenant_id=uuid.UUID(str(row[1])),
        task_id=uuid.UUID(str(row[2])),
        node_name=str(row[3]),
        checkpoint_at=checkpoint_at,
        decision_id=uuid.UUID(str(row[5])) if row[5] is not None else None,
        langgraph_checkpoint_id=langgraph_checkpoint_id,
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
