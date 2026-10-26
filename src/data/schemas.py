"""合成数据的行级 schema 与样本分类常量（T-A1 · Wave 1【A】路）。

权威依据（可反查）：

- **架构 v5.2 §9.4**「合成数据标准集规格」（裁决 #16/#15/#17 / ADR-040）：
  全部记录标 ``_synthetic: true``；五类特殊样本（高投诉 / 高退款 / 明确拒绝 / 无授权 /
  Top 1% 高价值）各 ≥50 条（1 万标准集）；九宫格 9 层全覆盖；身份匹配三档预置样本。
- **架构 v5.2 §9.5** + **裁决 #17**：身份匹配三档——高置信 = 跨平台 ``ext_key`` 精确匹配；
  中置信 = 手机号哈希 / 收件地址指纹**部分匹配**；低置信 = 无匹配特征。
- **附录D §4 规则 ID 登记表**：五类风控样本各自对应的 ``rule_id``（**禁止自造 ID**）。
- **PRD C1**：明文 PII 一律不落库 → 只写 ``phone_hash`` / ``email_hash`` /
  ``address_fingerprint``。
- **裁决 #15**：知情同意三字段 ``consent_status`` / ``consent_time`` /
  ``consent_source`` 齐全，缺失授权即 ``NO_CONSENT`` → DENY。
- **数据库纪律**：``customer`` 表结构由 **T0.2** 落地（``migrations/001_core_tables.sql``），
  本模块的字段与 ``src/models/customer.py`` **逐列一一对应**；本波**不写迁移**。

本模块是**纯数据定义**（常量 + 不可变 dataclass），零 I/O、零时钟依赖，可直接单测。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

__all__ = [
    "AMOUNT_TIERS",
    "CONSENT_STATUSES_WITHOUT_CONSENT",
    "CONSENT_STATUS_GRANTED",
    "CUSTOMER_COLUMNS",
    "IDENTITY_TIERS",
    "IDENTITY_TIER_HIGH",
    "IDENTITY_TIER_LOW",
    "IDENTITY_TIER_MEDIUM",
    "LAYER_NAMES",
    "PLATFORMS",
    "PLATFORM_DOUYIN",
    "PLATFORM_TAOBAO",
    "RECENCY_TIERS",
    "RISK_CATEGORIES",
    "RISK_RULE_IDS",
    "SAMPLE_CLASSES",
    "SAMPLE_CLASS_EXPLICIT_REFUSAL",
    "SAMPLE_CLASS_HIGH_COMPLAINT",
    "SAMPLE_CLASS_HIGH_REFUND",
    "SAMPLE_CLASS_IDENTITY_HIGH",
    "SAMPLE_CLASS_IDENTITY_LOW",
    "SAMPLE_CLASS_IDENTITY_MEDIUM",
    "SAMPLE_CLASS_NORMAL",
    "SAMPLE_CLASS_NO_CONSENT",
    "SAMPLE_CLASS_TOP_VALUE",
    "CustomerRow",
    "GeneratedSample",
]

# ---------------------------------------------------------------------------
# 平台口径
# ---------------------------------------------------------------------------

#: 淘系（平台口径统一自评测标注基准 v1.3：jd → douyin 全量替换）。
PLATFORM_TAOBAO: Final = "taobao"
#: 抖店。
PLATFORM_DOUYIN: Final = "douyin"
#: 参与生成的两个平台（跨平台重叠样本由这两个平台构成）。
PLATFORMS: Final[tuple[str, ...]] = (PLATFORM_TAOBAO, PLATFORM_DOUYIN)

# ---------------------------------------------------------------------------
# 样本分类
# ---------------------------------------------------------------------------

#: 普通样本（无风控特征、无跨平台重叠）。
SAMPLE_CLASS_NORMAL: Final = "normal"
#: 高投诉样本（近 90 天投诉 ≥1）。
SAMPLE_CLASS_HIGH_COMPLAINT: Final = "high_complaint"
#: 高退款样本（近 90 天有纠纷 **或** 退款率 > 阈值）。
SAMPLE_CLASS_HIGH_REFUND: Final = "high_refund"
#: 明确拒绝营销样本。
SAMPLE_CLASS_EXPLICIT_REFUSAL: Final = "explicit_refusal"
#: 无有效营销授权样本。
SAMPLE_CLASS_NO_CONSENT: Final = "no_consent"
#: Top 1% 高价值消费样本。
SAMPLE_CLASS_TOP_VALUE: Final = "top_value"
#: 身份匹配高置信样本（跨平台共享 ``ext_key``，自动归集）。
SAMPLE_CLASS_IDENTITY_HIGH: Final = "identity_high"
#: 身份匹配中置信样本（跨平台共享**一个**特征，进人工复核队列 E4）。
SAMPLE_CLASS_IDENTITY_MEDIUM: Final = "identity_medium"
#: 身份匹配低置信样本（**完全无**匹配特征，保留独立身份，PRD P1 红线）。
SAMPLE_CLASS_IDENTITY_LOW: Final = "identity_low"

#: 全部样本分类。
SAMPLE_CLASSES: Final[tuple[str, ...]] = (
    SAMPLE_CLASS_NORMAL,
    SAMPLE_CLASS_HIGH_COMPLAINT,
    SAMPLE_CLASS_HIGH_REFUND,
    SAMPLE_CLASS_EXPLICIT_REFUSAL,
    SAMPLE_CLASS_NO_CONSENT,
    SAMPLE_CLASS_TOP_VALUE,
    SAMPLE_CLASS_IDENTITY_HIGH,
    SAMPLE_CLASS_IDENTITY_MEDIUM,
    SAMPLE_CLASS_IDENTITY_LOW,
)

#: 五类风控样本（附录D 规则 1a / 1b）。
RISK_CATEGORIES: Final[tuple[str, ...]] = (
    SAMPLE_CLASS_HIGH_COMPLAINT,
    SAMPLE_CLASS_HIGH_REFUND,
    SAMPLE_CLASS_EXPLICIT_REFUSAL,
    SAMPLE_CLASS_NO_CONSENT,
    SAMPLE_CLASS_TOP_VALUE,
)

#: 风控样本 → 命中 rule_id（**取自附录D §4 登记表，禁止自造**）。
RISK_RULE_IDS: Final[dict[str, str]] = {
    SAMPLE_CLASS_HIGH_COMPLAINT: "SUP_HIGH_COMPLAINT",
    SAMPLE_CLASS_HIGH_REFUND: "SUP_HIGH_REFUND",
    SAMPLE_CLASS_EXPLICIT_REFUSAL: "SUP_EXPLICIT_REFUSAL",
    SAMPLE_CLASS_NO_CONSENT: "NO_CONSENT",
    SAMPLE_CLASS_TOP_VALUE: "SUP_HIGH_VALUE",
}

# ---------------------------------------------------------------------------
# 身份匹配三档（架构 v5.2 §9.5）
# ---------------------------------------------------------------------------

IDENTITY_TIER_HIGH: Final = "high"
IDENTITY_TIER_MEDIUM: Final = "medium"
IDENTITY_TIER_LOW: Final = "low"

#: 三档置信度（顺序 = 判定优先级：高 → 中 → 低）。
IDENTITY_TIERS: Final[tuple[str, ...]] = (
    IDENTITY_TIER_HIGH,
    IDENTITY_TIER_MEDIUM,
    IDENTITY_TIER_LOW,
)

# ---------------------------------------------------------------------------
# 知情同意（裁决 #15）
# ---------------------------------------------------------------------------

#: 有有效授权。
CONSENT_STATUS_GRANTED: Final = "granted"
#: 无有效授权的四种状态 → 命中 ``NO_CONSENT``（规则 1b，发送前校验）。
CONSENT_STATUSES_WITHOUT_CONSENT: Final[tuple[str, ...]] = (
    "denied",
    "expired",
    "revoked",
    "unknown",
)

# ---------------------------------------------------------------------------
# 九宫格分层（附录D 规则 3 / 规则 4 的 layer_score 体系）
# ---------------------------------------------------------------------------

#: 金额档（低 → 高）。
AMOUNT_TIERS: Final[tuple[str, ...]] = ("low", "mid", "high")
#: 近度档（陈旧 → 新近）。
RECENCY_TIERS: Final[tuple[str, ...]] = ("stale", "mid", "recent")

#: 九宫格 9 个层名，**按 layer_score 升序**排列（层名口径与评测标注基准 v1.3 一致）。
LAYER_NAMES: Final[tuple[str, ...]] = tuple(
    f"amount_{amount}_{recency}" for amount in AMOUNT_TIERS for recency in RECENCY_TIERS
)


# ---------------------------------------------------------------------------
# 行级 schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CustomerRow:
    """``customer`` 表的一行（**逐列对应** ``src/models/customer.py``）。

    - 无明文 PII：只有 ``phone_hash`` / ``email_hash`` / ``address_fingerprint``（PRD C1）；
    - 授权三字段齐全（裁决 #15）；
    - ``_synthetic`` 恒为 ``True``——本生成器**不写**任何 ``_synthetic = false`` 的行。
    """

    customer_id: uuid.UUID
    tenant_id: uuid.UUID
    platform: str
    shop_id: str
    ext_key: str
    alias: str | None
    phone_hash: str | None
    email_hash: str | None
    address_fingerprint: str | None
    consent_status: str
    consent_time: datetime | None
    consent_source: str | None
    total_amount: Decimal
    order_count: int
    last_order_at: datetime | None
    avg_order_value_90d: Decimal
    complaint_count_90d: int
    has_refund_dispute: bool
    refund_rate: Decimal
    explicit_refusal: bool
    is_top_value: bool
    status: str
    _synthetic: bool
    created_at: datetime
    updated_at: datetime

    def as_db_tuple(self) -> tuple[object, ...]:
        """按 :data:`CUSTOMER_COLUMNS` 的顺序返回可直接喂给 ``INSERT`` 的值元组。"""
        return (
            self.customer_id,
            self.tenant_id,
            self.platform,
            self.shop_id,
            self.ext_key,
            self.alias,
            self.phone_hash,
            self.email_hash,
            self.address_fingerprint,
            self.consent_status,
            self.consent_time,
            self.consent_source,
            self.total_amount,
            self.order_count,
            self.last_order_at,
            self.avg_order_value_90d,
            self.complaint_count_90d,
            self.has_refund_dispute,
            self.refund_rate,
            self.explicit_refusal,
            self.is_top_value,
            self.status,
            self._synthetic,
            self.created_at,
            self.updated_at,
        )


#: ``customer`` 表的列顺序（与 DDL 一致；INSERT / upsert 冲突目标共用）。
CUSTOMER_COLUMNS: Final[tuple[str, ...]] = (
    "customer_id",
    "tenant_id",
    "platform",
    "shop_id",
    "ext_key",
    "alias",
    "phone_hash",
    "email_hash",
    "address_fingerprint",
    "consent_status",
    "consent_time",
    "consent_source",
    "total_amount",
    "order_count",
    "last_order_at",
    "avg_order_value_90d",
    "complaint_count_90d",
    "has_refund_dispute",
    "refund_rate",
    "explicit_refusal",
    "is_top_value",
    "status",
    "_synthetic",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class GeneratedSample:
    """一条合成样本 = 行数据 + 分类标签（标签**不落库**，只服务于验收断言）。

    - ``sample_class`` —— 样本分类（见 :data:`SAMPLE_CLASSES`）；
    - ``identity_tier`` —— **预期**匹配档位（相对跨平台对端，架构 v5.2 §9.5）；
    - ``layer`` —— 该行的九宫格层名（由行内数据算出，非人工指定）。
    """

    row: CustomerRow
    sample_class: str
    identity_tier: str
    layer: str
