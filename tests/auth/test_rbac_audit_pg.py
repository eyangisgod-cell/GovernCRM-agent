"""T-E5 越权审计 **真 PostgreSQL** 集成验收。

标记 ``pg``：审计写入依赖 ``audit_log`` 的 append-only 触发器与哈希链
（``record_hash`` 由 DB 算，非客户端），不得用 SQLite / 内存库替代。

权威依据：

- **FR-30（PRD v5.2:409）**：越权被拒并审计。
- **红线③**：审计 append-only + 触发器 + 哈希链；写审计唯一入口 = ``insert_statement_sql()``。
- 越权场景 = 运营专员执行合规独占操作（配置抑制规则，PRD 十一章 ops_specialist ❌）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any, cast

import psycopg
import pytest

from src.audit.hash_chain import verify_chain, verify_query_sql
from src.auth.rbac import check_permission, record_denial
from src.auth.roles import Permission, Role
from src.policy_kernel.kernel import Decision

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


def _fetch_chain(
    db_conn: psycopg.Connection[tuple[object, ...]], tenant_id: uuid.UUID
) -> list[dict[str, Any]]:
    with db_conn.cursor() as cur:
        cur.execute(verify_query_sql(f"'{tenant_id}'"))
        description = cur.description
        assert description is not None
        columns = [desc.name for desc in description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def test_ops_specialist_denied_and_audited(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """越权调用（运营专员执行合规独占操作）→ Deny 且 ``audit_log`` 有拒绝记录（FR-30）。

    审计写入走 ``insert_statement_sql()``，``record_hash`` 由 DB 算；链应保持完整（红线③）。
    """
    tenant_id = uuid.uuid4()

    # ① 纯判定：运营专员配置抑制规则 → Deny
    result = check_permission(Role.OPS_SPECIALIST, Permission.CONFIGURE_SUPPRESSION_RULE)
    assert result.conclusion is Decision.DENY

    # ② 审计写入（独立显式函数，不藏在纯判定里）
    audit_id, _record_seq, _record_hash = record_denial(
        conn,
        tenant_id=tenant_id,
        role=Role.OPS_SPECIALIST,
        action=Permission.CONFIGURE_SUPPRESSION_RULE,
        check=result,
        trace_id=str(uuid.uuid4()),
    )

    # ③ audit_log 可查到拒绝记录
    with conn.cursor() as cur:
        cur.execute(
            "select action_type, actor_role, payload, hit_rule_ids "
            "from audit_log where tenant_id = %s and audit_id = %s",
            (tenant_id, audit_id),
        )
        row = cur.fetchone()
    assert row is not None, "越权拒绝应写入 audit_log"
    assert row[0] == "configure_suppression_rule"
    assert row[1] == "ops_specialist"
    payload = cast(dict[str, Any], row[2])
    assert payload["decision"] == "DENY"
    assert payload["error_code"] == "E_RBAC_DENIED"

    # ④ 哈希链完整（证明 record_hash 由 DB 按同一口径算出）
    rows = _fetch_chain(conn, tenant_id)
    verification = verify_chain(rows)
    assert verification.ok, f"越权审计写入后链应完整：{[b.describe() for b in verification.breaks]}"
    assert verification.total == 1


def test_admin_force_release_irreversible_audited(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """管理员对 SUP_HIGH_VALUE 强制放行 → Deny（E_DENY_IRREVERSIBLE）且审计可查。

    架构 v5.3:199 / ADR-044：命中 SUP_* / NO_CONSENT 的强制放行一律拒，拒绝须写审计。
    """
    tenant_id = uuid.uuid4()

    result = check_permission(
        Role.ADMIN,
        Permission.FORCE_RELEASE,
        context={"hit_rule_ids": ["SUP_HIGH_VALUE"]},
    )
    assert result.conclusion is Decision.DENY
    assert result.error_code == "E_DENY_IRREVERSIBLE"

    audit_id, _, _ = record_denial(
        conn,
        tenant_id=tenant_id,
        role=Role.ADMIN,
        action=Permission.FORCE_RELEASE,
        check=result,
        trace_id=str(uuid.uuid4()),
    )

    with conn.cursor() as cur:
        cur.execute(
            "select payload, hit_rule_ids from audit_log where tenant_id = %s and audit_id = %s",
            (tenant_id, audit_id),
        )
        row = cur.fetchone()
    assert row is not None
    payload = cast(dict[str, Any], row[0])
    assert payload["error_code"] == "E_DENY_IRREVERSIBLE"
    assert payload["rule_id"] == "SUP_HIGH_VALUE"
    assert row[1] == ["SUP_HIGH_VALUE"]
