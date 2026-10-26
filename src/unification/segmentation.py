"""九宫格分层引擎（全局 + 平台局部各一套）——**分层算法的唯一实现点**。

权威依据：
- 附录 D 规则 3 —— ``layer_score = amount_score × 10 + recency_score``，取值 11..33，
  数值大者层级更高；层级用九宫格 9 层比较，不是三档。
- 附录 D 规则 4 —— 全局九宫格与平台局部九宫格是**两套独立层级体系**，各自按
  ``layer_score`` 比较；**不存在「全局层级恒大于平台层级」的固定序**。
- 裁决 #11 —— 全局 + 平台局部各一套分层。
- 阈值唯一来源 = ``governance.yaml`` 的 ``segmentation`` 段（代码里不写死常量）。

纪律：
- **唯一实现点**：T-B2 的 ``rules.py`` 只**调用**本模块，不另写一份 layer_score 计算。
- **纯函数**：零 LLM、零框架、零 IO（不连库）。落库在 ``segment_store.py``，
  且 ``src/unification/__init__.py`` **不得** import 落库模块——否则会把 ORM 带进
  策略内核（红线 1）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from src.policy_kernel.config import Segmentation as SegmentationThresholds, load_and_validate

__all__ = [
    "AMOUNT_TIER_HIGH",
    "AMOUNT_TIER_LOW",
    "AMOUNT_TIER_MID",
    "RECENCY_TIER_MID",
    "RECENCY_TIER_RECENT",
    "RECENCY_TIER_STALE",
    "SCOPE_GLOBAL",
    "SCOPE_PLATFORM_LOCAL",
    "SEGMENT_RULE_CODE",
    "CustomerLayerMetrics",
    "CustomerLayers",
    "LayerProfile",
    "Segment",
    "SegmentationError",
    "amount_tier_of",
    "compare_layer_scores",
    "compute_customer_layers",
    "days_since",
    "layer_score_of",
    "recency_tier_of",
    "segment_customer",
    "segment_global",
    "segment_platform_local",
    "thresholds_from_governance",
    "to_cents",
]

#: 分层范围：全局层 / 平台局部层（两套独立体系）。
SCOPE_GLOBAL = "global"
SCOPE_PLATFORM_LOCAL = "platform_local"

#: 分段规则码（与 ``customer_segment.segment_rule_code`` 默认值一致）。
SEGMENT_RULE_CODE = "NINE_GRID"

AMOUNT_TIER_HIGH = "high"
AMOUNT_TIER_MID = "mid"
AMOUNT_TIER_LOW = "low"

RECENCY_TIER_RECENT = "recent"
RECENCY_TIER_MID = "mid"
RECENCY_TIER_STALE = "stale"

#: 金额档 → 分数（附录 D 规则 3：high=3 / mid=2 / low=1）。
_AMOUNT_SCORES: Mapping[str, int] = {
    AMOUNT_TIER_HIGH: 3,
    AMOUNT_TIER_MID: 2,
    AMOUNT_TIER_LOW: 1,
}

#: 近度档 → 分数（附录 D 规则 3：recent=3 / mid=2 / stale=1）。
_RECENCY_SCORES: Mapping[str, int] = {
    RECENCY_TIER_RECENT: 3,
    RECENCY_TIER_MID: 2,
    RECENCY_TIER_STALE: 1,
}

_SECONDS_PER_DAY = 86400

#: 1 元 = 100 分（单位换算系数，**不是**分层阈值）。
_CENTS_PER_YUAN = 100


class SegmentationError(ValueError):
    """分层输入 / 配置错误。消息必须**可操作**：说清哪里错、怎么改。"""


@dataclass(frozen=True)
class CustomerLayerMetrics:
    """一套层级体系下的分层输入度量。

    - ``avg_order_value_cents``：客单价，单位**分**（与 governance.yaml 阈值同单位）；
    - ``days_since_last_purchase``：距最近购买的天数（按整天向下取整）。
    """

    avg_order_value_cents: int
    days_since_last_purchase: int


@dataclass(frozen=True)
class Segment:
    """一套层级体系下的分层结果。"""

    scope: str
    platform: str | None
    amount_tier: str
    recency_tier: str
    amount_score: int
    recency_score: int
    layer_score: int
    segment_rule_code: str = SEGMENT_RULE_CODE

    @property
    def layer_name(self) -> str:
        """九宫格层名（如 ``amount_mid_recent``），与评测标注基准字段同口径。"""
        return f"amount_{self.amount_tier}_{self.recency_tier}"


@dataclass(frozen=True)
class LayerProfile:
    """一套层级体系下的「输入度量 + 分层结果」。"""

    metrics: CustomerLayerMetrics
    segment: Segment


@dataclass(frozen=True)
class CustomerLayers:
    """同一客户的全局层 + 各平台局部层（两套独立体系）。"""

    global_profile: LayerProfile
    platform_profiles: Mapping[str, LayerProfile]

    @property
    def global_segment(self) -> Segment:
        return self.global_profile.segment

    @property
    def platform_segments(self) -> Mapping[str, Segment]:
        return {name: profile.segment for name, profile in self.platform_profiles.items()}


# ---------------------------------------------------------------------------
# 阈值来源（唯一来源 = governance.yaml）
# ---------------------------------------------------------------------------


def thresholds_from_governance(path: str | Path = "governance.yaml") -> SegmentationThresholds:
    """从 ``governance.yaml`` 读取九宫格分层阈值（策略参数唯一来源）。

    走 ``policy_kernel.config.load_and_validate()``——非法配置直接拒绝（fail-fast），
    不在这里另写一份解析逻辑。
    """
    return load_and_validate(path).segmentation


def _validate_thresholds(thresholds: SegmentationThresholds) -> None:
    if thresholds.layer_score_base < 2:
        raise SegmentationError(
            "分层乘数 `segmentation.layer_score_base` 必须 >= 2"
            f"（当前 {thresholds.layer_score_base}）。\n"
            "  请在 governance.yaml 的 `segmentation` 段修正。"
        )
    if thresholds.amount_mid_min_cents >= thresholds.amount_high_min_cents:
        raise SegmentationError(
            "金额档边界不自洽：`segmentation.amount.mid_min_cents` 必须 < "
            f"`segmentation.amount.high_min_cents`（当前 {thresholds.amount_mid_min_cents} >= "
            f"{thresholds.amount_high_min_cents}）。\n"
            "  语义：mid = [mid_min, high_min)，high = >= high_min。"
        )
    if thresholds.recency_recent_max_days >= thresholds.recency_mid_max_days:
        raise SegmentationError(
            "近度档边界不自洽：`segmentation.recency.recent_max_days` 必须 < "
            f"`segmentation.recency.mid_max_days`（当前 {thresholds.recency_recent_max_days} >= "
            f"{thresholds.recency_mid_max_days}）。\n"
            "  语义：recent = <= recent_max，mid = (recent_max, mid_max]，stale = > mid_max。"
        )


# ---------------------------------------------------------------------------
# 档位与层级分（附录 D 规则 3）
# ---------------------------------------------------------------------------


def amount_tier_of(
    avg_order_value_cents: int, thresholds: SegmentationThresholds
) -> tuple[str, int]:
    """金额档判定 → ``(档名, 分数)``（附录 D 规则 3）。

    边界：``>= high_min_cents`` = high；``>= mid_min_cents`` = mid；否则 low。
    """
    if avg_order_value_cents < 0:
        raise SegmentationError(
            f"客单价不能为负（收到 {avg_order_value_cents} 分）。\n"
            "  这是数据错误，不是分层结果——请先修上游数据，不要在这里兜底。"
        )
    if avg_order_value_cents >= thresholds.amount_high_min_cents:
        tier = AMOUNT_TIER_HIGH
    elif avg_order_value_cents >= thresholds.amount_mid_min_cents:
        tier = AMOUNT_TIER_MID
    else:
        tier = AMOUNT_TIER_LOW
    return tier, _AMOUNT_SCORES[tier]


def recency_tier_of(
    days_since_last_purchase: int, thresholds: SegmentationThresholds
) -> tuple[str, int]:
    """近度档判定 → ``(档名, 分数)``（附录 D 规则 3）。

    边界：``<= recent_max_days`` = recent；``<= mid_max_days`` = mid；否则 stale。
    """
    if days_since_last_purchase < 0:
        raise SegmentationError(
            f"距最近购买天数不能为负（收到 {days_since_last_purchase} 天）。\n"
            "  这是数据错误（购买时间在未来），不是分层结果。"
        )
    if days_since_last_purchase <= thresholds.recency_recent_max_days:
        tier = RECENCY_TIER_RECENT
    elif days_since_last_purchase <= thresholds.recency_mid_max_days:
        tier = RECENCY_TIER_MID
    else:
        tier = RECENCY_TIER_STALE
    return tier, _RECENCY_SCORES[tier]


def layer_score_of(
    amount_score: int, recency_score: int, thresholds: SegmentationThresholds
) -> int:
    """``layer_score = amount_score × layer_score_base + recency_score``（附录 D 规则 3）。

    乘数取自配置（``segmentation.layer_score_base``，出厂值 10），取值域 11..33。
    """
    return amount_score * thresholds.layer_score_base + recency_score


# ---------------------------------------------------------------------------
# 分层入口
# ---------------------------------------------------------------------------


def segment_customer(
    metrics: CustomerLayerMetrics,
    thresholds: SegmentationThresholds,
    *,
    scope: str = SCOPE_GLOBAL,
    platform: str | None = None,
) -> Segment:
    """按指定范围分层。

    - ``scope='global'``：全局层，**不得**带 platform（全局分层是客户级属性）。
    - ``scope='platform_local'``：平台局部层，**必须**带 platform
      （对齐 CHECK ``chk_customer_segment_scope_platform``）。
    """
    _validate_thresholds(thresholds)
    if scope not in (SCOPE_GLOBAL, SCOPE_PLATFORM_LOCAL):
        raise SegmentationError(
            f"未知分层范围 {scope!r}。\n"
            f"  可选值只有：{SCOPE_GLOBAL!r}（全局层）、{SCOPE_PLATFORM_LOCAL!r}（平台局部层）。"
        )
    if scope == SCOPE_PLATFORM_LOCAL and not platform:
        raise SegmentationError(
            "平台局部层必须带 platform。\n"
            f"  scope={SCOPE_PLATFORM_LOCAL!r} 时 platform 不能为空"
            "（表约束 chk_customer_segment_scope_platform）。\n"
            "  请在调用时传入平台标识（如 'taobao'）。"
        )
    if scope == SCOPE_GLOBAL and platform is not None:
        raise SegmentationError(
            f"全局层（scope={SCOPE_GLOBAL!r}）不得带 platform（收到 {platform!r}）。\n"
            "  全局分层是客户级属性；平台局部层级请用 scope="
            f"{SCOPE_PLATFORM_LOCAL!r} 单独计算。"
        )

    amount_tier, amount_score = amount_tier_of(metrics.avg_order_value_cents, thresholds)
    recency_tier, recency_score = recency_tier_of(metrics.days_since_last_purchase, thresholds)
    return Segment(
        scope=scope,
        platform=platform,
        amount_tier=amount_tier,
        recency_tier=recency_tier,
        amount_score=amount_score,
        recency_score=recency_score,
        layer_score=layer_score_of(amount_score, recency_score, thresholds),
    )


def segment_global(metrics: CustomerLayerMetrics, thresholds: SegmentationThresholds) -> Segment:
    """全局层分层（无 platform）。"""
    return segment_customer(metrics, thresholds, scope=SCOPE_GLOBAL)


def segment_platform_local(
    metrics: CustomerLayerMetrics, thresholds: SegmentationThresholds, platform: str
) -> Segment:
    """平台局部层分层（必须带 platform）。"""
    return segment_customer(metrics, thresholds, scope=SCOPE_PLATFORM_LOCAL, platform=platform)


def compute_customer_layers(
    global_metrics: CustomerLayerMetrics,
    platform_metrics: Mapping[str, CustomerLayerMetrics],
    thresholds: SegmentationThresholds,
) -> CustomerLayers:
    """一次算出同一客户的全局层 + 各平台局部层。

    两套体系**各自独立**比较（附录 D 规则 4）——本函数不假设任何 scope 之间的固定序。
    """
    global_profile = LayerProfile(global_metrics, segment_global(global_metrics, thresholds))
    platform_profiles = {
        platform: LayerProfile(metrics, segment_platform_local(metrics, thresholds, platform))
        for platform, metrics in platform_metrics.items()
    }
    return CustomerLayers(global_profile=global_profile, platform_profiles=platform_profiles)


def compare_layer_scores(left: Segment, right: Segment) -> int:
    """按 ``layer_score`` 比较两个层级：>0 左边更高，<0 右边更高，0 平局。

    **只比分数，不看 scope**——附录 D 规则 4 已删除「全局层级恒大于平台层级」的固定序。
    """
    return (left.layer_score > right.layer_score) - (left.layer_score < right.layer_score)


# ---------------------------------------------------------------------------
# 输入度量工具
# ---------------------------------------------------------------------------


def to_cents(amount_yuan: Decimal) -> int:
    """元 → 分（分层阈值的单位是分）。

    四舍五入到分（ROUND_HALF_UP），避免浮点误差把 49999.99 元判成 high。
    """
    if amount_yuan < 0:
        raise SegmentationError(
            f"金额不能为负（收到 {amount_yuan}）。\n  这是数据错误，不是分层结果——请先修上游数据。"
        )
    return int((amount_yuan * _CENTS_PER_YUAN).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def days_since(timestamp: datetime, now: datetime) -> int:
    """距今整天数（向下取整）。

    时间戳必须带时区；未来时间戳视为数据错误（拒绝，不静默当成 0 天）。
    """
    if timestamp.tzinfo is None or now.tzinfo is None:
        raise SegmentationError(
            "时间戳必须带时区（aware datetime）。\n"
            "  请统一用 UTC 或明确的时区偏移，避免 naive/aware 混用算出错误天数。"
        )
    delta = now - timestamp
    if delta.total_seconds() < 0:
        raise SegmentationError(
            f"时间戳在未来（{timestamp.isoformat()} > {now.isoformat()}）。\n"
            "  这是数据错误，不是分层结果——请先修上游数据。"
        )
    return int(delta.total_seconds() // _SECONDS_PER_DAY)
