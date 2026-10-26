"""`python -m src.audit.hash_chain --verify` 的端到端验收（红线③的运维入口）。

验收三步（T-E3 PRD）：
  1. 插入 ≥2 条真实记录 → `--verify` 链完好 → 退出 0；
  2. `UPDATE audit_log` → append-only 触发器拒绝（报错含 "append-only"）；
  3. 禁用触发器篡改一条 `prev_hash` → `--verify` 非 0 退出且定位到第 N 条
     （tenant_id + record_seq + audit_id + 原因）。

标记 `pg`：依赖真 PostgreSQL 的触发器与 jsonb/sha256 口径，**不得 SQLite 替代**
（`.trellis/spec/backend/database-guidelines.md`）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from src.audit.hash_chain import GENESIS_HASH, insert_statement_sql

pytestmark = pytest.mark.pg

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    connection: psycopg.Connection[tuple[object, ...]],
    tenant_id: uuid.UUID,
    prev_hash: str,
    trace_id: str,
) -> tuple[str, str]:
    """按链口径写入一条审计记录，返回 (audit_id, record_hash)。record_hash 由 DB 算。"""
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
        "chain_cli_test",  # action_type
        "customer",  # object_type
        str(uuid.uuid4()),  # object_id
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
    with connection.cursor() as cur:
        cur.execute(insert_statement_sql(), values)
        row = cur.fetchone()
    assert row is not None
    return str(row[0]), str(row[2])


def _run_cli(*args: str) -> tuple[int, str]:
    """子进程调用 `python -m src.audit.hash_chain ...`，返回 (returncode, 合并输出)。

    用 bytes 捕获再按 UTF-8 解码：Windows 控制台编码不定，text 模式会乱码甚至炸。
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url()
    env["PYTHONUTF8"] = "1"  # 子进程 stdout/stderr 恒为 UTF-8，断言不依赖控制台代码页
    completed = subprocess.run(
        [sys.executable, "-m", "src.audit.hash_chain", *args],
        capture_output=True,
        cwd=PROJECT_ROOT,
        env=env,
        timeout=120,
        check=False,
    )
    output = completed.stdout.decode("utf-8", errors="replace") + completed.stderr.decode(
        "utf-8", errors="replace"
    )
    return completed.returncode, output


def test_cli_verify_exits_zero_on_intact_chain(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """验收①：插入 ≥2 条真实记录后，`--verify` 链完好 → 退出 0。"""
    tenant_id = uuid.uuid4()
    prev = GENESIS_HASH
    for index in range(2):
        _, prev = _insert_chain_row(conn, tenant_id, prev, f"cli-ok-{index}")

    returncode, output = _run_cli("--verify")
    assert returncode == 0, f"--verify 应退出 0，实际 {returncode}：\n{output}"
    assert "链完整" in output, f"输出应宣告链完整：\n{output}"

    # 带 --tenant-id 的定向校验同样必须绿（CLI 接线 verify_query_sql 的租户过滤）
    scoped_code, scoped_out = _run_cli("--verify", "--tenant-id", str(tenant_id))
    assert scoped_code == 0, f"定向 --verify 应退出 0，实际 {scoped_code}：\n{scoped_out}"


def test_update_rejected_then_cli_locates_tampered_prev_hash() -> None:
    """验收②③：UPDATE 被触发器拒绝 → 篡改 prev_hash → CLI 定位到第 N 条。

    篡改通道：禁用 append-only 触发器模拟 DBA 级攻击（绕过应用层直接改库），
    这正是哈希链要防的场景。若环境无权限禁用触发器 → `pytest.fail`，不许 skip。
    篡改必须落库（CLI 子进程是独立连接，看不到未提交数据），finally 里恢复并
    重新启用触发器——审计 append-only，落库的破坏不清理会把整条链永久弄脏。
    """
    url = _database_url()
    tenant_id = uuid.uuid4()

    with psycopg.connect(url) as connection:
        prev = GENESIS_HASH
        for index in range(3):
            _, prev = _insert_chain_row(connection, tenant_id, prev, f"cli-tamper-{index}")
        # 先落库：下面被触发器拒绝的 UPDATE 会把事务回滚掉，不 commit 连插入一起没
        connection.commit()

        with connection.cursor() as cur:
            cur.execute(
                "select record_seq, audit_id, prev_hash from audit_log "
                "where tenant_id = %s order by record_seq",
                (tenant_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 3, f"应有 3 条真实记录，实际 {len(rows)} 条"
        second_seq, second_audit_id, second_prev = (
            int(rows[1][0]),
            str(rows[1][1]),
            str(rows[1][2]),
        )

        # ② 普通 UPDATE 必须被 append-only 触发器拒绝
        with connection.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
            cur.execute(
                "update audit_log set prev_hash = %s where tenant_id = %s and record_seq = %s",
                ("f" * 64, tenant_id, second_seq),
            )
        message = str(excinfo.value)
        assert "append-only" in message, f"触发器拒绝信息不可读：{message}"
        connection.rollback()

        trigger_disabled = False
        try:
            # ③ 禁用触发器篡改第 2 条的 prev_hash（DBA 级攻击）
            with connection.cursor() as cur:
                try:
                    cur.execute("alter table audit_log disable trigger trg_audit_log_append_only")
                except psycopg.Error as exc:
                    pytest.fail(
                        f"环境无权限禁用 append-only 触发器，篡改通道无法验收（不许 skip）：{exc}"
                    )
                trigger_disabled = True
                cur.execute(
                    "update audit_log set prev_hash = %s where tenant_id = %s and record_seq = %s",
                    ("f" * 64, tenant_id, second_seq),
                )
                assert cur.rowcount == 1, f"篡改应命中 1 行，实际 {cur.rowcount}"
            connection.commit()  # CLI 子进程须看到篡改后的状态

            # CLI：全链 --verify 非 0 退出，且定位到第 2 条（tenant + seq + audit_id + 原因）
            returncode, output = _run_cli("--verify")
            assert returncode != 0, f"链断裂时 --verify 必须非 0 退出：\n{output}"
            assert "链完整" not in output, f"断裂链不得宣告完整：\n{output}"

            scoped_code, scoped_out = _run_cli("--verify", "--tenant-id", str(tenant_id))
            assert scoped_code != 0, f"定向 --verify 应非 0 退出：\n{scoped_out}"
            assert str(tenant_id) in scoped_out, f"输出应含 tenant_id：\n{scoped_out}"
            assert "第 2 条" in scoped_out, f"输出应定位到第 2 条：\n{scoped_out}"
            assert str(second_seq) in scoped_out, f"输出应含 record_seq：\n{scoped_out}"
            assert str(second_audit_id) in scoped_out, f"输出应含 audit_id：\n{scoped_out}"
            assert "链接断裂" in scoped_out or "内容哈希不符" in scoped_out, (
                f"输出应含断裂原因：\n{scoped_out}"
            )
        finally:
            with connection.cursor() as cur:
                if trigger_disabled:
                    # 恢复原值并重新启用触发器（否则后续用例的 append-only 断言会假绿/假红）
                    cur.execute(
                        "update audit_log set prev_hash = %s "
                        "where tenant_id = %s and record_seq = %s",
                        (second_prev, tenant_id, second_seq),
                    )
                    cur.execute("alter table audit_log enable trigger trg_audit_log_append_only")
            connection.commit()

        # 恢复后链必须重新完整（CLI 退出 0）
        restored_code, restored_out = _run_cli("--verify", "--tenant-id", str(tenant_id))
        assert restored_code == 0, f"恢复后链应完整：\n{restored_out}"
