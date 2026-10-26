"""分层结果落库在**真 PostgreSQL** 上的行为验证（T-C2）。

标记 ``pg``：CHECK 约束与落库路径**不得用 SQLite 或内存库代替**
（``.trellis/spec/backend/database-guidelines.md``）。

覆盖 prd.md §3 的验收标准：
- ``platform_local`` 落库**必带** ``platform``（对齐 ``chk_customer_segment_scope_platform``）；
- 全局层与平台局部层两套独立行同时落库，``layer_score`` 与 ``amount_score`` /
  ``recency_score`` 自洽；
- 库侧 CHECK 真实生效（scope 取值、layer_score 公式、platform 必带）。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.unification.segment_store import build_segment_rows, persist_segments
from src.unification.segmentation import (
    SCOPE_GLOBAL,
    SCOPE_PLATFORM_LOCAL,
    CustomerLayerMetrics,
    compute_customer_layers,
    thresholds_from_governance,
)

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[3]
GOVERNANCE_YAML = ROOT / "governance.yaml"


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        # 不用 skip：可跳过的门等于没有门（v5.3 §F.4 ⑥⑦）。
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env` 并 `docker compose up -d`，再跑 `make pg-test`。"
        )
    return url


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    # autocommit：夹具插入的 customer 立即对 SQLAlchemy 连接可见；
    # 且 CHECK 违规的语句各自独立成事务，不会连累后续断言。
    with psycopg.connect(
        _database_url().replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True
    ) as connection:
        yield connection


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(_database_url())
    with Session(engine) as db_session:
        yield db_session
    engine.dispose()


def _insert_customer(conn: psycopg.Connection[tuple[object, ...]]) -> tuple[uuid.UUID, uuid.UUID]:
    tenant_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "insert into customer (customer_id, tenant_id, platform, shop_id, ext_key) "
            "values (%s, %s, 'taobao', 'shop-1', %s) returning customer_id",
            (customer_id, tenant_id, uuid.uuid4().hex),
        )
    return tenant_id, customer_id


def test_persisted_rows_keep_global_and_platform_local_scopes(
    conn: psycopg.Connection[tuple[object, ...]],
    session: Session,
) -> None:
    """全局层 + 平台局部层各落一行；``platform_local`` 行**必带** platform。"""
    tenant_id, customer_id = _insert_customer(conn)
    thresholds = thresholds_from_governance(GOVERNANCE_YAML)
    layers = compute_customer_layers(
        CustomerLayerMetrics(avg_order_value_cents=60000, days_since_last_purchase=10),
        {"taobao": CustomerLayerMetrics(avg_order_value_cents=5000, days_since_last_purchase=200)},
        thresholds,
    )

    rows = persist_segments(
        session,
        tenant_id=tenant_id,
        customer_id=customer_id,
        layers=layers,
        rule_version="governance.yaml@1.0.0",
        effective_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )
    session.commit()
    assert len(rows) == 2

    with conn.cursor() as cur:
        cur.execute(
            "select scope, platform, layer_score, amount_score, recency_score, amount_tier, "
            "recency_tier from customer_segment where tenant_id = %s order by scope",
            (tenant_id,),
        )
        persisted = {cast(str, row[0]): row for row in cur.fetchall()}

    assert set(persisted) == {SCOPE_GLOBAL, SCOPE_PLATFORM_LOCAL}
    assert persisted[SCOPE_GLOBAL][1] is None, "全局层不带 platform"
    assert persisted[SCOPE_PLATFORM_LOCAL][1] == "taobao"
    assert persisted[SCOPE_GLOBAL][2] == 33
    assert persisted[SCOPE_PLATFORM_LOCAL][2] == 11
    for row in persisted.values():
        amount_score = cast(int, row[3])
        recency_score = cast(int, row[4])
        assert cast(int, row[2]) == amount_score * 10 + recency_score, (
            "layer_score 必须与库 CHECK 同口径"
        )


def test_build_segment_rows_never_emits_platform_local_without_platform() -> None:
    """写库前自检：平台局部层缺 platform 一律拒绝（不等库拒收）。"""
    thresholds = thresholds_from_governance(GOVERNANCE_YAML)
    layers = compute_customer_layers(
        CustomerLayerMetrics(avg_order_value_cents=60000, days_since_last_purchase=10),
        {},
        thresholds,
    )
    rows = build_segment_rows(
        tenant_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        layers=layers,
        rule_version="governance.yaml@1.0.0",
        effective_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )
    assert [row.scope for row in rows] == [SCOPE_GLOBAL]
    assert rows[0].platform is None


def test_database_rejects_platform_local_without_platform(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """库侧 CHECK ``chk_customer_segment_scope_platform`` 真实生效。"""
    tenant_id, customer_id = _insert_customer(conn)
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute(
            "insert into customer_segment (tenant_id, customer_id, scope, platform, rule_version, "
            "layer_score, amount_score, recency_score, amount_tier, recency_tier) "
            "values (%s, %s, 'platform_local', null, 'r@1', 11, 1, 1, 'low', 'stale')",
            (tenant_id, customer_id),
        )
    assert "chk_customer_segment_scope_platform" in str(excinfo.value)


def test_database_rejects_inconsistent_layer_score(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """库侧 CHECK ``chk_customer_segment_layer_score`` 真实生效。"""
    tenant_id, customer_id = _insert_customer(conn)
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute(
            "insert into customer_segment (tenant_id, customer_id, scope, rule_version, "
            "layer_score, amount_score, recency_score, amount_tier, recency_tier) "
            "values (%s, %s, 'global', 'r@1', 30, 3, 1, 'high', 'stale')",
            (tenant_id, customer_id),
        )
    assert "chk_customer_segment_layer_score" in str(excinfo.value)


def test_database_rejects_unknown_scope(
    conn: psycopg.Connection[tuple[object, ...]],
) -> None:
    """库侧 CHECK ``chk_customer_segment_scope`` 真实生效。"""
    tenant_id, customer_id = _insert_customer(conn)
    with conn.cursor() as cur, pytest.raises(psycopg.Error) as excinfo:
        cur.execute(
            "insert into customer_segment (tenant_id, customer_id, scope, rule_version, "
            "layer_score, amount_score, recency_score, amount_tier, recency_tier) "
            "values (%s, %s, 'platform', 'r@1', 11, 1, 1, 'low', 'stale')",
            (tenant_id, customer_id),
        )
    assert "chk_customer_segment_scope" in str(excinfo.value)


def test_dimension_values_record_the_segmentation_inputs() -> None:
    """落库行保留分层输入（客单价 / 距今天数 / 层名），便于回放举证。"""
    thresholds = thresholds_from_governance(GOVERNANCE_YAML)
    layers = compute_customer_layers(
        CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20),
        {"douyin": CustomerLayerMetrics(avg_order_value_cents=20000, days_since_last_purchase=20)},
        thresholds,
    )
    rows = build_segment_rows(
        tenant_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        layers=layers,
        rule_version="governance.yaml@1.0.0",
        effective_at=datetime(2026, 10, 23, 12, 0, tzinfo=UTC),
    )
    assert rows[0].dimension_values == {
        "layer_name": "amount_mid_recent",
        "avg_order_value_cents": 20000,
        "days_since_last_purchase": 20,
    }
    assert rows[1].dimension_values["layer_name"] == "amount_mid_recent"
