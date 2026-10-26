"""六条冲突消解规则的**唯一实现点**（附录D 冻结版）。

权威依据：`docs/specs/GovernCRM_附录D_冻结版.md` §1 规则 1–6、§4 规则 ID 登记表与尾注。
阈值唯一来源 = `governance.yaml`（经 `load_and_validate()` 读入），**代码里不写死常量**。

红线与纪律：
- **红线 ①**：纯函数、零 LLM、零框架依赖、零 IO、**不读服务端当前时间**
  ——所有「距今」判定以入参 `now` 为基准（附录D 规则 2「时间锚点」）。
- **红线 ②**：DENY 是终态；`ESCALATE_HUMAN` 只能是 REQUIRE_APPROVAL，**不得**转成 ALLOW。
- **唯一实现点**：抑制 / 频控 / 冲突消解 / 层级 / 净值 / 升级人工的判定逻辑**只在本文件实现一次**，
  `src/decision/` 下不得出现第二份。
- **分层唯一实现点** = `src/unification/segmentation.py`（T-C2）：本文件**只调用**它，
  不另写一份 layer_score 计算。
- **禁止自造 rule_id**：只用附录D §4 登记表的 11 个 ID。

规则与实现对照（评审用）：

| 规则 | 实现位置 | 冻结版条款 |
| --- | --- | --- |
| 1a 抑制红线（四红线） | `_redline_hits` | 附录D §1 规则 1 |
| 1b 无有效营销授权 | `_consent_hits` | 附录D §1 规则 1b |
| 2 全局频控（≤2 语义） | `_evaluate_frequency` | 附录D §1 规则 2 |
| 3 冲突窗口三键排序 | `_conflict_groups` / `_sort_key` | 附录D §1 规则 3 |
| 4 层级分维度取判 | `_layer_evidence` | 附录D §1 规则 4 |
| 5 权益净值折算 | `net_value_cents` | 附录D §1 规则 5 |
| 6 升级人工 | `evaluate`（三键全平分支） | 附录D §1 规则 6 |
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from src.policy_kernel.config import GovernanceConfig, Thresholds
from src.policy_kernel.kernel import Decision, DecisionResult, RuleId
from src.unification.segmentation import (
    SCOPE_GLOBAL,
    SCOPE_PLATFORM_LOCAL,
    CustomerLayerMetrics,
    Segment,
    segment_customer,
)

__all__ = [
    "ActionCandidate",
    "CustomerContext",
    "EvaluationOutcome",
    "EvaluationRequest",
    "ScopeFacts",
    "TouchEvent",
    "evaluate",
    "net_value_cents",
]

#: rule_id 登记顺序（附录D §4）。命中集合按此顺序输出，保证同一输入的输出**稳定可复现**。
_RULE_ORDER: tuple[RuleId, ...] = tuple(RuleId)


# ---------------------------------------------------------------------------
# 输入契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TouchEvent:
    """一次历史触达（规则 2 的输入）。

    `at` 必须带时区；`channel` 与候选的 `channel` 同名即视为**同通道**。
    """

    at: datetime
    channel: str


@dataclass(frozen=True)
class ScopeFacts:
    """一个 scope（全局 / 平台局部）下的客户事实。

    规则 1a / 1b 的判定输入。`suppression_hits` 是**显式给定**的该 scope 已命中的红线
    （如分层引擎在平台局部的判定结果），与字段推导结果取**并集**。
    """

    metrics: CustomerLayerMetrics
    total_spend_percentile: float = 0.0
    complaints: tuple[datetime, ...] = ()
    refund_disputes: tuple[datetime, ...] = ()
    refund_rate: float = 0.0
    explicit_refusal: bool = False
    has_marketing_consent: bool = True
    suppression_hits: tuple[RuleId, ...] = ()


@dataclass(frozen=True)
class ActionCandidate:
    """一条候选动作（规则 3 的排序单元）。

    金额单位 = **分**（与分层阈值同单位）。`platform_facts` 缺省时，平台局部 scope
    沿用全局事实（同一客户在多数平台上的层级与全局一致）。
    """

    candidate_id: str
    platform: str
    coupon_value_cents: int
    coupon_threshold_cents: int
    created_at: datetime
    channel: str
    platform_facts: ScopeFacts | None = None


@dataclass(frozen=True)
class CustomerContext:
    """客户上下文：全局事实 + 触达历史（客户级、全平台合计）。"""

    global_facts: ScopeFacts
    touches: tuple[TouchEvent, ...] = ()


@dataclass(frozen=True)
class EvaluationRequest:
    """一次判定请求（全部输入，无隐式状态、无 IO）。"""

    customer: CustomerContext
    candidates: tuple[ActionCandidate, ...]
    now: datetime
    config: GovernanceConfig


@dataclass(frozen=True)
class EvaluationOutcome:
    """判定结果：逐候选结论 + 放行候选（`None` = 无候选放行：全部 DENY 或升级人工）。"""

    results: Mapping[str, DecisionResult] = field(default_factory=dict[str, DecisionResult])
    allowed_candidate_id: str | None = None


@dataclass(frozen=True)
class _FrequencyVerdict:
    """规则 2 的判定结果（延迟类）。"""

    denied: bool
    cooling_remaining_seconds: int | None = None
    channel_cooling_remaining_seconds: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _CandidateContext:
    """候选在本次判定中的中间量（分层结果 + 权益净值 + 过程性证据）。"""

    candidate: ActionCandidate
    global_segment: Segment
    platform_segment: Segment
    net_value_cents: int
    evidence: Mapping[str, Any] = field(default_factory=dict[str, Any])


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _order_rules(rules: Iterable[RuleId]) -> tuple[RuleId, ...]:
    """按附录D §4 登记顺序输出命中集合（去重、顺序稳定）。"""
    unique = set(rules)
    return tuple(rule for rule in _RULE_ORDER if rule in unique)


def _require_aware(moment: datetime, field_name: str) -> None:
    """时间戳必须带时区——naive/aware 混用会算出错误冷却时间（静默错判比报错更糟）。"""
    if moment.tzinfo is None:
        raise ValueError(
            f"{field_name} 必须带时区（aware datetime），收到 naive datetime。\n"
            "  请统一用 UTC 或明确的时区偏移，避免「距今」判定不可复现。"
        )


def _count_in_window(events: Sequence[datetime], now: datetime, window_days: int) -> int:
    """统计落在「近 window_days 天」窗口内的事件数（闭区间，含边界）。"""
    if window_days <= 0:
        return len(events)
    lower = now - timedelta(days=window_days)
    return sum(1 for at in events if lower <= at <= now)


def _whole_seconds(delta: timedelta) -> int:
    """时间差 → 整秒（向下取整；避免浮点秒的表示误差）。"""
    return delta // timedelta(seconds=1)


# ---------------------------------------------------------------------------
# 规则 5：权益净值折算
# ---------------------------------------------------------------------------


def net_value_cents(
    coupon_value_cents: int,
    coupon_threshold_cents: int,
    avg_order_value_cents: int,
) -> int:
    """权益净值（附录D 规则 5）：

    ``净值 = max(0, 面额 − max(0, 门槛 − 客户近 90 天客单价))``

    **下限 0**——门槛远超客单价时净值为负，负净值无业务含义；
    两个候选净值都为 0 时仍按规则 3 键③（创建时间）裁决。
    """
    gap = max(0, coupon_threshold_cents - avg_order_value_cents)
    return max(0, coupon_value_cents - gap)


# ---------------------------------------------------------------------------
# 规则 1a：抑制红线（四红线）
# ---------------------------------------------------------------------------


def _redline_hits(facts: ScopeFacts, thresholds: Thresholds, now: datetime) -> tuple[RuleId, ...]:
    """规则 1a 的**四红线**判定（附录D §1 规则 1）。

    - `SUP_HIGH_COMPLAINT`：近 `complaint_window_days`（90）天投诉 ≥1 次；
    - `SUP_HIGH_REFUND`：近 `refund_dispute_window_days`（90）天**存在纠纷**，
      **或**退款率超 `refund_rate_threshold`（**固定阈值常量，不做 2σ 统计**）；
    - `SUP_EXPLICIT_REFUSAL`：客户明示拒绝营销（**与授权状态无关**）；
    - `SUP_HIGH_VALUE`：客户消费金额在租户 Top `high_value_percentile`%（1%）。
    """
    hits: list[RuleId] = []

    # Top 1%：百分位越大越靠前（≥ 99.0 即落在 Top 1%）。
    if facts.total_spend_percentile >= 100.0 - thresholds.high_value_percentile:
        hits.append(RuleId.SUP_HIGH_VALUE)

    if _count_in_window(facts.complaints, now, thresholds.complaint_window_days) >= 1:
        hits.append(RuleId.SUP_HIGH_COMPLAINT)

    has_dispute = (
        _count_in_window(facts.refund_disputes, now, thresholds.refund_dispute_window_days) >= 1
    )
    rate_over_threshold = facts.refund_rate > thresholds.refund_rate_threshold
    if has_dispute or rate_over_threshold:
        hits.append(RuleId.SUP_HIGH_REFUND)

    if facts.explicit_refusal:
        hits.append(RuleId.SUP_EXPLICIT_REFUSAL)

    return tuple(hits)


# ---------------------------------------------------------------------------
# 规则 1b：无有效营销授权（发送前校验）
# ---------------------------------------------------------------------------


def _consent_hits(facts: ScopeFacts) -> tuple[RuleId, ...]:
    """规则 1b：客户无有效营销授权 → `NO_CONSENT`（发送前校验阶段判定）。

    与规则 1a **分两步求值**；两者同时命中时**记录全部命中的 rule_id**（附录D 规则 1 求值顺序）。
    """
    if facts.has_marketing_consent:
        return ()
    return (RuleId.NO_CONSENT,)


# ---------------------------------------------------------------------------
# 规则 2：全局频控（≤2 语义）
# ---------------------------------------------------------------------------


def _evaluate_frequency(
    touches: Sequence[TouchEvent],
    channel: str,
    thresholds: Thresholds,
    now: datetime,
) -> _FrequencyVerdict:
    """规则 2（附录D §1 规则 2）：

    - 7 天滚动窗口内全平台合计触达 **≤2 次**（第 2 次合法，第 3 次 DENY）——
      裁决 #12 的「≤2」是**上限**语义，不是「≥2 次 → DENY」；
    - 同一通道两次触达间隔 **≥72 小时**，不足即 DENY。

    返回 `cooling_remaining_seconds`（窗口侧）与 `channel_cooling_remaining_seconds`（通道侧）。
    """
    window = timedelta(days=thresholds.freq_window_days)
    lower = now - window
    in_window = sorted(
        (touch for touch in touches if lower <= touch.at <= now),
        key=lambda touch: touch.at,
    )

    cooling_remaining: int | None = None
    if len(in_window) >= thresholds.freq_max_touches:
        # 窗口内已满额：最早那次触达滑出窗口后即可再触达
        oldest = in_window[0]
        cooling_remaining = _whole_seconds(oldest.at + window - now)

    channel_cooling_remaining: int | None = None
    same_channel = [touch for touch in touches if touch.channel == channel]
    if same_channel:
        latest = max(same_channel, key=lambda touch: touch.at)
        elapsed = now - latest.at
        cooldown = timedelta(hours=thresholds.channel_cooldown_hours)
        if elapsed < cooldown:
            channel_cooling_remaining = _whole_seconds(cooldown - elapsed)

    if cooling_remaining is not None and channel_cooling_remaining is not None:
        reason = (
            f"{thresholds.freq_window_days} 天内触达已达 {thresholds.freq_max_touches} 次上限"
            f"（本次为第 {thresholds.freq_max_touches + 1} 次），且同通道冷却未满"
        )
    elif cooling_remaining is not None:
        reason = (
            f"{thresholds.freq_window_days} 天内触达已达 {thresholds.freq_max_touches} 次上限"
            f"（本次为第 {thresholds.freq_max_touches + 1} 次）"
        )
    elif channel_cooling_remaining is not None:
        reason = f"同通道冷却未满 {thresholds.channel_cooldown_hours} 小时"
    else:
        reason = None

    return _FrequencyVerdict(
        denied=cooling_remaining is not None or channel_cooling_remaining is not None,
        cooling_remaining_seconds=cooling_remaining,
        channel_cooling_remaining_seconds=channel_cooling_remaining,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# 规则 4：层级分维度取判
# ---------------------------------------------------------------------------


def _layer_evidence(
    global_segment: Segment, platform_segment: Segment, platform: str
) -> dict[str, Any]:
    """规则 4 的**分维度**取判（附录D §1 规则 4），产出**过程性记录** `LAYER_RESOLVE`。

    - 权益与资源投入 → 取**更高的 layer_score**（对客户更有价值）；
    - 抑制与频控 → 取**更严格**一档（在 `evaluate` 里以「并集 + DENY 优先」实现）；
    - 分层判定 → 取**更严格**一档（保守原则：取更低的一侧）。

    **不存在「全局层级恒大于平台层级」的固定序**——两套层级体系各自按 `layer_score` 比较。
    `LAYER_RESOLVE` 是过程性记录，写进 `evidence`，**不进入 `hit_rule_ids`**（附录D §4 尾注）。
    """
    benefit_scope = (
        SCOPE_GLOBAL
        if global_segment.layer_score >= platform_segment.layer_score
        else (SCOPE_PLATFORM_LOCAL)
    )
    stricter_scope = (
        SCOPE_GLOBAL
        if global_segment.layer_score <= platform_segment.layer_score
        else (SCOPE_PLATFORM_LOCAL)
    )
    return {
        "layer_resolve": {
            "rule_id": RuleId.LAYER_RESOLVE.value,
            "global": {
                "scope": SCOPE_GLOBAL,
                "segment": global_segment.layer_name,
                "layer_score": global_segment.layer_score,
            },
            "platform_local": {
                "scope": SCOPE_PLATFORM_LOCAL,
                "platform": platform,
                "segment": platform_segment.layer_name,
                "layer_score": platform_segment.layer_score,
            },
            "benefit_scope": benefit_scope,
            "benefit_layer_score": max(global_segment.layer_score, platform_segment.layer_score),
            "stricter_scope": stricter_scope,
        }
    }


# ---------------------------------------------------------------------------
# 规则 3：冲突窗口三键排序
# ---------------------------------------------------------------------------


def _conflict_groups(
    candidates: Sequence[ActionCandidate], window_hours: int
) -> list[list[ActionCandidate]]:
    """按 `conflict_window_hours`（72h）把候选切成**冲突组**。

    同一组 = 落在同一 72 小时窗口内的候选（组内首个候选起算）。
    """
    window = timedelta(hours=window_hours)
    ordered = sorted(
        candidates, key=lambda candidate: (candidate.created_at, candidate.candidate_id)
    )
    groups: list[list[ActionCandidate]] = []
    current: list[ActionCandidate] = []
    for candidate in ordered:
        if current and candidate.created_at - current[0].created_at > window:
            groups.append(current)
            current = []
        current.append(candidate)
    if current:
        groups.append(current)
    return groups


def _sort_key(
    candidate: ActionCandidate,
    layer_score: int,
    net_value: int,
    overrides: Mapping[str, int],
) -> tuple[tuple[int, int], int, int, datetime]:
    """规则 3 的排序键（**覆盖优先于三键**）。

    ① `platform_priority_overrides`（**最高优先**）：显式配置了优先级的平台优先，
       数值小者优先；未配置的平台才走三键；
    ② 平台局部 `layer_score`（高者胜）；
    ③ 权益净值（高者胜）；
    ④ 创建时间（早者胜）。

    返回值可直接比较——**四元组全相等即「三键全平」**（触发规则 6 升级人工）。
    """
    override = overrides.get(candidate.platform)
    override_key = (0, override) if override is not None else (1, 0)
    return (override_key, -layer_score, -net_value, candidate.created_at)


# ---------------------------------------------------------------------------
# 判定入口
# ---------------------------------------------------------------------------


def evaluate(request: EvaluationRequest) -> EvaluationOutcome:
    """六条规则的判定入口（架构 v5.2 §4.3 治理闸门 = 唯一决策出口）。

    求值顺序（附录D §1）：

    ① 规则 1a 四红线 + 规则 1b NO_CONSENT → 命中即 DENY，**不进入后续规则**；
       两者同时命中时**记录全部命中的 rule_id**（附录D 规则 1 求值顺序）；
    ② 规则 2 全局频控 → 命中即 DENY（延迟类）；
    ③ 规则 3 冲突窗口排序 → 只放行 1 条，其余 DENY（`CONFLICT_WINDOW_72H`）；
       三键全平 → 规则 6 升级人工（REQUIRE_APPROVAL），**不默认放行**；
    ④ 规则 4 / 规则 5 作为**过程性记录**写入 `evidence`（`LAYER_RESOLVE` / `BENEFIT_MAX`）。

    **纯函数**：无 IO、无随机、**不读服务端当前时间**（时间全部来自 `request.now`）。
    """
    now = request.now
    _require_aware(now, "request.now")
    config = request.config
    thresholds = config.thresholds
    customer = request.customer
    global_facts = customer.global_facts
    global_segment = segment_customer(global_facts.metrics, config.segmentation, scope=SCOPE_GLOBAL)

    results: dict[str, DecisionResult] = {}
    survivors: list[ActionCandidate] = []
    contexts: dict[str, _CandidateContext] = {}

    for candidate in request.candidates:
        platform_facts = (
            candidate.platform_facts if candidate.platform_facts is not None else (global_facts)
        )
        platform_segment = segment_customer(
            platform_facts.metrics,
            config.segmentation,
            scope=SCOPE_PLATFORM_LOCAL,
            platform=candidate.platform,
        )
        net_value = net_value_cents(
            candidate.coupon_value_cents,
            candidate.coupon_threshold_cents,
            global_facts.metrics.avg_order_value_cents,
        )

        # 规则 1a（四红线）+ 规则 1b（NO_CONSENT）——分两步求值，命中集合取并集
        redline_1a = _order_rules(
            set(_redline_hits(global_facts, thresholds, now))
            | set(_redline_hits(platform_facts, thresholds, now))
            | set(global_facts.suppression_hits)
            | set(platform_facts.suppression_hits)
        )
        redline_1b = _order_rules(
            set(_consent_hits(global_facts)) | set(_consent_hits(platform_facts))
        )
        hits = _order_rules(set(redline_1a) | set(redline_1b))

        evidence: dict[str, Any] = {
            "suppression_stages": {
                "1a": [rule.value for rule in redline_1a],
                "1b": [rule.value for rule in redline_1b],
            },
            **_layer_evidence(global_segment, platform_segment, candidate.platform),
            "benefit_max": {
                "rule_id": RuleId.BENEFIT_MAX.value,
                "net_value_cents": net_value,
                "coupon_value_cents": candidate.coupon_value_cents,
                "coupon_threshold_cents": candidate.coupon_threshold_cents,
                "avg_order_value_cents": global_facts.metrics.avg_order_value_cents,
            },
        }
        contexts[candidate.candidate_id] = _CandidateContext(
            candidate=candidate,
            global_segment=global_segment,
            platform_segment=platform_segment,
            net_value_cents=net_value,
            evidence=evidence,
        )

        if hits:
            results[candidate.candidate_id] = DecisionResult(
                conclusion=Decision.DENY,
                hit_rule_ids=hits,
                evidence={**evidence, "manual_follow_up": RuleId.SUP_HIGH_VALUE in hits},
                reason=f"命中抑制红线：{[rule.value for rule in hits]}",
            )
            continue

        frequency = _evaluate_frequency(customer.touches, candidate.channel, thresholds, now)
        if frequency.denied:
            results[candidate.candidate_id] = DecisionResult(
                conclusion=Decision.DENY,
                hit_rule_ids=(RuleId.FREQ_GLOBAL_7D,),
                evidence=evidence,
                cooling_remaining_seconds=frequency.cooling_remaining_seconds,
                channel_cooling_remaining_seconds=frequency.channel_cooling_remaining_seconds,
                reason=frequency.reason,
            )
            continue

        survivors.append(candidate)

    allowed_candidate_id: str | None = None
    overrides = config.conflict_resolution.platform_priority_overrides
    for group in _conflict_groups(survivors, thresholds.conflict_window_hours):
        if len(group) == 1:
            candidate = group[0]
            results[candidate.candidate_id] = DecisionResult(
                conclusion=Decision.ALLOW,
                hit_rule_ids=(),
                suppressed_rule_ids=(),
                suppressed_candidates=0,
                evidence=dict(contexts[candidate.candidate_id].evidence),
                reason="无抑制 / 无频控 / 无冲突 → 放行",
            )
            allowed_candidate_id = candidate.candidate_id
            continue

        keys = {
            candidate.candidate_id: _sort_key(
                candidate,
                contexts[candidate.candidate_id].platform_segment.layer_score,
                contexts[candidate.candidate_id].net_value_cents,
                overrides,
            )
            for candidate in group
        }
        ranked = sorted(group, key=lambda candidate: keys[candidate.candidate_id])

        if keys[ranked[0].candidate_id] == keys[ranked[1].candidate_id]:
            # 三键全平 → 规则 6：全部候选挂起，升级人工（E5），**不默认放行**
            snapshot = {
                "conflict_window_hours": thresholds.conflict_window_hours,
                "candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "platform": candidate.platform,
                        "layer_score": contexts[
                            candidate.candidate_id
                        ].platform_segment.layer_score,
                        "net_value_cents": contexts[candidate.candidate_id].net_value_cents,
                        "created_at": candidate.created_at.isoformat(),
                    }
                    for candidate in group
                ],
            }
            for candidate in group:
                results[candidate.candidate_id] = DecisionResult(
                    conclusion=Decision.REQUIRE_APPROVAL,
                    hit_rule_ids=(RuleId.ESCALATE_HUMAN,),
                    suppressed_candidates=0,
                    evidence={**snapshot, "rule_id": RuleId.ESCALATE_HUMAN.value},
                    reason="三键全平，无法自动裁决 → 升级人工（规则 6）",
                )
            continue

        winner = ranked[0]
        losers = ranked[1:]
        results[winner.candidate_id] = DecisionResult(
            conclusion=Decision.ALLOW,
            hit_rule_ids=(),
            suppressed_rule_ids=(RuleId.CONFLICT_WINDOW_72H,),
            suppressed_candidates=len(losers),
            evidence={
                **contexts[winner.candidate_id].evidence,
                "conflict_window": {
                    "rule_id": RuleId.CONFLICT_WINDOW_72H.value,
                    "suppressed_candidate_ids": [loser.candidate_id for loser in losers],
                    "winner_candidate_id": winner.candidate_id,
                },
            },
            reason=f"冲突窗口内优先级最高（键：{keys[winner.candidate_id][:3]}）",
        )
        allowed_candidate_id = winner.candidate_id
        for loser in losers:
            results[loser.candidate_id] = DecisionResult(
                conclusion=Decision.DENY,
                hit_rule_ids=(RuleId.CONFLICT_WINDOW_72H,),
                suppressed_candidates=0,
                evidence={
                    **contexts[loser.candidate_id].evidence,
                    "conflict_window": {
                        "rule_id": RuleId.CONFLICT_WINDOW_72H.value,
                        "winner_candidate_id": winner.candidate_id,
                    },
                },
                reason="同一冲突窗口内优先级低于放行候选 → 抑制",
            )

    return EvaluationOutcome(results=results, allowed_candidate_id=allowed_candidate_id)
