"""T-A1 合成数据生成器的验收用例（纯函数 + 真 PostgreSQL）。

权威依据（每个用例的 docstring 里标注了可反查的条款）：

- **架构 v5.2 §9.4**「合成数据标准集规格」：五类风控样本 / 九宫格 9 层 / 身份匹配三档
  预置样本 / 跨平台 ``ext_key`` 重叠；全部记录标 ``_synthetic: true``。
- **架构 v5.2 §9.5**（裁决 #17）：身份匹配三档判定（高 = 跨平台 ext_key 精确；
  中 = 手机号哈希 / 地址指纹部分匹配；低 = 无匹配特征）。
- **附录D §4 规则 ID 登记表**：五类风控样本各自对应的 ``rule_id``，禁止自造。
- **PRD C1**：明文 PII 一律不落库；**裁决 #15**：授权三字段齐全。
- **T-A1 prd.md §3**：验收标准（各类 ≥10、九宫格全覆盖、幂等、不写 ``_synthetic=false``）。

真 PG 用例标 ``@pytest.mark.pg``（**不得用 SQLite 替代**，见
``.trellis/spec/backend/database-guidelines.md``）；``DATABASE_URL`` 缺失时
``pytest.fail``——可跳过的门等于没有门。
"""

from __future__ import annotations

import os
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from src.data.generator import (
    DEFAULT_NOW,
    DEFAULT_SEED,
    generate_samples,
    layer_of,
    load_thresholds,
    write_samples,
)
from src.data.schemas import (
    CONSENT_STATUS_GRANTED,
    CONSENT_STATUSES_WITHOUT_CONSENT,
    LAYER_NAMES,
    RISK_CATEGORIES,
    RISK_RULE_IDS,
    SAMPLE_CLASS_IDENTITY_LOW,
    SAMPLE_CLASS_NO_CONSENT,
    SAMPLE_CLASS_TOP_VALUE,
)

pytestmark = pytest.mark.unit

#: 测试用租户（与 CLI 默认租户隔离，避免污染演示数据集）。
TEST_TENANT_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
#: 单测用的样本规模（MVP 演示量级 2000）。
SAMPLE_COUNT = 2000

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 纯函数部分（默认集：`-m "not pg"`）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def thresholds() -> Any:
    """治理阈值（唯一来源 = ``governance.yaml``）。"""
    return load_thresholds()


@pytest.fixture(scope="module")
def samples(thresholds: Any) -> list[Any]:
    """一次生成的 2000 条样本（纯函数，不连库）。"""
    return generate_samples(
        count=SAMPLE_COUNT,
        seed=DEFAULT_SEED,
        tenant_id=TEST_TENANT_ID,
        now=DEFAULT_NOW,
        thresholds=thresholds,
    )


def test_generator_is_deterministic() -> None:
    """同 seed + 同输入 → 同输出；不同 seed → 不同输出。

    依据：架构 v5.2 §9.4「可复现的合成数据生成器」；生成器必须纯函数化（不连库、
    不取服务端时钟），否则回归门禁无法复现。
    """
    first = generate_samples(count=200, seed=7, tenant_id=TEST_TENANT_ID, now=DEFAULT_NOW)
    second = generate_samples(count=200, seed=7, tenant_id=TEST_TENANT_ID, now=DEFAULT_NOW)
    assert [s.row.as_db_tuple() for s in first] == [s.row.as_db_tuple() for s in second]
    assert len(first) == 200

    other = generate_samples(count=200, seed=8, tenant_id=TEST_TENANT_ID, now=DEFAULT_NOW)
    assert [s.row.as_db_tuple() for s in other] != [s.row.as_db_tuple() for s in first]


def test_five_risk_categories_each_meet_minimum(samples: list[Any]) -> None:
    """五类风控样本各类 ≥10（T-A1 prd.md §3）。

    依据：架构 v5.2 §9.4「特殊样本（各 ≥50 条）」+ 附录D §4 登记表。
    """
    counts = Counter(s.sample_class for s in samples)
    for category in RISK_CATEGORIES:
        assert counts[category] >= 10, f"{category} 只有 {counts[category]} 条，少于 10 条下限"


