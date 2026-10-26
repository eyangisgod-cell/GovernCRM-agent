"""合成数据生成器（T-A1 · Wave 1【A】路）。

交付目标（架构 v5.2 §9.4）：**可复现**的合成客户数据集，让抑制 / 合规 / 频控 / 冲突 /
身份匹配每条规则都有可演示、可验收的样本；全部记录标 ``_synthetic: true``。

设计（两层）：

- **纯函数层** ``generate_samples``：不连库、不取服务端时钟、不用全局随机源——
  同 ``(count, seed, tenant_id, now)`` 必得同一批行（可单测、可复现）。
- **薄壳层** ``write_samples``：只负责把纯函数产出的行写进真 PostgreSQL（``psycopg``），
  **不含任何判定逻辑**（判定逻辑属 T-B2 的 ``src/policy_kernel/rules.py``）。

阈值来源：``governance.yaml`` 是策略参数的**唯一来源**（该文件头纪律 1），
本模块的 ``load_thresholds`` 只读取、不另写常量；九宫格分层阈值同样取自
``governance.yaml`` 的 ``segmentation`` 块。

幂等口径（T-A1 prd.md §2.1 允许自选，**此处明确写明**）：

1. **upsert**：``INSERT ... ON CONFLICT (platform, shop_id, ext_key) DO UPDATE``——
   ``customer`` 的唯一键是 ``(platform, shop_id, ext_key)``（**不含 tenant_id**，见
   ``migrations/001_core_tables.sql`` 的 ``uq_customer_platform_shop_extkey``）。
   为此 ``ext_key`` 带租户短标签（``syn_<tenant8位>_<序号>``），**跨租户不会撞键**。
2. **清理**：同一次事务内删除「本租户 + ``_synthetic`` + 不在本次键集合内」的行，
   使 ``SELECT count(*) FROM customer WHERE _synthetic = true`` 恒等于本次 ``--count``。
   被其它表外键引用的行（``customer_identity`` 等）**不删**（避免破坏 T-C1 的归集结果），
   此时总数可能大于 ``--count``，属有意取舍。

纪律：

- **红线⑤**：本模块只 import 标准库 + ``yaml`` + ``psycopg``，**零供应商 SDK**。
- **迁移单一写者**：本波**不写迁移**；``customer`` 表结构由 T0.2 落地，本模块只 INSERT。
- **PRD C1**：明文 PII 一律不落库，只写 ``phone_hash`` / ``email_hash`` /
  ``address_fingerprint``（SHA-256 十六进制）。
- **裁决 #15**：``consent_status`` / ``consent_time`` / ``consent_source`` 三字段齐全。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final

import psycopg
import yaml
from psycopg import sql

from src.data.schemas import (
    CONSENT_STATUS_GRANTED,
    CONSENT_STATUSES_WITHOUT_CONSENT,
    CUSTOMER_COLUMNS,
    LAYER_NAMES,
    PLATFORMS,
    RISK_CATEGORIES,
    SAMPLE_CLASS_EXPLICIT_REFUSAL,
    SAMPLE_CLASS_HIGH_COMPLAINT,
    SAMPLE_CLASS_HIGH_REFUND,
    SAMPLE_CLASS_IDENTITY_HIGH,
    SAMPLE_CLASS_IDENTITY_LOW,
    SAMPLE_CLASS_IDENTITY_MEDIUM,
    SAMPLE_CLASS_NO_CONSENT,
    SAMPLE_CLASS_NORMAL,
    SAMPLE_CLASS_TOP_VALUE,
    CustomerRow,
    GeneratedSample,
)
from src.policy_kernel.config import Segmentation
from src.unification.segmentation import amount_tier_of, recency_tier_of

__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_NOW",
    "DEFAULT_SEED",
    "DEFAULT_TENANT_ID",
    "Thresholds",
    "generate_samples",
    "layer_of",
    "load_thresholds",
    "main",
    "summarize",
    "write_samples",
]

#: 默认随机种子（固定 → 可复现）。
DEFAULT_SEED: Final = 20261026
#: 默认样本数（标准集 1 万；MVP 演示用 2000）。
DEFAULT_COUNT: Final = 10000
#: 默认时间锚点 = 冻结评测基准的 ``now``（2026-10-23T12:00:00Z）——保证"距今"判定可复现。
DEFAULT_NOW: Final = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
#: 默认租户（合成数据专用，与业务租户隔离）。
DEFAULT_TENANT_ID: Final = uuid.UUID("11111111-1111-4111-8111-111111111111")

#: ``governance.yaml`` 的路径（仓库根）。
GOVERNANCE_PATH: Final = Path(__file__).resolve().parents[2] / "governance.yaml"

#: ``customer_id`` 的确定性派生命名空间（uuid5 → 同输入同 id）。
_ID_NAMESPACE: Final = uuid.UUID("6f2f1f4c-1d3a-5b7e-9c21-0a7d5e3b8c44")

#: 合成数据用的店铺前缀（每平台 3 家，便于演示多店铺场景）。
_SHOP_COUNT: Final = 3

#: 金额档取值区间（**留边界余量**，防止"按真实 now 重算分层"时跨档）。
_AMOUNT_CENTS: Final[dict[str, tuple[int, int]]] = {
    "low": (100, 9500),
    "mid": (10500, 49000),
    "high": (50500, 300000),
}
#: 近度档取值区间（天；同样留余量）。
_RECENCY_DAYS: Final[dict[str, tuple[int, int]]] = {
    "recent": (1, 20),
    "mid": (35, 80),
    "stale": (95, 300),
}

#: 普通样本的退款率区间（**必须 ≤ 阈值**，否则会误触 ``SUP_HIGH_REFUND``）。
_SAFE_REFUND_RATE_CENTS: Final[tuple[int, int]] = (0, 1500)
#: 高退款样本的退款率区间（**必须 > 阈值**）。
_HIGH_REFUND_RATE_CENTS: Final[tuple[int, int]] = (2500, 6000)


# ---------------------------------------------------------------------------
# 阈值（唯一来源 = governance.yaml）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """治理阈值快照（唯一来源 = ``governance.yaml``）。

    九宫格分层阈值**不另存一份**，挂在 ``segmentation`` 上——分档算法的唯一实现点是
    ``src/unification/segmentation.py``（拍板⑩：消除 T-A1 / T-C2 并行导致的重复实现）。
    """

    complaint_window_days: int
    refund_rate_threshold: Decimal
    high_value_percentile: Decimal
    segmentation: Segmentation


def load_thresholds(path: Path | None = None) -> Thresholds:
    """读取 ``governance.yaml`` 的阈值块（**策略参数唯一来源**）。

    依据：``governance.yaml`` 文件头纪律 1「本文件是策略参数的唯一来源，代码里不得
    再写一份阈值常量」+ 附录D 冻结版规则 1「阈值参数化」。

    失败即抛错（**fail-fast**，不静默降级）：配置读不出来 = 门禁失效。
    """
    governance_path = path or GOVERNANCE_PATH
    with governance_path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    thresholds = raw["thresholds"]
    segmentation = raw["segmentation"]
    return Thresholds(
        complaint_window_days=int(thresholds["complaint_window_days"]),
        refund_rate_threshold=Decimal(str(thresholds["refund_rate_threshold"])),
        high_value_percentile=Decimal(str(thresholds["high_value_percentile"])),
        segmentation=Segmentation(
            layer_score_base=int(segmentation["layer_score_base"]),
            amount_high_min_cents=int(segmentation["amount"]["high_min_cents"]),
            amount_mid_min_cents=int(segmentation["amount"]["mid_min_cents"]),
            recency_recent_max_days=int(segmentation["recency"]["recent_max_days"]),
            recency_mid_max_days=int(segmentation["recency"]["mid_max_days"]),
        ),
    )


# ---------------------------------------------------------------------------
# 九宫格分层（附录D 规则 3 / 规则 4）
# ---------------------------------------------------------------------------


def layer_of(
    avg_order_value_90d: Decimal, days_since_last_order: int, thresholds: Thresholds
) -> str:
    """算九宫格层名（**纯函数**：阈值取自 ``governance.yaml``）。

    **不另写分档逻辑**——分档的唯一实现点是 ``src/unification/segmentation.py``
    （拍板⑩）。本函数只做单位换算（元 → 分）与层名拼装
    （``amount_<金额档>_<近度档>``，口径与评测标注基准 v1.3 一致）。
    """
    cents = int((Decimal(avg_order_value_90d) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    amount_tier, _ = amount_tier_of(cents, thresholds.segmentation)
    recency_tier, _ = recency_tier_of(days_since_last_order, thresholds.segmentation)
    return f"amount_{amount_tier}_{recency_tier}"


# ---------------------------------------------------------------------------
# 计划（样本分类 + 覆盖维度）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Allocation:
    """各类样本的条数配额。"""

    risk_per_category: int
    high_identity_groups: int
    medium_identity_pairs: int
    low_identity_rows: int

    @property
    def total_reserved(self) -> int:
        return (
            len(RISK_CATEGORIES) * self.risk_per_category
            + 2 * self.high_identity_groups
            + 2 * self.medium_identity_pairs
            + self.low_identity_rows
        )


@dataclass(frozen=True)
class _Spec:
    """一条待生成样本的规格（不含随机值）。

    - ``group_id`` —— 同一"人"的组号（跨平台共享 ``ext_key`` 的成对行同组）；
    - ``shared_feature`` —— 该组**跨平台共享**的特征（``"phone"`` / ``"addr"`` / ``None``）：
      ``None`` = 两个平台共享全部特征（高置信）；``"phone"`` / ``"addr"`` = **只共享一个**
      特征（中置信，其余特征按平台区分）。
    """

    sample_class: str
    identity_tier: str
    platform: str
    shop_id: str
    ext_key: str
    layer: str
    group_id: int
    shared_feature: str | None = None


class _LayerPicker:
    """按固定顺序轮转取层名——保证九宫格 9 层**均匀覆盖**。"""

    def __init__(self, names: Sequence[str] = LAYER_NAMES) -> None:
        self._names = tuple(names)
        self._index = 0

    def next(self) -> str:
        name = self._names[self._index % len(self._names)]
        self._index += 1
        return name


def _allocate(count: int) -> _Allocation:
    """按 ``count`` 分配各类样本的条数配额（小规模时按优先级回缩）。

    规模口径（架构 v5.2 §9.4）：1 万标准集下五类风控样本各 ≥50 条、身份三档各 ≥10；
    2 千演示集下各类 ≥10（本实现给到 12 条留余量）。
    """
    risk_per_category = max(1, min(50, max(10, round(count * 0.006))))
    high_identity_groups = max(1, count // 5)
    medium_identity_pairs = max(1, round(count * 0.01))
    low_identity_rows = max(1, round(count * 0.01))

    allocation = _Allocation(
        risk_per_category=risk_per_category,
        high_identity_groups=high_identity_groups,
        medium_identity_pairs=medium_identity_pairs,
        low_identity_rows=low_identity_rows,
    )
    # 预算不够时依次回缩：低置信 → 中置信 → 高置信 → 风控（风控是验收重点，最后动）。
    while allocation.total_reserved > count and allocation.low_identity_rows > 1:
        allocation = _Allocation(
            allocation.risk_per_category,
            allocation.high_identity_groups,
            allocation.medium_identity_pairs,
            allocation.low_identity_rows - 1,
        )
    while allocation.total_reserved > count and allocation.medium_identity_pairs > 1:
        allocation = _Allocation(
            allocation.risk_per_category,
            allocation.high_identity_groups,
            allocation.medium_identity_pairs - 1,
            allocation.low_identity_rows,
        )
    while allocation.total_reserved > count and allocation.high_identity_groups > 1:
        allocation = _Allocation(
            allocation.risk_per_category,
            allocation.high_identity_groups - 1,
            allocation.medium_identity_pairs,
            allocation.low_identity_rows,
        )
    while allocation.total_reserved > count and allocation.risk_per_category > 1:
        allocation = _Allocation(
            allocation.risk_per_category - 1,
            allocation.high_identity_groups,
            allocation.medium_identity_pairs,
            allocation.low_identity_rows,
        )
    return allocation


def _build_specs(count: int, rng: random.Random, tenant_id: uuid.UUID) -> list[_Spec]:
    """排布样本计划（分类 × 覆盖维度）。"""
    allocation = _allocate(count)
    tenant_tag = tenant_id.hex[:8]
    specs: list[_Spec] = []
    group_id = 0

    def next_group() -> int:
        nonlocal group_id
        group_id += 1
        return group_id

    def next_ext_key(gid: int) -> str:
        return f"syn_{tenant_tag}_{gid:06d}"

    def shop_for(platform: str) -> str:
        return f"shop_{platform}_{rng.randint(1, _SHOP_COUNT):03d}"

    normal_picker = _LayerPicker()
    risk_picker = _LayerPicker()
    identity_picker = _LayerPicker()
    top_value_picker = _LayerPicker(
        [name for name in LAYER_NAMES if name.startswith("amount_high")]
    )

    # ① 五类风控样本（各类至少 10 条；每类只命中自己的 rule_id）。
    for category in RISK_CATEGORIES:
        for index in range(allocation.risk_per_category):
            gid = next_group()
            platform = PLATFORMS[index % len(PLATFORMS)]
            layer = (
                top_value_picker.next()
                if category == SAMPLE_CLASS_TOP_VALUE
                else risk_picker.next()
            )
            specs.append(
                _Spec(
                    sample_class=category,
                    identity_tier="low",
                    platform=platform,
                    shop_id=shop_for(platform),
                    ext_key=next_ext_key(gid),
                    layer=layer,
                    group_id=gid,
                    shared_feature=None,
                )
            )

    # ② 身份匹配高置信：跨平台共享同一 ext_key（自动归集，供 T-B2 冲突消解验收）。
    for _ in range(allocation.high_identity_groups):
        gid = next_group()
        ext_key = next_ext_key(gid)
        for platform in PLATFORMS:
            specs.append(
                _Spec(
                    sample_class=SAMPLE_CLASS_IDENTITY_HIGH,
                    identity_tier="high",
                    platform=platform,
                    shop_id=shop_for(platform),
                    ext_key=ext_key,
                    layer=identity_picker.next(),
                    group_id=gid,
                    shared_feature=None,
                )
            )

    # ③ 身份匹配中置信：跨平台共享**一个**特征（手机号哈希 或 地址指纹），ext_key 不同。
    for index in range(allocation.medium_identity_pairs):
        gid = next_group()
        share_phone = index % 2 == 0
        for platform in PLATFORMS:
            specs.append(
                _Spec(
                    sample_class=SAMPLE_CLASS_IDENTITY_MEDIUM,
                    identity_tier="medium",
                    platform=platform,
                    shop_id=shop_for(platform),
                    ext_key=next_ext_key(next_group()),
                    layer=identity_picker.next(),
                    group_id=gid,
                    shared_feature="phone" if share_phone else "addr",
                )
            )

    # ④ 身份匹配低置信：完全无匹配特征（保留独立身份，PRD P1 红线）。
    for index in range(allocation.low_identity_rows):
        gid = next_group()
        platform = PLATFORMS[index % len(PLATFORMS)]
        specs.append(
            _Spec(
                sample_class=SAMPLE_CLASS_IDENTITY_LOW,
                identity_tier="low",
                platform=platform,
                shop_id=shop_for(platform),
                ext_key=next_ext_key(gid),
                layer=identity_picker.next(),
                group_id=gid,
                shared_feature=None,
            )
        )

    # ⑤ 普通样本（铺满剩余预算，九宫格均匀覆盖）。
    remaining = count - len(specs)
    for index in range(max(0, remaining)):
        gid = next_group()
        platform = PLATFORMS[index % len(PLATFORMS)]
        specs.append(
            _Spec(
                sample_class=SAMPLE_CLASS_NORMAL,
                identity_tier="low",
                platform=platform,
                shop_id=shop_for(platform),
                ext_key=next_ext_key(gid),
                layer=normal_picker.next(),
                group_id=gid,
                shared_feature=None,
            )
        )
    return specs


# ---------------------------------------------------------------------------
# 行物化
# ---------------------------------------------------------------------------


def _digest(kind: str, seed: str) -> str:
    """特征哈希（SHA-256 十六进制）——**只落哈希，不落明文**（PRD C1）。"""
    return hashlib.sha256(f"{kind}|{seed}".encode()).hexdigest()


def _amount_value(amount_tier: str, rng: random.Random) -> Decimal:
    low, high = _AMOUNT_CENTS[amount_tier]
    return (Decimal(rng.randint(low, high)) / 100).quantize(Decimal("0.01"))


def _recency_days(recency_tier: str, rng: random.Random) -> int:
    low, high = _RECENCY_DAYS[recency_tier]
    return rng.randint(low, high)


def _materialize(
    spec: _Spec,
    *,
    tenant_id: uuid.UUID,
    now: datetime,
    rng: random.Random,
) -> CustomerRow:
    """把一条规格物化成一行 ``customer`` 数据。"""
    _, amount_tier, recency_tier = spec.layer.split("_")
    avg_order_value = _amount_value(amount_tier, rng)
    days_since_last_order = _recency_days(recency_tier, rng)
    last_order_at = now - timedelta(days=days_since_last_order)
    order_count = rng.randint(1, 20)
    total_amount = (avg_order_value * order_count).quantize(Decimal("0.01"))

    # ── 授权三字段（裁决 #15）──
    consent_status = CONSENT_STATUS_GRANTED
    consent_time: datetime | None = now - timedelta(days=rng.randint(1, 365))
    consent_source: str | None = "synthetic_form"
    if spec.sample_class == SAMPLE_CLASS_NO_CONSENT:
        consent_status = CONSENT_STATUSES_WITHOUT_CONSENT[
            spec.group_id % len(CONSENT_STATUSES_WITHOUT_CONSENT)
        ]
        if consent_status == "revoked":
            # 曾经授权、现被撤回：保留历史授权时间与来源，便于审计举证。
            consent_time = now - timedelta(days=rng.randint(1, 365))
        else:
            consent_time = None
            consent_source = None

    # ── 抑制红线输入（附录D 规则 1a / 1b）──
    complaint_count_90d = 0
    has_refund_dispute = False
    refund_rate = (Decimal(rng.randint(*_SAFE_REFUND_RATE_CENTS)) / 10000).quantize(
        Decimal("0.000001")
    )
    explicit_refusal = False
    is_top_value = False

    if spec.sample_class == SAMPLE_CLASS_HIGH_COMPLAINT:
        complaint_count_90d = rng.randint(1, 5)
    elif spec.sample_class == SAMPLE_CLASS_HIGH_REFUND:
        if spec.group_id % 2 == 0:
            # 变体一：近 90 天有纠纷（退款率保持低位，只由纠纷触发）。
            has_refund_dispute = True
        else:
            # 变体二：无纠纷但退款率超阈值。
            refund_rate = (Decimal(rng.randint(*_HIGH_REFUND_RATE_CENTS)) / 10000).quantize(
                Decimal("0.000001")
            )
    elif spec.sample_class == SAMPLE_CLASS_EXPLICIT_REFUSAL:
        explicit_refusal = True
    elif spec.sample_class == SAMPLE_CLASS_TOP_VALUE:
        is_top_value = True

    # ── 身份匹配特征（PRD C1：只有哈希 / 指纹）──
    # 组种子 = 跨平台共享（同一"人"）；平台种子 = 各平台独立。
    group_seed = f"g{spec.group_id}"
    platform_seed = f"g{spec.group_id}|{spec.platform}"
    if spec.sample_class == SAMPLE_CLASS_IDENTITY_LOW:
        # 低置信样本：完全无匹配特征（保留独立身份，PRD P1 红线）。
        phone_hash = None
        email_hash = None
        address_fingerprint = None
    elif spec.shared_feature == "phone":
        # 中置信：只共享手机号哈希，地址指纹各平台不同。
        phone_hash = _digest("phone", group_seed)
        email_hash = _digest("email", platform_seed)
        address_fingerprint = _digest("addr", platform_seed)
    elif spec.shared_feature == "addr":
        # 中置信：只共享地址指纹，手机号哈希各平台不同。
        phone_hash = _digest("phone", platform_seed)
        email_hash = _digest("email", platform_seed)
        address_fingerprint = _digest("addr", group_seed)
    else:
        # 高置信（跨平台共享 ext_key）与普通 / 风控样本：组内特征一致。
        phone_hash = _digest("phone", group_seed)
        email_hash = _digest("email", group_seed)
        address_fingerprint = _digest("addr", group_seed)

    customer_id = uuid.uuid5(
        _ID_NAMESPACE, f"{tenant_id}|{spec.platform}|{spec.shop_id}|{spec.ext_key}"
    )
    return CustomerRow(
        customer_id=customer_id,
        tenant_id=tenant_id,
        platform=spec.platform,
        shop_id=spec.shop_id,
        ext_key=spec.ext_key,
        alias=f"syn_{spec.group_id:06d}",
        phone_hash=phone_hash,
        email_hash=email_hash,
        address_fingerprint=address_fingerprint,
        consent_status=consent_status,
        consent_time=consent_time,
        consent_source=consent_source,
        total_amount=total_amount,
        order_count=order_count,
        last_order_at=last_order_at,
        avg_order_value_90d=avg_order_value,
        complaint_count_90d=complaint_count_90d,
        has_refund_dispute=has_refund_dispute,
        refund_rate=refund_rate,
        explicit_refusal=explicit_refusal,
        is_top_value=is_top_value,
        status="active",
        _synthetic=True,
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# 纯函数入口
# ---------------------------------------------------------------------------


def generate_samples(
    *,
    count: int,
    seed: int = DEFAULT_SEED,
    tenant_id: uuid.UUID | None = None,
    now: datetime | None = None,
    thresholds: Thresholds | None = None,
) -> list[GeneratedSample]:
    """生成合成样本（**纯函数**：不连库、不取时钟、不用全局随机源）。

    - 同 ``(count, seed, tenant_id, now)`` → 同一批行（可复现，回归门禁的前提）；
    - ``count`` = 落库的 ``customer`` 行数（含跨平台重叠的成对行）。

    覆盖维度（架构 v5.2 §9.4）：五类风控样本各 ≥10、九宫格 9 层全覆盖、
    身份匹配三档各 ≥10、跨平台同 ``ext_key`` 重叠 ≥10 组。
    """
    if count <= 0:
        raise ValueError(f"count 必须为正整数，收到 {count}")
    resolved_thresholds = thresholds or load_thresholds()
    resolved_tenant = tenant_id or DEFAULT_TENANT_ID
    resolved_now = now or DEFAULT_NOW
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=UTC)

    rng = random.Random(seed)
    specs = _build_specs(count, rng, resolved_tenant)
    samples: list[GeneratedSample] = []
    for spec in specs:
        row = _materialize(
            spec,
            tenant_id=resolved_tenant,
            now=resolved_now,
            rng=rng,
        )
        days_since_last_order = (resolved_now - row.last_order_at).days  # type: ignore[operator]
        samples.append(
            GeneratedSample(
                row=row,
                sample_class=spec.sample_class,
                identity_tier=spec.identity_tier,
                layer=layer_of(row.avg_order_value_90d, days_since_last_order, resolved_thresholds),
            )
        )
    return samples


def summarize(samples: Sequence[GeneratedSample]) -> dict[str, int]:
    """按样本分类统计条数（CLI 输出 / 人工核对用）。"""
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample.sample_class] = counts.get(sample.sample_class, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# 落库（薄壳：只写库，不含判定逻辑）
# ---------------------------------------------------------------------------


def _normalize_url(database_url: str) -> str:
    """把 SQLAlchemy 风格的连接串转成 psycopg 能吃的形式。"""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


_UPSERT_SQL = f"""
INSERT INTO customer ({", ".join(CUSTOMER_COLUMNS)})
VALUES ({", ".join(["%s"] * len(CUSTOMER_COLUMNS))})
ON CONFLICT (platform, shop_id, ext_key) DO UPDATE SET
    alias = EXCLUDED.alias,
    phone_hash = EXCLUDED.phone_hash,
    email_hash = EXCLUDED.email_hash,
    address_fingerprint = EXCLUDED.address_fingerprint,
    consent_status = EXCLUDED.consent_status,
    consent_time = EXCLUDED.consent_time,
    consent_source = EXCLUDED.consent_source,
    total_amount = EXCLUDED.total_amount,
    order_count = EXCLUDED.order_count,
    last_order_at = EXCLUDED.last_order_at,
    avg_order_value_90d = EXCLUDED.avg_order_value_90d,
    complaint_count_90d = EXCLUDED.complaint_count_90d,
    has_refund_dispute = EXCLUDED.has_refund_dispute,
    refund_rate = EXCLUDED.refund_rate,
    explicit_refusal = EXCLUDED.explicit_refusal,
    is_top_value = EXCLUDED.is_top_value,
    status = EXCLUDED.status,
    _synthetic = EXCLUDED._synthetic,
    updated_at = EXCLUDED.updated_at
