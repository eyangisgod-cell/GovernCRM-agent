"""T-E2 恢复重判审计写入（红线③：append-only + 哈希链，唯一走 ``insert_statement_sql``）。

权威依据：

- **红线③**：审计 append-only + 触发器拒 UPDATE/DELETE + 哈希链；
  写审计唯一入口 = ``src.audit.hash_chain.insert_statement_sql()``，
  ``record_hash`` 由数据库按同一口径算出（调用方不传）。
- **ADR-030**（架构 v5.2:238）：恢复重判结论必须写审计；
  ``rule_version`` = 重判时**当前** governance config 版本（非挂起时快照）。

**原子性（红线③：哈希链不可断）**：取链尾与插入落在**同一事务**内，且同租户写入
经 ``pg_advisory_xact_lock`` 串行（``src/audit/chain.py`` 是唯一实现点）。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from src.audit.chain import chain_tip, chain_write_transaction, retry_on_chain_conflict
from src.audit.hash_chain import insert_statement_sql

if TYPE_CHECKING:
    import psycopg

__all__ = ["DeniedCandidate", "RejudgeAudit", "write_rejudge_audit"]


@dataclass(frozen=True)
class DeniedCandidate:
    """一条被拒（DENY/抑制）候选的留痕（红线②：携带 rule_id 与理由）。"""

    candidate_id: str
    rule_id: str
    reason: str


@dataclass(frozen=True)
class RejudgeAudit:
    """恢复重判的审计载荷（ADR-030：``rule_version`` = 重判时当前版本）。"""

    tenant_id: uuid.UUID
    trace_id: str
    task_id: uuid.UUID | None
    worker_id: uuid.UUID | None
    approval_status: str
    rule_version: str
    pending_candidate_ids: tuple[str, ...]
    sendable_candidate_ids: tuple[str, ...]
    denied: tuple[DeniedCandidate, ...]
    impact_unique_customers: int
    signers: tuple[str, ...]


def _insert_rejudge_row(
    conn: psycopg.Connection[tuple[object, ...]], record: RejudgeAudit
) -> tuple[str, int, str]:
    """在**同一事务**内「取链尾 → 插入」一条恢复重判审计（红线③：哈希链不可断）。"""
    # 局部 import：仅 IO 路径需要 Jsonb 包装
    # （模块级零 DB 依赖——``import src.graph.workflow`` 不加载 psycopg）。
    from psycopg.types.json import Jsonb

    with chain_write_transaction(conn, record.tenant_id):
        prev_hash = chain_tip(conn, record.tenant_id)
        hit_rule_ids = [denied.rule_id for denied in record.denied]
        input_snapshot = {
            "pending_candidate_ids": list(record.pending_candidate_ids),
            "impact_unique_customers": record.impact_unique_customers,
        }
        evidence = {
            "adr": "ADR-030",
            "approval_status": record.approval_status,
            "signers": list(record.signers),
            "denied": [
                {
                    "candidate_id": denied.candidate_id,
                    "rule_id": denied.rule_id,
                    "reason": denied.reason,
                }
                for denied in record.denied
            ],
        }
        payload = {
            "decision": "rejudge",
            "approval_status": record.approval_status,
            "rule_version": record.rule_version,
            "sendable_candidate_ids": list(record.sendable_candidate_ids),
        }

        # HASHED_FIELDS 顺序（见 hash_chain.HASHED_FIELDS）+ prev_hash。
        values: list[object] = [
            record.tenant_id,
            record.trace_id,
            record.task_id,
            record.worker_id,
            None,  # decision_id：恢复重判不绑定单条 decision 行
            None,  # approval_id：MVP 无独立 approval 行，状态记在 payload
            "system",  # actor_type：重判由系统在 resume 路径执行
            "gate-rejudge",  # actor_id
            "system",  # actor_role
            "gate_rejudge",  # action_type
            "task_batch",  # object_type
            str(record.task_id) if record.task_id else record.trace_id,
            hit_rule_ids,
            "n/a",  # model_version
            "n/a",  # prompt_version
            "n/a",  # skill_version
            record.rule_version,
            Jsonb(input_snapshot),
            Jsonb(evidence),
            Jsonb(payload),
            "sha256",  # hash_algo
            prev_hash,
        ]

        with conn.cursor() as cur:
            cur.execute(insert_statement_sql(), values)
            row = cur.fetchone()
        assert row is not None, "insert_statement_sql 应返回 (audit_id, record_seq, record_hash)"
        return str(row[0]), cast(int, row[1]), str(row[2])


def write_rejudge_audit(
    record: RejudgeAudit,
    *,
    conn: psycopg.Connection[tuple[object, ...]] | None = None,
) -> tuple[str, int, str]:
    """写入一条「恢复重判」审计记录（红线③：唯一走 ``insert_statement_sql``）。

    ``record_hash`` 由数据库算出（调用方不传）。``conn`` 缺省时按 ``DATABASE_URL``
    自建连接（graph 生产路径）；测试可注入真 PG 连接。返回 ``(audit_id, record_seq,
    record_hash)``。

    **原子性**：取链尾与插入同事务、同租户串行；链尾被并发写入方抢占时重试一次，
    仍失败则异常上抛（审计失败不得被吞，红线③）。
    """
    # 局部 import：仅 IO 路径需要 psycopg 驱动
    # （模块级零 DB 依赖——``import src.graph.workflow`` 不加载 psycopg）。
    import psycopg

    owns_connection = conn is None
    connection = conn
    if connection is None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "写恢复重判审计需要数据库：未设置 DATABASE_URL。"
                "请先 `cp .env.example .env` 并 `docker compose up -d`。"
            )
        # 非 autocommit：取链尾与插入必须落在同一事务内（autocommit 会把
        # 「取链尾」与「插入」拆成两个事务 → 竞态窗口 → 断链）。
        connection = psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1))

    try:
        return retry_on_chain_conflict(lambda: _insert_rejudge_row(connection, record))
    finally:
        if owns_connection:
            connection.close()