def test_risk_samples_trigger_exactly_their_own_rule(samples: list[Any], thresholds: Any) -> None:
    """每条风控样本**恰好**命中自己的 rule_id，不串台（附录D §4）。

    依据：附录D 规则 1a / 1b 的红线条件；T-A1 prd.md §2.2「每类须恰好触发对应的 rule_id」。
    """
    assert samples, "样本为空会让本用例空转——必须先有数据再断言"
    for sample in samples:
        row = sample.row
        hits = set()
        if row.complaint_count_90d >= 1:
            hits.add("SUP_HIGH_COMPLAINT")
        if row.has_refund_dispute or (
            row.refund_rate is not None and row.refund_rate > thresholds.refund_rate_threshold
        ):
            hits.add("SUP_HIGH_REFUND")
        if row.explicit_refusal:
            hits.add("SUP_EXPLICIT_REFUSAL")
        if row.consent_status != CONSENT_STATUS_GRANTED:
            hits.add("NO_CONSENT")
        if row.is_top_value:
            hits.add("SUP_HIGH_VALUE")

        expected = RISK_RULE_IDS.get(sample.sample_class)
        if expected is None:
            assert not hits, f"非风控样本却命中了 {hits}"
        else:
            assert hits == {expected}, f"{sample.sample_class} 命中 {hits}，期望仅 {{{expected}}}"


def test_nine_grid_layers_all_covered(samples: list[Any], thresholds: Any) -> None:
    """九宫格 9 层全覆盖，且层名与行内数据自洽（架构 v5.2 §9.4 分层覆盖）。

    依据：附录D 规则 3 / 规则 4 的 ``layer_score = amount_score × 10 + recency_score``；
    金额档 / 近度档阈值取自 ``governance.yaml`` 的 ``segmentation`` 块。
    """
    layers = Counter(s.layer for s in samples)
    assert set(layers) == set(LAYER_NAMES), f"未覆盖的层：{set(LAYER_NAMES) - set(layers)}"
    for name in LAYER_NAMES:
        assert layers[name] >= 1, f"层 {name} 无样本"

    for sample in samples:
        row = sample.row
        assert row.last_order_at is not None
        days = (DEFAULT_NOW - row.last_order_at).days
        assert sample.layer == layer_of(row.avg_order_value_90d, days, thresholds)


def test_identity_three_tiers_preloaded(samples: list[Any]) -> None:
    """身份匹配三档预置样本各 ≥10（架构 v5.2 §9.4 / §9.5，裁决 #17）。

    - 高置信：跨平台 ``ext_key`` 精确相等 → 自动归集；
    - 中置信：跨平台共享**一个**特征（手机号哈希或地址指纹）→ 人工复核队列（E4）；
    - 低置信：**完全无**匹配特征 → 保留独立身份（PRD P1 红线）。
    """
    high = [s for s in samples if s.identity_tier == "high"]
    medium = [s for s in samples if s.identity_tier == "medium"]
    low = [s for s in samples if s.sample_class == SAMPLE_CLASS_IDENTITY_LOW]
    assert len(high) >= 10, f"高置信样本只有 {len(high)} 条"
    assert len(medium) >= 10, f"中置信样本只有 {len(medium)} 条"
    assert len(low) >= 10, f"低置信样本只有 {len(low)} 条"

    # 高置信：同 ext_key 出现在两个不同平台
    high_by_ext: dict[str, set[str]] = defaultdict(set)
    for sample in high:
        high_by_ext[sample.row.ext_key].add(sample.row.platform)
    assert all(len(platforms) == 2 for platforms in high_by_ext.values())

    # 中置信：跨平台共享且**仅共享一个**特征，ext_key 不同
    by_phone: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for sample in medium:
        if sample.row.phone_hash:
            by_phone[sample.row.phone_hash].add((sample.row.platform, sample.row.ext_key))
    shared_phone = [pairs for pairs in by_phone.values() if len({p for p, _ in pairs}) >= 2]
    assert shared_phone, "中置信样本未跨平台共享手机号哈希"

    for phone_hash, pairs in by_phone.items():
        if len({platform for platform, _ in pairs}) < 2:
            continue
        rows = [s.row for s in medium if s.row.phone_hash == phone_hash]
        assert len({r.platform for r in rows}) == len(rows), "共享手机号哈希的行必须分属不同平台"
        assert len({r.address_fingerprint for r in rows}) == len(rows), "中置信只能共享一个特征"

    # 中置信的 ext_key 不得跨平台相同（否则会被判成高置信 ext_key 精确匹配）
    medium_ext_platforms: dict[str, set[str]] = defaultdict(set)
    for sample in medium:
        medium_ext_platforms[sample.row.ext_key].add(sample.row.platform)
    assert all(len(platforms) == 1 for platforms in medium_ext_platforms.values())

    # 低置信：三个特征列全为空 → 无匹配特征
    for sample in low:
        row = sample.row
        assert row.phone_hash is None
        assert row.email_hash is None
        assert row.address_fingerprint is None


