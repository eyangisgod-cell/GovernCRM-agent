"""审计哈希链：规范化口径 + 链校验（红线③的可验证形态）。

哈希链定义（`migrations/001_core_tables.sql` 注释口径）：
    record_hash = sha256(prev_hash || 规范化内容)
    prev_hash   = 同租户上一条记录的 record_hash；该租户首条为 64 个 '0'（创世值）

**规范化内容由 PostgreSQL 侧生成**（`jsonb_build_object(...)::text`），
写入方与校验方共用本模块的同一段 SQL 表达式——这是「字段规范化不一致 → 校验永远失败」
的唯一解法：口径只有一份，且由数据库统一渲染。

本模块只产出 SQL 片段与纯函数，**模块级不 import psycopg、不连库**；取数与执行在
`scripts/verify_hash_chain.py`、调用方，以及本模块的运维入口
`python -m src.audit.hash_chain --verify`（psycopg 仅在 `main()` 内惰性 import，
`record_hash` 一律由数据库算——红线③）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ENVELOPE_FIELDS",
    "GENESIS_HASH",
    "HASHED_FIELDS",
    "HASHED_FIELD_TYPES",
    "ChainBreak",
    "ChainVerification",
    "canonical_sql",
    "expected_hash_sql",
    "insert_statement_sql",
    "main",
    "verify_chain",
    "verify_query_sql",
]

#: 创世哈希：每个租户链的第一条记录用它作 prev_hash。
GENESIS_HASH = "0" * 64

#: 参与哈希的列（业务列全集）。**新增业务列必须同时加进这里**——
#: `tests/unit/test_hash_chain.py` 会比对真库列清单，漏加即红。
HASHED_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "trace_id",
    "task_id",
    "worker_id",
    "decision_id",
    "approval_id",
    "actor_type",
    "actor_id",
    "actor_role",
    "action_type",
    "object_type",
    "object_id",
    "hit_rule_ids",
    "model_version",
    "prompt_version",
    "skill_version",
    "rule_version",
    "input_snapshot",
    "evidence",
    "payload",
    "hash_algo",
)

#: 不参与哈希的信封列：数据库在写入时生成或派生，调用方算不出它们的最终值。
#: `record_seq` 决定链的遍历顺序，`audit_id` 是主键，`created_at` 是入库时刻。
ENVELOPE_FIELDS: frozenset[str] = frozenset(
    {"audit_id", "record_seq", "created_at", "prev_hash", "record_hash"}
)

#: 各业务列的 PostgreSQL 类型（写入时按此做显式转型，避免类型推断漂移）。
HASHED_FIELD_TYPES: dict[str, str] = {
    "tenant_id": "uuid",
    "trace_id": "text",
    "task_id": "uuid",
    "worker_id": "uuid",
    "decision_id": "uuid",
    "approval_id": "uuid",
    "actor_type": "text",
    "actor_id": "text",
    "actor_role": "text",
    "action_type": "text",
    "object_type": "text",
    "object_id": "text",
    "hit_rule_ids": "text[]",
    "model_version": "text",
    "prompt_version": "text",
    "skill_version": "text",
    "rule_version": "text",
    "input_snapshot": "jsonb",
    "evidence": "jsonb",
    "payload": "jsonb",
    "hash_algo": "text",
}


def canonical_sql(alias: str) -> str:
    """生成规范化内容的 SQL 表达式。

    `jsonb_build_object` 的键序由 jsonb 自身规范化（按长度、再按字节序），
    同一个键集合恒定产出同一段文本——这正是「写入方与校验方口径一致」的根据。
    """
    pairs = ", ".join(f"'{name}', {alias}.{name}" for name in HASHED_FIELDS)
    return f"jsonb_build_object({pairs})::text"


def expected_hash_sql(alias: str) -> str:
    """生成「本条记录应有的 record_hash」的 SQL 表达式。"""
    canonical = canonical_sql(alias)
    return f"encode(sha256(convert_to({alias}.prev_hash || {canonical}, 'UTF8')), 'hex')"


def verify_query_sql(tenant_id: str | None = None) -> str:
    """生成取数 + 逐行算出期望哈希的查询。"""
    where = ""
    if tenant_id is not None:
        where = f"where tenant_id = {tenant_id}"
    return (
        "select audit_id, tenant_id, record_seq, prev_hash, record_hash, "
        f"{expected_hash_sql('a')} as expected_hash "
        f"from audit_log a {where} order by tenant_id, record_seq"
    )


def insert_statement_sql() -> str:
    """生成写入一条审计记录的 INSERT 语句。

    `record_hash` 由数据库按同一口径算出（`expected_hash_sql`），调用方**不传**——
    客户端算出来的哈希没有意义，只有数据库渲染出来的才是链上那份。

    占位符用 psycopg 的 `%s` 风格，顺序 = HASHED_FIELDS 顺序 + prev_hash。
    """
    columns = ", ".join((*HASHED_FIELDS, "prev_hash", "record_hash"))
    selects = ", ".join(f"s.{name}" for name in HASHED_FIELDS)
    placeholders = ", ".join(f"%s::{HASHED_FIELD_TYPES[name]} as {name}" for name in HASHED_FIELDS)
    return (
        f"insert into audit_log ({columns}) "
        f"select {selects}, s.prev_hash, {expected_hash_sql('s')} "
        f"from (select {placeholders}, %s::text as prev_hash) s "
        "returning audit_id, record_seq, record_hash"
    )


@dataclass(frozen=True)
class ChainBreak:
    """链上的一处断裂。"""

    tenant_id: str
    #: 该断裂在**租户链内**的序号（1 起），即「第 N 条」。
    index: int
    record_seq: int
    audit_id: str
    reason: str
    expected: str
    actual: str

    def describe(self) -> str:
        return (
            f"租户 {self.tenant_id} 的第 {self.index} 条记录（record_seq={self.record_seq}, "
            f"audit_id={self.audit_id}）{self.reason}：期望 {self.expected}，实际 {self.actual}"
        )


@dataclass(frozen=True)
class ChainVerification:
    """全链校验结果。"""

    total: int
    breaks: tuple[ChainBreak, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.breaks

    @property
    def first_break(self) -> ChainBreak | None:
        return self.breaks[0] if self.breaks else None


def verify_chain(rows: Iterable[Mapping[str, Any]]) -> ChainVerification:
    """校验哈希链。

    逐条检查三件事：
      1. 创世——该租户链首条的 prev_hash 必须是 64 个 '0'；
      2. 链接——本条 prev_hash 等于同租户上一条的 record_hash；
      3. 内容——本条 record_hash 等于按同一口径重算出的期望哈希。

    `rows` 需含 `tenant_id` / `record_seq` / `audit_id` / `prev_hash` /
    `record_hash` / `expected_hash` 六个键（`verify_query_sql` 直接产出这个形状）。
    """
    ordered = sorted(rows, key=lambda row: (str(row["tenant_id"]), int(row["record_seq"])))

    breaks: list[ChainBreak] = []
    prev_by_tenant: dict[str, tuple[int, str, str]] = {}
    index_by_tenant: dict[str, int] = {}

    for row in ordered:
        tenant = str(row["tenant_id"])
        record_seq = int(row["record_seq"])
        audit_id = str(row["audit_id"])
        prev_hash = str(row["prev_hash"])
        record_hash = str(row["record_hash"])
        expected_hash = str(row["expected_hash"])

        index = index_by_tenant.get(tenant, 0) + 1
        index_by_tenant[tenant] = index

        if tenant not in prev_by_tenant:
            if prev_hash != GENESIS_HASH:
                breaks.append(
                    ChainBreak(
                        tenant_id=tenant,
                        index=index,
                        record_seq=record_seq,
                        audit_id=audit_id,
                        reason="不是创世记录（首条记录的 prev_hash 必须是 64 个 '0'）",
                        expected=GENESIS_HASH,
                        actual=prev_hash,
                    )
                )
        else:
            prev_seq, prev_audit, prev_record_hash = prev_by_tenant[tenant]
            if prev_hash != prev_record_hash:
                breaks.append(
                    ChainBreak(
                        tenant_id=tenant,
                        index=index,
                        record_seq=record_seq,
                        audit_id=audit_id,
                        reason=(
                            f"链接断裂（上一条 record_seq={prev_seq}, audit_id={prev_audit} "
                            f"的 record_hash 对不上）"
                        ),
                        expected=prev_record_hash,
                        actual=prev_hash,
                    )
                )

        if record_hash != expected_hash:
            breaks.append(
                ChainBreak(
                    tenant_id=tenant,
                    index=index,
                    record_seq=record_seq,
                    audit_id=audit_id,
                    reason="内容哈希不符（记录被改过，或写入时口径与校验时不一致）",
                    expected=expected_hash,
                    actual=record_hash,
                )
            )

        prev_by_tenant[tenant] = (record_seq, audit_id, record_hash)

    return ChainVerification(total=len(ordered), breaks=tuple(breaks))


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：`python -m src.audit.hash_chain --verify`。

    只接线现有的 `verify_query_sql` + `verify_chain`，不重写哈希逻辑；
    `record_hash` 由数据库算（红线③），本函数不自算哈希写库。

    退出码：0 = 链完整；1 = 链断裂（stderr 输出定位到第 N 条：tenant_id +
    record_seq + audit_id + 原因）；2 = 环境问题（缺 `DATABASE_URL` / 非法参数）。
    """
    import argparse
    import os
    import sys
    import uuid as uuid_module

    parser = argparse.ArgumentParser(
        prog="python -m src.audit.hash_chain",
        description="audit_log 哈希链全链校验（红线③的运维/验收入口）",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="全链校验（默认行为；显式声明供 make verify-chain / 门禁调用）",
    )
    parser.add_argument("--tenant-id", default=None, help="只校验指定租户（默认全部）")
    args = parser.parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        print(
            "未设置 DATABASE_URL，无法校验哈希链。\n"
            "  请先 `cp .env.example .env` 并 `docker compose up -d`，"
            "或经 `make verify-chain` 同源的 .env。",
            file=sys.stderr,
        )
        return 2
    url = url.replace("postgresql+psycopg://", "postgresql://", 1)

    tenant_id = args.tenant_id
    if tenant_id is not None:
        try:
            uuid_module.UUID(tenant_id)
        except ValueError as exc:
            print(f"--tenant-id 不是合法 UUID：{tenant_id}（{exc}）", file=sys.stderr)
            return 2
        # verify_query_sql 的租户过滤是裸插值，由调用方负责加引号（与测试口径一致）
        query = verify_query_sql(f"'{tenant_id}'")
    else:
        query = verify_query_sql()

    import psycopg

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(query)
        description = cur.description
        assert description is not None
        columns = [desc.name for desc in description]
        rows = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]

    result = verify_chain(rows)
    if result.ok:
        print(f"[hash-chain] 校验通过：{result.total} 条记录，链完整。")
        return 0

    print(
        f"[hash-chain] 校验失败：{result.total} 条记录，发现 {len(result.breaks)} 处断裂。",
        file=sys.stderr,
    )
    for item in result.breaks:
        print(f"  - {item.describe()}", file=sys.stderr)
    first = result.first_break
    if first is not None:
        print(
            f"\n首处断裂定位：租户 {first.tenant_id} 的第 {first.index} 条记录。",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
