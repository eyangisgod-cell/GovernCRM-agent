"""哈希链校验脚本：对真 PostgreSQL 里的 audit_log 做全链校验。

用法：
    DATABASE_URL=postgresql://... python scripts/verify_hash_chain.py
    DATABASE_URL=... python scripts/verify_hash_chain.py --tenant-id <uuid>

退出码：0 = 链完整；1 = 链断裂（打印定位到第 N 条）；2 = 环境问题。

「哈希链校验 → 失败即阻断」是红线③的一部分：本脚本是 CI 与运维的挡箭牌。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audit.hash_chain import verify_chain, verify_query_sql


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print(
            "未设置 DATABASE_URL，无法校验哈希链。\n"
            "  请先 `cp .env.example .env` 并 `docker compose up -d`，再跑本脚本。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="audit_log 哈希链校验")
    parser.add_argument("--tenant-id", default=None, help="只校验指定租户（默认全部）")
    args = parser.parse_args()

    import psycopg

    query = verify_query_sql(args.tenant_id)
    with psycopg.connect(_database_url()) as conn, conn.cursor() as cur:
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