def test_cross_platform_overlap_exists(samples: list[Any]) -> None:
    """跨平台同 ``ext_key`` 重叠 ≥10 组（供 T-B2 冲突消解验收：只放行 1 条）。

    依据：架构 v5.2 §9.4「重叠 2,000（跨平台共享同一 ext_key，用于身份匹配与冲突消解演示）」。
    """
    by_ext: dict[str, set[str]] = defaultdict(set)
    for sample in samples:
        by_ext[sample.row.ext_key].add(sample.row.platform)
    overlap = {ext_key: platforms for ext_key, platforms in by_ext.items() if len(platforms) >= 2}
    assert len(overlap) >= 10, f"跨平台重叠只有 {len(overlap)} 组"


def test_generated_rows_are_all_synthetic(samples: list[Any]) -> None:
    """全部记录标 ``_synthetic = true``，不写任何 ``_synthetic = false`` 的行。

    依据：架构 v5.2 §9.4「全部记录标 ``_synthetic: true``」+ T-A1 prd.md §3。
    """
    assert samples
    for sample in samples:
        assert sample.row._synthetic is True


def test_no_plaintext_pii_is_written(samples: list[Any]) -> None:
    """只写哈希 / 指纹，不写明文 PII（PRD C1）。

    依据：PRD C1 + 数据库纪律「在 ``customer`` 上加明文 PII 列——CI 明文 PII 全表扫描
    结果必须为 0」。
    """
    assert samples, "样本为空会让本用例空转——必须先有数据再断言"
    for sample in samples:
        row = sample.row
        for digest in (row.phone_hash, row.email_hash, row.address_fingerprint):
            if digest is None:
                continue
            assert _HEX64.match(digest), f"疑似明文 PII：{digest!r}"


def test_consent_fields_are_complete(samples: list[Any]) -> None:
    """授权三字段齐全（裁决 #15）；无授权样本落在四种无授权状态里。

    依据：裁决 #15 + 附录D 规则 1b（``NO_CONSENT`` 发送前校验）。
    """
    assert samples, "样本为空会让本用例空转——必须先有数据再断言"
    for sample in samples:
        row = sample.row
        if row.consent_status == CONSENT_STATUS_GRANTED:
            assert row.consent_time is not None
            assert row.consent_source is not None
        if sample.sample_class == SAMPLE_CLASS_NO_CONSENT:
            assert row.consent_status in CONSENT_STATUSES_WITHOUT_CONSENT


