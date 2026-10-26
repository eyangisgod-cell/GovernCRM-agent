"""通用审计事件写入（红线③：append-only + 哈希链，写入唯一走 ``insert_statement_sql``）。

分层理由（与 ``src/approval/audit.py`` 同构）：**写库的活落在审计层**，业务层
（``src/graph`` 等）只负责装配事件载荷——``src/graph`` 因此保持零 DB 依赖
（dry-run 链路不连库，``tests/graph/test_workflow_structure.py`` 的守卫依赖这一点）。

口径（红线③）：

- ``record_hash`` 由**数据库**按 ``insert_statement_sql()`` 的同一口径算出，
  调用方不传（客户端算的哈希没有意义）；
- ``prev_hash`` = 该租户链尾的 ``record_hash``（首条为创世值 64 个 '0'）；
- 写入方与链校验脚本（``src/audit/hash_chain.py``）共用同一段 SQL 表达式——
  「字段规范化口径不一致 → 校验永远失败」的唯一解法是口径只有一份。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from src.audit.chain import chain_tip
from src.audit.hash_chain import insert_statement_sql

if TYPE_CHECKING:
    import psycopg

__all__ = ["AuditEvent", "write_audit_event"]


@dataclass(frozen=True)
class AuditEvent:
    """一条审计事件的载荷（字段与 ``audit_log`` 的业务列一一对应）。

    默认值对齐 ``src/approval/audit.py`` 的既有写法：``model_version`` /
    ``prompt_version`` / ``skill_version`` 在无模型参与时记 ``n/a``。
    """

    tenant_id: uuid.UUID
    trace_id: str
    action_type: str
    rule_version: str
    task_id: uuid.UUID | None = None
    worker_id: uuid.UUID | None = None
    object_type: str = "task_batch"
    object_id: str = ""
    actor_type: str = "system"
    actor_id: str = "system"
    actor_role: str = "system"
    hit_rule_ids: tuple[str, ...] = ()
    model_version: str = "n/a"
    prompt_version: str = "n/a"
    skill_version: str = "n/a"
    input_snapshot: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)


def write_audit_event(
    event: AuditEvent,
    *,
    conn: psycopg.Connection[tuple[object, ...]] | None = None,
) -> tuple[str, int, str]:
    """写入一条审计事件（红线③：唯一走 ``insert_statement_sql()``）。

    ``record_hash`` 由数据库算出（调用方不传）。``conn`` 缺省时按 ``DATABASE_URL``
    自建连接（生产路径）；测试可注入真 PG 连接。返回
    ``(audit_id, record_seq, record_hash)``。
    """
    # 局部 import：只有真正写库的路径才需要 DB 驱动（审计层其余部分零 DB 依赖）。
    import psycopg
    from psycopg.types.json import Jsonb

    owns_connection = conn is None
    if conn is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "写审计需要数据库：未设置 DATABASE_URL。"
                "请先 `cp .env.example .env` 并 `docker compose up -d`。"
            )
        conn = psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
        )

    try:
        prev_hash = chain_tip(conn, event.tenant_id)
        values: list[object] = [
            event.tenant_id,
            event.trace_id,
            event.task_id,
            event.worker_id,
            None,  # decision_id：事件不绑定单条 decision 行
            None,  # approval_id：MVP 无独立 approval 行
            event.actor_type,
            event.actor_id,
            event.actor_role,
            event.action_type,
            event.object_type,
            event.object_id or event.trace_id,
            list(event.hit_rule_ids),
            event.model_version,
            event.prompt_version,
            event.skill_version,
            event.rule_version,
            Jsonb(dict(event.input_snapshot)),
            Jsonb(dict(event.evidence)),
            Jsonb(dict(event.payload)),
            "sha256",  # hash_algo
            prev_hash,
        ]
        with conn.cursor() as cur:
            cur.execute(insert_statement_sql(), values)
            row = cur.fetchone()
        assert row is not None, "insert_statement_sql 应返回 (audit_id, record_seq, record_hash)"
        return str(row[0]), cast(int, row[1]), str(row[2])
    finally:
        if owns_connection:
            conn.close()