"""

_CHILD_TABLES_SQL = """
SELECT DISTINCT kcu.table_name
FROM information_schema.table_constraints AS tc
JOIN information_schema.key_column_usage AS kcu
  ON kcu.constraint_name = tc.constraint_name
 AND kcu.constraint_schema = tc.constraint_schema
JOIN information_schema.constraint_column_usage AS ccu
  ON ccu.constraint_name = tc.constraint_name
 AND ccu.constraint_schema = tc.constraint_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
  AND tc.table_schema = current_schema()
  AND ccu.table_name = 'customer'
  AND ccu.column_name = 'customer_id'
ORDER BY kcu.table_name
"""


def _child_tables(cur: psycopg.Cursor[Any]) -> list[str]:
    """列出外键引用 ``customer(customer_id)`` 的表（清理时**不删**被引用的行）。"""
    cur.execute(_CHILD_TABLES_SQL)
    return [str(row[0]) for row in cur.fetchall()]


def _prune(
    cur: psycopg.Cursor[Any], samples: Sequence[GeneratedSample], tenant_id: uuid.UUID
) -> int:
    """删除本租户下"不在本次键集合内"的合成行（幂等的第二步）。

    被其它表外键引用的行**不删**（避免破坏 T-C1 的归集结果等他人数据）。
    """
    cur.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _synth_keys (
            platform TEXT NOT NULL,
            shop_id TEXT NOT NULL,
            ext_key TEXT NOT NULL,
            PRIMARY KEY (platform, shop_id, ext_key)
        ) ON COMMIT DROP
        """
    )
    cur.execute("TRUNCATE _synth_keys")
    cur.executemany(
        "INSERT INTO _synth_keys (platform, shop_id, ext_key) VALUES (%s, %s, %s)",
        [(s.row.platform, s.row.shop_id, s.row.ext_key) for s in samples],
    )
    child_guards = [
        sql.SQL("NOT EXISTS (SELECT 1 FROM {} ch WHERE ch.customer_id = c.customer_id)").format(
            sql.Identifier(table)
        )
        for table in _child_tables(cur)
    ]
    guard = sql.SQL(" AND ").join(child_guards) if child_guards else sql.SQL("TRUE")
    statement = sql.SQL(
        """
        DELETE FROM customer AS c
        WHERE c.tenant_id = %s
          AND c._synthetic = true
          AND NOT EXISTS (
              SELECT 1 FROM _synth_keys AS k
              WHERE k.platform = c.platform
                AND k.shop_id = c.shop_id
                AND k.ext_key = c.ext_key
          )
          AND {guard}
        """
    ).format(guard=guard)
    cur.execute(statement, (tenant_id,))
    return int(cur.rowcount)


