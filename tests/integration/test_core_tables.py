"""核心表在**真 PostgreSQL** 上的行为验证（T0.2）。

标记 `pg`：append-only 触发器 / CHECK 约束 / 幂等迁移**不得用 SQLite 或内存库替代**
（`.trellis/spec/backend/database-guidelines.md`）。

覆盖 prd.md §4 的验收标准：

- 表数 ≥ 15（14 张核心表 + 辅助表）；
- ``audit_log`` 的 UPDATE / DELETE / TRUNCATE **均被触发器拒绝**，报错可读；
- 迁移脚本**幂等**（重复执行不报错）；
- 关键 CHECK 约束真实生效（记忆空间键 / 低置信不合并 / 技能卡双门禁 / rule_id 登记表）；
- ORM 模型与真库表结构**逐列一致**。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import ARRAY, BigInteger, Boolean, Date, DateTime, Integer, Numeric, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from src.models import Base

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_FILE = ROOT / "migrations" / "001_core_tables.sql"

CORE_TABLES: tuple[str, ...] = (
    "customer",
    "customer_identity",
    "customer_segment",
    "action_candidate",
    "decision",
    "audit_log",
    "suppression_record",
    "approval",
    "tool_call",
    "worker",
    "worker_template",
    "worker_metrics",
    "skill_card",
    "cost_record",
)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("未设置 DATABASE_URL，跳过真 PG 用例")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(_database_url(), autocommit=True) as connection:
        yield connection


def _insert_audit_row(conn: psycopg.Connection[tuple[object, ...]]) -> uuid.UUID:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into audit_log (
                tenant_id, trace_id, actor_type, action_type,
                model_version, prompt_version, skill_version, rule_version,
                prev_hash, record_hash
            ) values (
                %s, %s, 'user', 'test_insert',
                'test/model@1', 'git:abc1234', 'git:def5678', 'governance.yaml@1',
                %s, %s
            ) returning audit_id
            """,
            (
                uuid.uuid4(),
                f"trace-{uuid.uuid4()}",
                "0" * 64,
                "a" * 64,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        return uuid.UUID(str(row[0]))


def test_migration_is_applied_and_tables_exist(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' and table_type = 'BASE TABLE'"
        )
        tables = {row[0] for row in cur.fetchall()}
    assert len(tables) >= 15, f"表数不足 15：{len(tables)}"
    for table_name in CORE_TABLES:
        assert table_name in tables, f"缺少核心表 {table_name}"


def test_migration_is_idempotent() -> None:
    """重复执行迁移脚本不得报错。"""
    sql = MIGRATION_FILE.read_text(encoding="utf-8")
    with (
        psycopg.connect(_database_url(), autocommit=True) as connection,
        connection.cursor() as cur,
    ):
        cur.execute(sql)
        cur.execute(sql)


def test_audit_log_rejects_update(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    audit_id = _insert_audit_row(conn)
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute(
            "update audit_log set action_type = 'tampered' where audit_id = %s",
            (audit_id,),
        )
    message = str(excinfo.value)
    assert "append-only" in message, f"报错信息不可读：{message}"
    assert "UPDATE" in message


def test_audit_log_rejects_delete(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    audit_id = _insert_audit_row(conn)
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute("delete from audit_log where audit_id = %s", (audit_id,))
    message = str(excinfo.value)
    assert "append-only" in message
    assert "DELETE" in message


def test_audit_log_rejects_truncate(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute("truncate audit_log")
    assert "append-only" in str(excinfo.value)


def test_audit_log_keeps_record_after_failed_update(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """被拒绝的 UPDATE 不得留下任何痕迹（行内容保持原样）。"""
    audit_id = _insert_audit_row(conn)
    with conn.cursor() as cur:
        with pytest.raises(psycopg.Error):
            cur.execute(
                "update audit_log set action_type = 'tampered' where audit_id = %s",
                (audit_id,),
            )
        cur.execute("select action_type from audit_log where audit_id = %s", (audit_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "test_insert"


def test_worker_memory_scope_key_check_is_enforced(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """``memory_scope_key`` 必须等于 ``{tenant_id}:{worker_id}``。"""
    tenant_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute(
            "insert into worker (worker_id, tenant_id, name, role_definition, "
            "autonomy_level, memory_scope_key, status, created_by) "
            "values (%s, %s, 'bad-scope', '{}'::jsonb, 'L0', 'wrong-key', 'draft', %s)",
            (worker_id, tenant_id, uuid.uuid4()),
        )
    assert "chk_worker_memory_scope_key" in str(excinfo.value)


def test_worker_accepts_correct_memory_scope_key(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """正例：按契约拼出 ``{tenant_id}:{worker_id}`` 的员工可正常入库。"""
    tenant_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "insert into worker (worker_id, tenant_id, name, role_definition, "
            "autonomy_level, memory_scope_key, status, created_by) "
            "values (%s, %s, '沉睡唤醒专员-测试', '{}'::jsonb, 'L1', %s, 'draft', %s) "
            "returning memory_scope_key",
            (worker_id, tenant_id, f"{tenant_id}:{worker_id}", uuid.uuid4()),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == f"{tenant_id}:{worker_id}"


def test_low_confidence_identity_cannot_point_to_customer(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """低置信身份不得强行合并（PRD P1 红线）。"""
    tenant_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "insert into customer (tenant_id, platform, shop_id, ext_key) "
            "values (%s, 'p1', 's1', %s) returning customer_id",
            (tenant_id, uuid.uuid4().hex),
        )
        row = cur.fetchone()
        assert row is not None
        customer_id = row[0]
        with pytest.raises(psycopg.Error) as excinfo:
            cur.execute(
                "insert into customer_identity (tenant_id, customer_id, platform, shop_id, "
                "ext_key, match_basis, confidence_tier) "
                "values (%s, %s, 'p2', 's1', %s, 'no_feature', 'low')",
                (tenant_id, customer_id, uuid.uuid4().hex),
            )
    assert "chk_customer_identity_low_not_merged" in str(excinfo.value)


def test_skill_card_publish_requires_double_gate(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """技能卡发布需回归评测通过 + 人工确认（双门禁）。"""
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute(
            "insert into skill_card (tenant_id, skill_code, name, status, skill_content, "
            "created_by) values (%s, 'S_TEST', 'test', 'published', '{}'::jsonb, %s)",
            (uuid.uuid4(), uuid.uuid4()),
        )
    assert "chk_skill_card_publish_gates" in str(excinfo.value)


def test_unregistered_rule_id_is_rejected(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """禁止自造 rule_id：登记表之外的取值必须被 CHECK 拒绝。"""
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute(
            "insert into decision (tenant_id, conclusion, hit_rule_ids, model_version, "
            "prompt_version, skill_version, rule_version) "
            "values (%s, 'DENY', ARRAY['MY_OWN_RULE'], 'm@1', 'p@1', 's@1', 'r@1')",
            (uuid.uuid4(),),
        )
    assert "chk_decision_hit_rule_ids" in str(excinfo.value)


def test_worker_strategy_binding_cannot_override_redline(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """策略包绑定不得覆盖抑制红线 / NO_CONSENT（写入期即拒绝）。"""
    tenant_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute(
            "insert into worker (worker_id, tenant_id, name, role_definition, "
            "autonomy_level, strategy_binding, memory_scope_key, status, created_by) "
            "values (%s, %s, 'bad-override', '{}'::jsonb, 'L0', "
            "'{\"no_consent\": {\"enabled\": false}}'::jsonb, %s, 'draft', %s)",
            (worker_id, tenant_id, f"{tenant_id}:{worker_id}", uuid.uuid4()),
        )
    assert "chk_worker_strategy_binding_no_redline" in str(excinfo.value)


def _orm_pg_type(column: object) -> str:
    """把 ORM 列类型折算成 PostgreSQL 的 ``udt_name``（用于逐列比对）。"""
    type_ = column.type  # type: ignore[attr-defined]
    if isinstance(type_, JSONB):
        return "jsonb"
    if isinstance(type_, Uuid):
        return "uuid"
    if isinstance(type_, DateTime):
        return "timestamp with time zone" if type_.timezone else "timestamp without time zone"
    if isinstance(type_, Date):
        return "date"
    if isinstance(type_, Boolean):
        return "boolean"
    if isinstance(type_, BigInteger):
        return "bigint"
    if isinstance(type_, Integer):
        return "integer"
    if isinstance(type_, Numeric):
        return "numeric"
    if isinstance(type_, ARRAY):
        return "text[]"
    if isinstance(type_, Text):
        return "text"
    return str(type_)


def test_orm_models_match_live_database_columns(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """模型与真库表结构逐列一致（类名/列名/列类型）。"""
    with conn.cursor() as cur:
        cur.execute(
            "select table_name, column_name, data_type, udt_name "
            "from information_schema.columns where table_schema = 'public'"
        )
        live: dict[tuple[str, str], str] = {}
        for row in cur.fetchall():
            table_name = str(row[0])
            column_name = str(row[1])
            data_type = str(row[2])
            udt_name = str(row[3])
            live[(table_name, column_name)] = udt_name if data_type == "ARRAY" else data_type

    for table_name, table in Base.metadata.tables.items():
        model_columns = {column.name for column in table.columns}
        live_columns = {name for (t, name) in live if t == table_name}
        assert model_columns == live_columns, (
            f"表 {table_name} 列不一致：模型多 {sorted(model_columns - live_columns)}，"
            f"库多 {sorted(live_columns - model_columns)}"
        )
        for column in table.columns:
            expected = _orm_pg_type(column)
            actual = live[(table_name, column.name)]
            if expected.endswith("[]") and actual == "_text":
                actual = "text[]"
            assert expected == actual, (
                f"{table_name}.{column.name} 类型不一致：模型 {expected} / 库 {actual}"
            )


def test_orm_constraint_and_index_names_match_live_database(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """约束与索引**同名同义**：模型里写的名字必须就是库里的名字。

    这是「模型 ↔ 表结构一一对应」的最后一环：列名、列类型、约束名、索引名四处对齐，
    杜绝「模型改了、迁移没改」这种双真相来源。
    """
    with conn.cursor() as cur:
        cur.execute(
            "select conrelid::regclass::text, conname from pg_constraint "
            "where connamespace = 'public'::regnamespace and contype in ('c', 'u')"
        )
        live_constraints: dict[str, set[str]] = {}
        for row in cur.fetchall():
            live_constraints.setdefault(str(row[0]), set()).add(str(row[1]))
        cur.execute("select tablename, indexname from pg_indexes where schemaname = 'public'")
        live_indexes: dict[str, set[str]] = {}
        for row in cur.fetchall():
            live_indexes.setdefault(str(row[0]), set()).add(str(row[1]))

    for table_name, table in Base.metadata.tables.items():
        model_constraints: set[str] = set()
        for constraint in table.constraints:
            constraint_name = constraint.name
            if isinstance(constraint_name, str) and constraint_name.startswith(("chk_", "uq_")):
                model_constraints.add(constraint_name)
        model_indexes: set[str] = set()
        for index in table.indexes:
            index_name = index.name
            if isinstance(index_name, str):
                model_indexes.add(index_name)

        db_constraints = {
            name
            for name in live_constraints.get(table_name, set())
            if name.startswith(("chk_", "uq_"))
        }
        db_indexes = live_indexes.get(table_name, set())

        assert model_constraints == db_constraints, (
            f"表 {table_name} 约束不一致：模型多 {sorted(model_constraints - db_constraints)}，"
            f"库多 {sorted(db_constraints - model_constraints)}"
        )
        assert model_indexes <= db_indexes, (
            f"表 {table_name} 索引缺失：{sorted(model_indexes - db_indexes)}"
        )