def test_thresholds_come_from_governance_yaml(thresholds: Any) -> None:
    """阈值取自 ``governance.yaml``，代码里不另写常量（T-A1 prd.md §2.2 注）。

    依据：``governance.yaml`` 文件头纪律 1「本文件是策略参数的唯一来源」+
    附录D 冻结版规则 1「阈值参数化」。
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    raw_path = os.path.join(repo_root, "governance.yaml")
    import yaml

    with open(raw_path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    assert thresholds.refund_rate_threshold == Decimal(
        str(raw["thresholds"]["refund_rate_threshold"])
    )
    assert thresholds.complaint_window_days == raw["thresholds"]["complaint_window_days"]
    assert (
        thresholds.segmentation.amount_high_min_cents
        == raw["segmentation"]["amount"]["high_min_cents"]
    )
    assert (
        thresholds.segmentation.recency_recent_max_days
        == raw["segmentation"]["recency"]["recent_max_days"]
    )


def test_layer_of_delegates_to_segmentation(thresholds: Any) -> None:
    """拍板⑩：分档唯一实现点是 ``src/unification/segmentation.py``。

    T-A1 与 T-C2 在 Wave 1 Batch 1 **并行**，T-A1 曾自写一份金额/近度分档 → 层名。
    两份实现会随阈值改动漂移，因此本用例锁死 ``layer_of`` 与
    ``segmentation.amount_tier_of`` / ``recency_tier_of`` 输出一致。
    """
    from decimal import Decimal

    from src.unification.segmentation import amount_tier_of, recency_tier_of

    cases = [
        (Decimal("500"), 20),  # amount_high / recency_recent
        (Decimal("40"), 150),  # amount_low / recency_stale
        (Decimal("200"), 60),  # amount_mid / recency_mid
        (Decimal("100"), 30),  # 边界：amount_mid 下界 / recency_recent 上界
        (Decimal("499.99"), 31),
    ]
    for yuan, days in cases:
        cents = int(yuan * 100)
        amount_tier, _ = amount_tier_of(cents, thresholds.segmentation)
        recency_tier, _ = recency_tier_of(days, thresholds.segmentation)
        assert layer_of(yuan, days, thresholds) == f"amount_{amount_tier}_{recency_tier}"


def test_top_value_samples_are_amount_high(samples: list[Any]) -> None:
    """Top 1% 高价值样本的客单价落在金额高档（数据自洽，避免"标记与字段打架"）。

    依据：附录D 规则 1a ``SUP_HIGH_VALUE``（消费金额 Top 1%）+ 评测标注基准 v1.3
    ``known_gap`` 教训（显式抑制与字段推导两条路径口径须自洽）。
    """
    assert samples, "样本为空会让本用例空转——必须先有数据再断言"
    for sample in samples:
        if sample.sample_class == SAMPLE_CLASS_TOP_VALUE:
            assert sample.layer.startswith("amount_high")
            assert sample.row.is_top_value is True


# ---------------------------------------------------------------------------
# 真 PostgreSQL 部分（`-m pg`）
# ---------------------------------------------------------------------------


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.fail(
            "未设置 DATABASE_URL，真 PG 门禁无法执行。"
            "请先 `cp .env.example .env` 并 `docker compose up -d`，再跑 `make pg-test`。"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture
def pg_tenant() -> Iterator[uuid.UUID]:
    """测试租户 + 清理：用例结束后删掉本租户的行，不污染演示数据集。

    依据：``customer`` 表的租户隔离纪律（``.trellis/spec/backend/database-guidelines.md``）。
    """
    with psycopg.connect(_database_url(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM customer WHERE tenant_id = %s", (TEST_TENANT_ID,))
    yield TEST_TENANT_ID
    with psycopg.connect(_database_url(), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM customer WHERE tenant_id = %s", (TEST_TENANT_ID,))


@pytest.mark.pg
def test_generator_persists_requested_count(pg_tenant: uuid.UUID) -> None:
    """生成的行全部落库且条数与 ``--count`` 一致（T-A1 prd.md §3）。

    依据：T-A1 验收命令 ``SELECT count(*) FROM customer WHERE _synthetic=true`` → 2000。
    """
    samples = generate_samples(count=200, seed=DEFAULT_SEED, tenant_id=pg_tenant, now=DEFAULT_NOW)
    write_samples(samples, database_url=_database_url(), tenant_id=pg_tenant)

    with psycopg.connect(_database_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM customer WHERE tenant_id = %s AND _synthetic = true", (pg_tenant,)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 200


@pytest.mark.pg
def test_rerun_is_idempotent(pg_tenant: uuid.UUID) -> None:
    """重复运行不产生重复行，条数不变（T-A1 prd.md §2.1 幂等）。

    依据：T-A1 prd.md §3「重复运行幂等（第二次执行后 count 不变）」。
    """
    first = generate_samples(count=200, seed=DEFAULT_SEED, tenant_id=pg_tenant, now=DEFAULT_NOW)
    write_samples(first, database_url=_database_url(), tenant_id=pg_tenant)
    second = generate_samples(count=200, seed=DEFAULT_SEED, tenant_id=pg_tenant, now=DEFAULT_NOW)
    write_samples(second, database_url=_database_url(), tenant_id=pg_tenant)

    with psycopg.connect(_database_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM customer WHERE tenant_id = %s AND _synthetic = true", (pg_tenant,)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 200


@pytest.mark.pg
def test_generator_writes_no_non_synthetic_rows(pg_tenant: uuid.UUID) -> None:
    """生成器不写任何 ``_synthetic = false`` 的行（T-A1 prd.md §3）。

    依据：架构 v5.2 §9.4「全部记录标 ``_synthetic: true``」。
    """
    samples = generate_samples(count=50, seed=DEFAULT_SEED, tenant_id=pg_tenant, now=DEFAULT_NOW)
    write_samples(samples, database_url=_database_url(), tenant_id=pg_tenant)

    with psycopg.connect(_database_url()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM customer WHERE tenant_id = %s AND _synthetic = false",
            (pg_tenant,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0