def write_samples(
    samples: Sequence[GeneratedSample], *, database_url: str, tenant_id: uuid.UUID
) -> int:
    """把样本写入真 PostgreSQL（**薄壳**：无判定逻辑）。

    幂等口径：upsert（冲突键 ``(platform, shop_id, ext_key)``）+ 清理本租户内的过期
    合成行。**不写迁移、不 ALTER、不建表**（表结构由 T0.2 落地）。
    """
    if not samples:
        return 0
    url = _normalize_url(database_url)
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.executemany(_UPSERT_SQL, [s.row.as_db_tuple() for s in samples])
        _prune(cur, samples, tenant_id)
    return len(samples)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_now(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.data.generator",
        description="合成数据生成器（T-A1）：生成 _synthetic=true 的客户数据集。",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=DEFAULT_COUNT,
        help=f"生成条数（默认 {DEFAULT_COUNT}，MVP 演示 2000）",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"随机种子（默认 {DEFAULT_SEED}）"
    )
    parser.add_argument(
        "--tenant-id", default=str(DEFAULT_TENANT_ID), help="目标租户 id（默认合成专用租户）"
    )
    parser.add_argument(
        "--now",
        default=None,
        help="时间锚点 ISO8601（默认 2026-10-23T12:00:00Z，与冻结评测基准一致）",
    )
    parser.add_argument(
        "--database-url", default=None, help="连接串（默认取环境变量 DATABASE_URL）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只生成不落库（打印分类统计）")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``python -m src.data.generator --count 2000``。"""
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        tenant_id = uuid.UUID(args.tenant_id)
    except ValueError as error:  # pragma: no cover - 参数错误路径
        parser.error(f"--tenant-id 不是合法 UUID：{args.tenant_id}（{error}）")
    now = _parse_now(args.now) if args.now else DEFAULT_NOW

    samples = generate_samples(count=args.count, seed=args.seed, tenant_id=tenant_id, now=now)
    counts = summarize(samples)
    print(
        f"生成 {len(samples)} 条合成客户样本（seed={args.seed}, tenant={tenant_id}, now={now.isoformat()}）"
    )
    for sample_class, amount in sorted(counts.items()):
        print(f"  {sample_class:<20} {amount:>6}")

    if args.dry_run:
        return 0

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "未设置 DATABASE_URL，无法落库。请先 `cp .env.example .env` 并 `docker compose up -d`，"
            "或用 --database-url 显式指定。",
            file=sys.stderr,
        )
        return 1
    written = write_samples(samples, database_url=database_url, tenant_id=tenant_id)
    print(f"已写入 customer 表 {written} 行（_synthetic=true，幂等 upsert + 清理本租户过期行）")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
