"""模拟适配器的**真 PostgreSQL** 用例（T-A2，标记 `pg`）。

验证重试审计真落库并接进哈希链（红线③：审计 append-only + 哈希链）：

- 写入唯一口径是 `insert_statement_sql()`，`record_hash` 由数据库算出；
- 租户首条记录的 `prev_hash` 为创世值，链可校验；
- `payload` 含 `attempt_number` / `retry_reason` / `retry_result`（架构 v5.3 §I.2 INV-5）。

本文件不在默认集运行（`pytest -m "not pg"`）；用 `make pg-test` 跑。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any, cast

import psycopg
import pytest

from src.adapters.mock_adapter import (
    AUDIT_ACTION_RETRY,
    MockModelProvider,
    pg_audit_writer,
)
from src.audit.hash_chain import verify_chain, verify_query_sql
from src.providers.protocols import GenerationRequest, Message

pytestmark = pytest.mark.pg


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # 不用 skip：可跳过的门等于没有门（v5.3 §F.4 ⑥⑦）。
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env` 并 `docker compose up -d`，再跑 `make pg-test`。"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        yield connection


def _request(task_id: str) -> GenerationRequest:
    return GenerationRequest(
        task_id=task_id,
        messages=(Message(role="user", content="唤醒沉睡客户"),),
        temperature=0.0,
        max_tokens=128,
    )


def test_retry_audit_is_written_to_pg_and_links_into_the_hash_chain(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """重试审计真落库 → 哈希链可校验 → payload 含 INV-5 三字段。"""
    tenant_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())

    provider = MockModelProvider(
        failure_modes=("timeout",),
        audit_writer=pg_audit_writer(conn),
        tenant_id=tenant_id,
        trace_id=f"trace-{task_id}",
    )
    response = provider.generate(_request(task_id))
    assert response.content

    with conn.cursor() as cursor:
        cursor.execute(
            "select action_type, payload, prev_hash, model_version "
            "from audit_log where tenant_id = %s::uuid",
            (tenant_id,),
        )
        rows = cursor.fetchall()
    assert len(rows) == 1
    action_type, payload, prev_hash, model_version = cast(tuple[Any, ...], rows[0])
    assert action_type == AUDIT_ACTION_RETRY
    assert payload["attempt_number"] == 2
    assert payload["retry_reason"] == "TIMEOUT"
    assert payload["retry_result"] == "SUCCESS"
    assert prev_hash == "0" * 64
    assert model_version == provider.model_version

    with conn.cursor() as cursor:
        cursor.execute(verify_query_sql(f"'{tenant_id}'"))
        description = cursor.description
        assert description is not None
        columns = [desc.name for desc in description]
        chain_rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    verification = verify_chain(chain_rows)
    assert verification.ok, verification.first_break.describe() if verification.first_break else ""
    assert verification.total == 1
