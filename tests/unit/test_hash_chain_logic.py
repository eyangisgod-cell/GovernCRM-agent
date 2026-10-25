"""哈希链纯逻辑单测（不连库）。

覆盖：
- `verify_chain` 的三种断裂判定（创世 / 链接 / 内容）；
- 「定位到第 N 条」的序号语义；
- `HASHED_FIELDS` 与 `ENVELOPE_FIELDS` 的分工（信封列不得进哈希，业务列不得漏）。
"""

from __future__ import annotations

import dataclasses

import pytest

from src.audit.hash_chain import (
    ENVELOPE_FIELDS,
    GENESIS_HASH,
    HASHED_FIELDS,
    ChainVerification,
    canonical_sql,
    expected_hash_sql,
    insert_statement_sql,
    verify_chain,
)

pytestmark = pytest.mark.unit


def _row(
    tenant: str,
    seq: int,
    audit_id: str,
    prev_hash: str,
    record_hash: str,
    expected_hash: str | None = None,
) -> dict[str, object]:
    return {
        "tenant_id": tenant,
        "record_seq": seq,
        "audit_id": audit_id,
        "prev_hash": prev_hash,
        "record_hash": record_hash,
        "expected_hash": expected_hash if expected_hash is not None else record_hash,
    }


def test_intact_chain_passes() -> None:
    rows = [
        _row("t1", 1, "a1", GENESIS_HASH, "h1"),
        _row("t1", 2, "a2", "h1", "h2"),
        _row("t1", 3, "a3", "h2", "h3"),
    ]
    result = verify_chain(rows)
    assert result.ok
    assert result.total == 3
    assert result.breaks == ()


def test_genesis_violation_is_detected() -> None:
    rows = [_row("t1", 1, "a1", "f" * 64, "h1")]
    result = verify_chain(rows)
    assert not result.ok
    first = result.first_break
    assert first is not None
    assert first.index == 1
    assert "创世" in first.reason


def test_link_break_is_detected_and_located() -> None:
    """改一条记录的 hash → 下一条的链接对不上，且能定位到第几条。"""
    rows = [
        _row("t1", 1, "a1", GENESIS_HASH, "h1"),
        _row("t1", 2, "a2", "h1", "h2"),
        _row("t1", 3, "a3", "TAMPERED", "h3"),
        _row("t1", 4, "a4", "h3", "h4"),
    ]
    result = verify_chain(rows)
    assert not result.ok
    first = result.first_break
    assert first is not None
    assert first.index == 3
    assert first.audit_id == "a3"
    assert "链接断裂" in first.reason
    assert first.expected == "h2"
    assert first.actual == "TAMPERED"


def test_content_break_is_detected() -> None:
    """内容被改但链接仍自洽——靠重算哈希抓出来。"""
    rows = [
        _row("t1", 1, "a1", GENESIS_HASH, "h1"),
        _row("t1", 2, "a2", "h1", "h2", expected_hash="computed-h2"),
    ]
    result = verify_chain(rows)
    assert not result.ok
    first = result.first_break
    assert first is not None
    assert first.index == 2
    assert "内容哈希不符" in first.reason


def test_tenants_are_chained_independently() -> None:
    """每个租户一条独立链：甲租户的断裂不影响乙租户。"""
    rows = [
        _row("t1", 1, "a1", GENESIS_HASH, "h1"),
        _row("t2", 1, "b1", GENESIS_HASH, "g1"),
        _row("t2", 2, "b2", "g1", "g2"),
    ]
    result = verify_chain(rows)
    assert result.ok
    assert result.total == 3


def test_hashed_and_envelope_fields_partition_audit_columns() -> None:
    """业务列进哈希、信封列不进——两个集合不得重叠，且必须覆盖全部列。"""
    assert not (set(HASHED_FIELDS) & ENVELOPE_FIELDS)
    assert "prev_hash" in ENVELOPE_FIELDS
    assert "record_hash" in ENVELOPE_FIELDS
    assert "record_seq" in ENVELOPE_FIELDS
    assert "tenant_id" in HASHED_FIELDS
    assert "action_type" in HASHED_FIELDS


def test_canonical_sql_covers_exactly_the_hashed_fields() -> None:
    sql = canonical_sql("a")
    for name in HASHED_FIELDS:
        assert f"'{name}', a.{name}" in sql
    for name in ENVELOPE_FIELDS:
        assert f"'{name}', a.{name}" not in sql


def test_expected_hash_sql_is_sha256_of_prev_plus_canonical() -> None:
    sql = expected_hash_sql("a")
    assert "sha256(" in sql
    assert "a.prev_hash ||" in sql
    assert canonical_sql("a") in sql


def test_insert_statement_does_not_accept_client_supplied_hash() -> None:
    """record_hash 必须由数据库算——客户端传的值不算数。"""
    sql = insert_statement_sql()
    assert "record_hash" in sql
    assert "expected_hash" not in sql
    assert "s.prev_hash" in sql


def test_verification_result_is_immutable() -> None:
    result = ChainVerification(total=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.total = 1  # type: ignore[misc]
