"""T-B2 六条规则实现测试（附录D 冻结版）。

依据（每条测试的 docstring 写明可反查的权威条款）：
- `docs/specs/GovernCRM_附录D_冻结版.md` §1 规则 1–6、§4 规则 ID 登记表与三要素分离尾注；
- `docs/specs/评测标注基准_v1.3.yaml`（T-G1 已入库的 `tests/evaluation/benchmark.yaml` 逐字副本）
  —— 20 条人工标注样本（S01–S15 简单 + B01–B05 边界）；
- `docs/specs/GovernCRM_技术架构方案_v5.2.md` §4.3（治理闸门 = 唯一决策出口）、§4.4-9（路径断言）。

纪律：
- **标注先于内核**（附录D §3 标注顺序 ②→③）：本文件的期望值来自人工标注的
  `tests/evaluation/benchmark.yaml`，**不得**由内核输出反推。
- **测试即规格**：每条测试的 docstring 写明依据条款，人可反查。
- 负向用例**不许 skip**：环境缺失用 `pytest.fail`。
"""

from __future__ import annotations

import ast
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from src.policy_kernel.config import GovernanceConfig, load_and_validate
from src.policy_kernel.kernel import Decision, DecisionResult, RuleId
from src.policy_kernel.rules import (
    ActionCandidate,
    CustomerContext,
    EvaluationOutcome,
    EvaluationRequest,
    ScopeFacts,
    TouchEvent,
    evaluate,
    net_value_cents,
)
from src.unification.segmentation import CustomerLayerMetrics, segment_global

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_YAML = ROOT / "governance.yaml"
BENCHMARK_YAML = ROOT / "tests" / "evaluation" / "benchmark.yaml"
FROZEN_BENCHMARK_YAML = ROOT / "docs" / "specs" / "评测标注基准_v1.3.yaml"
RULES_PY = ROOT / "src" / "policy_kernel" / "rules.py"

NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)

#: 九宫格档位 → 代表性度量（测试侧把冻结标注的层名还原成分层输入）。
#: 选取各档**明确落在档内**的取值：金额 high=500 元 / mid=200 元 / low=50 元；
#: 近度 recent=10 天 / mid=45 天 / stale=120 天。
_AMOUNT_REPRESENTATIVE_CENTS = {"high": 50000, "mid": 20000, "low": 5000}
_RECENCY_REPRESENTATIVE_DAYS = {"recent": 10, "mid": 45, "stale": 120}


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def config() -> GovernanceConfig:
    """治理配置（阈值唯一来源 = governance.yaml）。"""
    if not GOVERNANCE_YAML.exists():
        pytest.fail(f"找不到 {GOVERNANCE_YAML}：策略参数唯一来源缺失，内核无法取阈值。")
    return load_and_validate(GOVERNANCE_YAML)


@pytest.fixture(scope="session")
def benchmark() -> dict[str, Any]:
    """T-G1 落地的评测标注夹具（人工标注，20 条）。"""
    if not BENCHMARK_YAML.exists():
        pytest.fail(f"找不到 {BENCHMARK_YAML}：评测标注夹具缺失（T-G1 产物）。")
    data = yaml.safe_load(BENCHMARK_YAML.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        pytest.fail(f"{BENCHMARK_YAML} 顶层不是映射。")
    return data


def _dt(value: str) -> datetime:
    """解析带 Z 的 ISO 时间戳（统一 UTC）。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _metrics(avg_order_value_cents: int, days_since_last_purchase: int) -> CustomerLayerMetrics:
    return CustomerLayerMetrics(
        avg_order_value_cents=avg_order_value_cents,
        days_since_last_purchase=days_since_last_purchase,
    )


def _metrics_for_layer(layer_name: str) -> CustomerLayerMetrics:
    """把冻结标注的九宫格层名（如 ``amount_mid_recent``）还原成分层输入。"""
    parts = layer_name.split("_")
    if len(parts) != 3 or parts[0] != "amount":
        pytest.fail(f"无法解析层名 {layer_name!r}（期望 amount_<档>_<档>）。")
    _, amount, recency = parts
    return CustomerLayerMetrics(
        avg_order_value_cents=_AMOUNT_REPRESENTATIVE_CENTS[amount],
        days_since_last_purchase=_RECENCY_REPRESENTATIVE_DAYS[recency],
    )


def _facts(
    *,
    avg_order_value_cents: int = 20000,
    days_since_last_purchase: int = 20,
    total_spend_percentile: float = 50.0,
    complaints: tuple[datetime, ...] = (),
    refund_disputes: tuple[datetime, ...] = (),
    refund_rate: float = 0.01,
    explicit_refusal: bool = False,
    has_marketing_consent: bool = True,
    suppression_hits: tuple[RuleId, ...] = (),
) -> ScopeFacts:
    return ScopeFacts(
        metrics=_metrics(avg_order_value_cents, days_since_last_purchase),
        total_spend_percentile=total_spend_percentile,
        complaints=complaints,
        refund_disputes=refund_disputes,
        refund_rate=refund_rate,
        explicit_refusal=explicit_refusal,
        has_marketing_consent=has_marketing_consent,
        suppression_hits=suppression_hits,
    )


def _candidate(
    candidate_id: str = "c1",
    *,
    platform: str = "taobao",
    coupon_value_cents: int = 2000,
    coupon_threshold_cents: int = 10000,
    created_at: datetime = datetime(2026, 10, 23, 10, 0, tzinfo=UTC),
    channel: str = "sms",
    platform_facts: ScopeFacts | None = None,
) -> ActionCandidate:
    return ActionCandidate(
        candidate_id=candidate_id,
        platform=platform,
        coupon_value_cents=coupon_value_cents,
        coupon_threshold_cents=coupon_threshold_cents,
        created_at=created_at,
        channel=channel,
        platform_facts=platform_facts,
    )


def _request(
    facts: ScopeFacts,
    candidates: tuple[ActionCandidate, ...],
    *,
    touches: tuple[TouchEvent, ...] = (),
    now: datetime = NOW,
    cfg: GovernanceConfig | None = None,
) -> EvaluationRequest:
    if cfg is None:
        cfg = load_and_validate(GOVERNANCE_YAML)
    return EvaluationRequest(
        customer=CustomerContext(global_facts=facts, touches=touches),
        candidates=candidates,
        now=now,
        config=cfg,
    )


def _only(outcome: EvaluationOutcome) -> DecisionResult:
    """取唯一候选的判定结果。"""
    assert len(outcome.results) == 1, f"期望恰好 1 个候选结果，实际 {len(outcome.results)}"
    return next(iter(outcome.results.values()))


# ---------------------------------------------------------------------------
# 一、规则 1a：抑制红线（四红线）
# ---------------------------------------------------------------------------


def test_sup_high_complaint_denies_customer_with_complaint_in_90_days() -> None:
    """附录D 规则 1a：客户近 **90 天**投诉 ≥1 次 → DENY（rule_id=SUP_HIGH_COMPLAINT）。

    投诉窗口是 **90 天**不是 30 天：本用例的投诉发生在 **45 天前**——
    落在 90 天窗口内、落在 30 天窗口外。按 30 天实现会误放行，故本用例对窗口口径有判别力。
    """
    facts = _facts(complaints=(NOW - timedelta(days=45),))
    outcome = evaluate(_request(facts, (_candidate(),)))
    result = _only(outcome)

    assert result.conclusion == Decision.DENY
    assert result.hit_rule_ids == (RuleId.SUP_HIGH_COMPLAINT,)
    assert RuleId.SUP_HIGH_COMPLAINT in result.hit_rule_ids


def test_sup_high_complaint_ignores_complaints_outside_window() -> None:
    """附录D 规则 1a：投诉窗口 = `thresholds.complaint_window_days`（90 天）。

    窗口外的投诉（91 天前）**不**命中——否则红线会被无限期触发。
    """
    facts = _facts(complaints=(NOW - timedelta(days=91),))
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.ALLOW
    assert RuleId.SUP_HIGH_COMPLAINT not in result.hit_rule_ids


def test_sup_high_refund_denies_when_refund_rate_over_threshold() -> None:
    """附录D 规则 1a：退款率超阈值 → DENY（rule_id=SUP_HIGH_REFUND）。

    阈值 = `thresholds.refund_rate_threshold`（0.20），**固定阈值常量，不做 2σ 统计**。
    """
    facts = _facts(refund_rate=0.35)
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.DENY
    assert result.hit_rule_ids == (RuleId.SUP_HIGH_REFUND,)


def test_sup_high_refund_denies_on_dispute_within_window() -> None:
    """附录D 规则 1a：近 90 天**存在纠纷**，**或**退款率超阈值 → DENY。

    本用例退款率仅 0.03（未超阈值），仅靠「近 90 天有纠纷」命中——验证两个条件是**或**关系。
    """
    facts = _facts(refund_rate=0.03, refund_disputes=(NOW - timedelta(days=10),))
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.DENY
    assert result.hit_rule_ids == (RuleId.SUP_HIGH_REFUND,)


def test_sup_high_refund_allows_when_rate_at_threshold_and_no_dispute() -> None:
    """附录D 规则 1a：退款率**等于**阈值不算命中（判定是「超阈值」= `>`）。"""
    facts = _facts(refund_rate=0.20)
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.ALLOW
    assert RuleId.SUP_HIGH_REFUND not in result.hit_rule_ids


def test_sup_explicit_refusal_denies_even_with_consent() -> None:
    """附录D 规则 1a：客户明示拒绝营销 → DENY（rule_id=SUP_EXPLICIT_REFUSAL）。

    明示拒绝**与授权状态无关**——有 `has_marketing_consent=True` 也照样 DENY。
    """
    facts = _facts(explicit_refusal=True, has_marketing_consent=True)
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.DENY
    assert result.hit_rule_ids == (RuleId.SUP_EXPLICIT_REFUSAL,)


def test_sup_high_value_denies_and_escalates_to_human() -> None:
    """附录D 规则 1a：客户消费金额在租户 Top 1% → DENY **+ 转人工跟进队列**。

    「转人工跟进」不是放行——判定仍是 DENY（红线②：DENY 不可绕过），
    但必须留下「需人工跟进」的证据，供跟进队列取用。
    """
    facts = _facts(total_spend_percentile=99.5)
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.DENY
    assert result.hit_rule_ids == (RuleId.SUP_HIGH_VALUE,)
    assert result.evidence.get("manual_follow_up") is True


def test_sup_high_value_not_triggered_outside_top_percentile() -> None:
    """附录D 规则 1a：Top 1% 之外的高消费客户**不**命中 SUP_HIGH_VALUE。"""
    facts = _facts(total_spend_percentile=85.0)
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.ALLOW
    assert RuleId.SUP_HIGH_VALUE not in result.hit_rule_ids


def test_multiple_redlines_record_all_hit_rule_ids() -> None:
    """附录D 规则 1「求值顺序」：四红线与 NO_CONSENT 均命中时，**记录全部命中的 rule_id**。

    判定结果同为 DENY，但写 `suppression_record` 时按命中列表**全量记录**，不只记第一个。
    """
    facts = _facts(explicit_refusal=True, has_marketing_consent=False)
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.DENY
    assert set(result.hit_rule_ids) == {RuleId.SUP_EXPLICIT_REFUSAL, RuleId.NO_CONSENT}
    assert len(result.hit_rule_ids) == 2, "多命中必须全量记录，不能只记第一个"


def test_no_consent_denies_at_pre_send_stage() -> None:
    """附录D 规则 1b：客户无有效营销授权 → DENY（rule_id=NO_CONSENT）。

    规则 1b 在**发送前校验**阶段判定，与规则 1a **分两步求值**：
    证据里的 `suppression_stages` 必须把 1a 与 1b 的命中分开记。
    """
    facts = _facts(has_marketing_consent=False)
    result = _only(evaluate(_request(facts, (_candidate(),))))

    assert result.conclusion == Decision.DENY
    assert result.hit_rule_ids == (RuleId.NO_CONSENT,)
    stages = result.evidence.get("suppression_stages")
    assert isinstance(stages, dict), "证据缺少 suppression_stages（1a / 1b 分步求值）"
    assert RuleId.NO_CONSENT.value in stages["1b"]
    assert RuleId.NO_CONSENT.value not in stages["1a"]


def test_redline_deny_short_circuits_frequency_rule() -> None:
    """附录D 规则 1：「直接 DENY，不进入后续规则」。

    客户既命中红线又已触达 3 次（频控 DENY 条件）→ 只记红线 rule_id，
    不得把 FREQ_GLOBAL_7D 混进 hit_rule_ids（否则「命中集合」不可举证）。
    """
    facts = _facts(explicit_refusal=True)
    touches = (
        TouchEvent(at=NOW - timedelta(days=2), channel="sms"),
        TouchEvent(at=NOW - timedelta(days=1), channel="sms"),
    )
    result = _only(evaluate(_request(facts, (_candidate(),), touches=touches)))

    assert result.conclusion == Decision.DENY
    assert result.hit_rule_ids == (RuleId.SUP_EXPLICIT_REFUSAL,)
    assert RuleId.FREQ_GLOBAL_7D not in result.hit_rule_ids


# ---------------------------------------------------------------------------
# 二、规则 2：全局频控（≤2 语义）
# ---------------------------------------------------------------------------


def test_freq_global_7d_allows_second_touch_and_denies_third() -> None:
    """附录D 规则 2：7 天滚动窗口内全平台合计触达 **≤2 次**（第 2 次合法，第 3 次 DENY）。

    裁决 #12 原文是「≤2 次」（**上限**语义）——不是架构草案的「≥2 次 → DENY」。
    本用例同时验证两种情形：已有 1 次 → 本次第 2 次**放行**；已有 2 次 → 本次第 3 次**DENY**。
    """
    facts = _facts()
    one_touch = (TouchEvent(at=NOW - timedelta(days=1), channel="email"),)
    two_touches = (
        TouchEvent(at=NOW - timedelta(days=2), channel="email"),
        TouchEvent(at=NOW - timedelta(days=1), channel="email"),
    )

    second = _only(evaluate(_request(facts, (_candidate(),), touches=one_touch)))
    third = _only(evaluate(_request(facts, (_candidate(),), touches=two_touches)))

    assert second.conclusion == Decision.ALLOW, "第 2 次触达必须合法（≤2 语义）"
    assert RuleId.FREQ_GLOBAL_7D not in second.hit_rule_ids
    assert third.conclusion == Decision.DENY
    assert third.hit_rule_ids == (RuleId.FREQ_GLOBAL_7D,)
    assert third.cooling_remaining_seconds is not None
    assert third.cooling_remaining_seconds > 0


def test_same_channel_touch_within_72h_denies() -> None:
    """附录D 规则 2：同一通道两次触达间隔 **≥72 小时**；不足 72h → DENY。

    返回值 = `channel_cooling_remaining_seconds`（延迟类，供 `suppression_record` 同名列）。
    """
    facts = _facts()
    touches = (TouchEvent(at=NOW - timedelta(hours=27), channel="sms"),)
    result = _only(evaluate(_request(facts, (_candidate(channel="sms"),), touches=touches)))

    assert result.conclusion == Decision.DENY
    assert result.hit_rule_ids == (RuleId.FREQ_GLOBAL_7D,)
    assert result.channel_cooling_remaining_seconds == 72 * 3600 - 27 * 3600


def test_same_channel_touch_exactly_72h_is_allowed() -> None:
    """附录D 规则 2：间隔**恰为 72 小时**即满足「≥72 小时」，不 DENY。"""
    facts = _facts()
    touches = (TouchEvent(at=NOW - timedelta(hours=72), channel="sms"),)
    result = _only(evaluate(_request(facts, (_candidate(channel="sms"),), touches=touches)))

    assert result.conclusion == Decision.ALLOW
    assert result.channel_cooling_remaining_seconds is None


def test_cross_channel_touch_does_not_trigger_cooldown() -> None:
    """附录D 规则 2：同通道冷却只比较**同通道**的上次触达；跨通道不触发。"""
    facts = _facts()
    touches = (TouchEvent(at=NOW - timedelta(hours=1), channel="email"),)
    result = _only(evaluate(_request(facts, (_candidate(channel="sms"),), touches=touches)))

    assert result.conclusion == Decision.ALLOW
    assert RuleId.FREQ_GLOBAL_7D not in result.hit_rule_ids


def test_frequency_window_excludes_touches_older_than_window() -> None:
    """附录D 规则 2：频控窗口 = `thresholds.freq_window_days`（7 天）。

    8 天前的触达**不计入**窗口（否则频控会被无限期触发）。
    """
    facts = _facts()
    touches = (
        TouchEvent(at=NOW - timedelta(days=8), channel="email"),
        TouchEvent(at=NOW - timedelta(days=7, hours=1), channel="email"),
    )
    result = _only(evaluate(_request(facts, (_candidate(),), touches=touches)))

    assert result.conclusion == Decision.ALLOW
    assert RuleId.FREQ_GLOBAL_7D not in result.hit_rule_ids


def test_threshold_evaluation_uses_supplied_now_not_server_clock() -> None:
    """附录D 规则 2「时间锚点」：所有「距今」判定以**入参 `now`** 为基准。

    同一份触达历史，`now` 前移 3 天后第 3 次触达不再超限 → 证明内核用的是入参时间，
    而不是服务端当前时间（红线①：纯函数、无隐式时钟）。
    """
    facts = _facts()
    touches = (
        TouchEvent(at=NOW - timedelta(days=6), channel="email"),
        TouchEvent(at=NOW - timedelta(days=5), channel="email"),
    )
    anchored = _only(evaluate(_request(facts, (_candidate(),), touches=touches, now=NOW)))
    shifted = _only(
        evaluate(_request(facts, (_candidate(),), touches=touches, now=NOW + timedelta(days=2)))
    )

    assert anchored.conclusion == Decision.DENY
    assert shifted.conclusion == Decision.ALLOW


# ---------------------------------------------------------------------------
# 三、规则 3：冲突窗口三键排序
# ---------------------------------------------------------------------------


def _conflict_facts() -> ScopeFacts:
    """冲突用例的公共客户事实：金额 mid（200 元）、近度 recent（10 天）→ 全局 mid_recent。"""
    return _facts(avg_order_value_cents=20000, days_since_last_purchase=10)


def test_conflict_window_selects_highest_platform_local_layer_score() -> None:
    """附录D 规则 3 键①：**平台局部** layer_score 高者胜。

    全局分层是客户级属性，同客户多候选必然相同 → 键①必须用**平台局部层级**比较。
    本用例两候选的平台局部层级分分别为 23（mid_recent）与 13（low_recent），淘系胜出。
    """
    facts = _conflict_facts()
    taobao = _candidate(
        "taobao",
        platform="taobao",
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    douyin = _candidate(
        "douyin",
        platform="douyin",
        created_at=NOW - timedelta(hours=3),
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_low_recent")),
    )
    outcome = evaluate(_request(facts, (taobao, douyin)))

    assert outcome.allowed_candidate_id == "taobao"
    winner = outcome.results["taobao"]
    assert winner.conclusion == Decision.ALLOW
    assert winner.suppressed_rule_ids == (RuleId.CONFLICT_WINDOW_72H,)
    assert winner.suppressed_candidates == 1


def test_conflict_window_breaks_tie_by_net_value_then_creation_time() -> None:
    """附录D 规则 3 键②③：层级相同 → 比**权益净值**；净值相同 → 比**创建时间（早者胜）**。"""
    facts = _conflict_facts()
    taobao = _candidate(
        "taobao",
        platform="taobao",
        coupon_value_cents=3000,
        coupon_threshold_cents=15000,
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    douyin = _candidate(
        "douyin",
        platform="douyin",
        coupon_value_cents=3000,
        coupon_threshold_cents=30000,
        created_at=NOW - timedelta(hours=3),
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    outcome = evaluate(_request(facts, (taobao, douyin)))

    # 净值：淘系 = 3000 − max(0, 15000 − 20000) = 3000；抖店 = 3000 − 10000 → 下限 0 → 0
    assert outcome.allowed_candidate_id == "taobao"

    # 净值也相同 → 创建时间早者胜
    early = _candidate(
        "douyin-early",
        platform="douyin",
        created_at=NOW - timedelta(hours=3),
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    late = _candidate(
        "taobao-late",
        platform="taobao",
        created_at=NOW - timedelta(hours=1),
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    outcome2 = evaluate(_request(facts, (late, early)))
    assert outcome2.allowed_candidate_id == "douyin-early"


def test_platform_priority_overrides_beat_three_keys() -> None:
    """附录D 规则 3「优先级覆盖」：`platform_priority_overrides` **优先于三键**。

    覆盖项指向的候选**不是**三键（键③早者胜）的赢家——二者反向，
    因此本用例对「覆盖优先于三键」具备判别力（覆盖失效时本用例会失败）。
    """
    facts = _conflict_facts()
    cfg = _config_with_priority_overrides({"taobao": 200})

    taobao = _candidate(
        "taobao",
        platform="taobao",
        created_at=NOW - timedelta(hours=1),
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    douyin = _candidate(
        "douyin",
        platform="douyin",
        created_at=NOW - timedelta(hours=3),
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    outcome = evaluate(_request(facts, (taobao, douyin), cfg=cfg))

    assert outcome.allowed_candidate_id == "taobao"


def test_suppressed_rule_ids_kept_separate_from_hit_rule_ids() -> None:
    """附录D §4 尾注：`hit_rule_ids` 与 `suppressed_rule_ids` **必须分开记**。

    `CONFLICT_WINDOW_72H` 表示「**其他**候选被抑制」，不得混进本候选的命中集合。
    """
    facts = _conflict_facts()
    taobao = _candidate(
        "taobao",
        platform="taobao",
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    douyin = _candidate(
        "douyin",
        platform="douyin",
        created_at=NOW - timedelta(hours=3),
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_low_recent")),
    )
    outcome = evaluate(_request(facts, (taobao, douyin)))

    winner = outcome.results["taobao"]
    loser = outcome.results["douyin"]

    assert winner.hit_rule_ids == (), "放行候选不得带 CONFLICT_WINDOW_72H 命中"
    assert winner.suppressed_rule_ids == (RuleId.CONFLICT_WINDOW_72H,)
    assert loser.hit_rule_ids == (RuleId.CONFLICT_WINDOW_72H,)
    assert loser.suppressed_rule_ids == (), "被抑制候选不再抑制别人"
    assert set(winner.hit_rule_ids).isdisjoint(winner.suppressed_rule_ids)


# ---------------------------------------------------------------------------
# 四、规则 4 / 规则 5：层级取判与权益净值
# ---------------------------------------------------------------------------


def test_layer_resolve_takes_higher_score_for_benefit_and_stricter_for_suppression() -> None:
    """附录D 规则 4「分维度取判」：权益取**更高 layer_score**，抑制/频控取**更严格**一档。

    本用例：全局层 mid_recent（23）> 平台局部层 low_recent（13）→ 权益取**全局**层；
    平台局部 scope 显式命中红线 → 抑制取**更严**一档 → 最终 DENY。
    """
    facts = _facts(
        avg_order_value_cents=20000,
        days_since_last_purchase=10,
        total_spend_percentile=50.0,
    )
    platform_facts = replace(
        facts,
        metrics=_metrics_for_layer("amount_low_recent"),
        suppression_hits=(RuleId.SUP_HIGH_COMPLAINT,),
    )
    candidate = _candidate(platform_facts=platform_facts)
    result = _only(evaluate(_request(facts, (candidate,))))

    layer = result.evidence.get("layer_resolve")
    assert isinstance(layer, dict)
    assert layer["benefit_scope"] == "global", (
        "权益应取更高 layer_score 的一侧（全局 23 > 平台 13）"
    )
    assert layer["benefit_layer_score"] == 23
    assert result.conclusion == Decision.DENY, "抑制取严：平台局部命中红线 → DENY"
    assert RuleId.SUP_HIGH_COMPLAINT in result.hit_rule_ids


def test_layer_resolve_takes_platform_local_when_higher() -> None:
    """附录D 规则 4：平台局部分高于全局时，权益取**平台**层（无「全局恒大于平台」固定序）。"""
    facts = _facts(
        avg_order_value_cents=20000,
        days_since_last_purchase=10,
        total_spend_percentile=50.0,
    )
    platform_facts = replace(facts, metrics=_metrics_for_layer("amount_high_recent"))
    candidate = _candidate(platform_facts=platform_facts)
    result = _only(evaluate(_request(facts, (candidate,))))

    layer = result.evidence.get("layer_resolve")
    assert isinstance(layer, dict)
    assert layer["benefit_scope"] == "platform_local"
    assert layer["benefit_layer_score"] == 33
    assert result.conclusion == Decision.ALLOW


def test_benefit_net_value_formula_floors_at_zero() -> None:
    """附录D 规则 5：净值 = `max(0, 面额 − max(0, 门槛 − 客单价))`，**下限 0**。

    门槛远超客单价时净值会为负——负净值无业务含义，置 0。
    """
    # 客单价 50 元；券（满 200 减 20）→ 20 − 150 = −130 → 0
    assert net_value_cents(2000, 20000, 5000) == 0
    # 券（满 50 减 15）→ 15 − max(0, 50 − 80) = 15
    assert net_value_cents(1500, 5000, 8000) == 1500
    # 券（满 100 减 20），客单价 80 元 → 20 − 20 = 0
    assert net_value_cents(2000, 10000, 8000) == 0


def test_benefit_max_is_recorded_as_evidence_not_hit() -> None:
    """附录D §4 尾注：`BENEFIT_MAX` / `LAYER_RESOLVE` 是**过程性记录**，不独立决定判定。

    它们的 rule_id 写进 `decision.evidence`，**不得**进入 `hit_rule_ids`。
    """
    facts = _conflict_facts()
    candidate = _candidate()
    result = _only(evaluate(_request(facts, (candidate,))))

    assert result.conclusion == Decision.ALLOW
    assert result.hit_rule_ids == ()
    benefit = result.evidence.get("benefit_max")
    assert isinstance(benefit, dict)
    assert benefit["rule_id"] == RuleId.BENEFIT_MAX.value
    assert benefit["net_value_cents"] == 2000


# ---------------------------------------------------------------------------
# 五、规则 6：升级人工
# ---------------------------------------------------------------------------


def test_three_keys_all_equal_escalates_to_human_not_allow() -> None:
    """附录D 规则 6：前五条无法裁决 → **全部候选挂起**升级人工（E5），**不默认放行**。

    本用例三键全平（层级同 + 净值同 + 创建时间同）→ 结论必须是 REQUIRE_APPROVAL，
    rule_id=ESCALATE_HUMAN，**不得**出现 ALLOW（红线②：不得默认放行）。
    """
    facts = _conflict_facts()
    created = datetime(2026, 10, 23, 10, 0, 0, tzinfo=UTC)
    taobao = _candidate(
        "taobao",
        platform="taobao",
        created_at=created,
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    douyin = _candidate(
        "douyin",
        platform="douyin",
        created_at=created,
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    outcome = evaluate(_request(facts, (taobao, douyin)))

    assert outcome.allowed_candidate_id is None, "三键全平不得默认放行"
    for result in outcome.results.values():
        assert result.conclusion == Decision.REQUIRE_APPROVAL
        assert result.hit_rule_ids == (RuleId.ESCALATE_HUMAN,)
        assert result.suppressed_candidates == 0


def test_escalate_human_is_never_turned_into_allow() -> None:
    """红线 ②：`ESCALATE_HUMAN` 只能是 REQUIRE_APPROVAL，**不得**转成 ALLOW。

    本用例在同客户同时叠加「明示拒绝」红线，验证升级人工也不会被「顺手放行」。
    """
    facts = replace(_conflict_facts(), explicit_refusal=True)
    created = datetime(2026, 10, 23, 10, 0, 0, tzinfo=UTC)
    taobao = _candidate(
        "taobao",
        platform="taobao",
        created_at=created,
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    douyin = _candidate(
        "douyin",
        platform="douyin",
        created_at=created,
        platform_facts=replace(facts, metrics=_metrics_for_layer("amount_mid_recent")),
    )
    outcome = evaluate(_request(facts, (taobao, douyin)))

    for result in outcome.results.values():
        assert result.conclusion != Decision.ALLOW
        assert result.conclusion in (Decision.DENY, Decision.REQUIRE_APPROVAL)


# ---------------------------------------------------------------------------
# 六、评测基准回放（20 条人工标注样本）
# ---------------------------------------------------------------------------


def _build_touches(sample: dict[str, Any], now: datetime) -> tuple[TouchEvent, ...]:
    """把冻结标注的 `touch_history_7d` + `last_touch_at` 还原成触达事件序列。

    标注只给了**窗口内次数**与**最近一次**的时间/通道；其余次触达按更早的时间补齐
    （落在同一窗口内）——窗口内**次数**与**最近一次**才是规则的输入。
    """
    count = int(sample["touch_history_7d"])
    if count == 0:
        return ()
    last_at = _dt(sample["last_touch_at"])
    channel = sample["last_touch_channel"]
    events = [
        TouchEvent(at=last_at - timedelta(days=offset), channel=channel)
        for offset in range(count - 1, 0, -1)
    ]
    events.append(TouchEvent(at=last_at, channel=channel))
    return tuple(events)


def _build_request(
    sample: dict[str, Any],
    cfg: GovernanceConfig,
    now: datetime,
) -> tuple[EvaluationRequest, dict[str, ActionCandidate]]:
    customer = sample["customer"]
    global_metrics = _metrics(
        int(customer["avg_order_value_90d"] * 100),
        int(customer["last_purchase_days_ago"]),
    )
    complaints = tuple(
        now - timedelta(days=10) for _ in range(int(customer["complaint_count_90d"]))
    )
    global_facts = ScopeFacts(
        metrics=global_metrics,
        total_spend_percentile=float(customer["total_spend_percentile"]),
        complaints=complaints,
        refund_rate=float(customer["refund_rate"]),
        explicit_refusal=bool(customer["explicit_refusal"]),
        has_marketing_consent=bool(customer["has_marketing_consent"]),
        suppression_hits=tuple(RuleId(rule) for rule in sample.get("global_suppression", [])),
    )
    candidates: list[ActionCandidate] = []
    for index, raw in enumerate(sample["candidates"]):
        platform_segment = raw.get("platform_segment")
        platform_facts: ScopeFacts | None = None
        if platform_segment is not None:
            platform_facts = replace(
                global_facts,
                metrics=_metrics_for_layer(platform_segment),
                suppression_hits=tuple(
                    RuleId(rule) for rule in sample.get("platform_suppression", [])
                ),
            )
        candidates.append(
            ActionCandidate(
                candidate_id=f"{sample['id']}-c{index}",
                platform=raw["platform"],
                coupon_value_cents=int(raw["coupon_value"] * 100),
                coupon_threshold_cents=int(raw["coupon_threshold"] * 100),
                created_at=_dt(raw["created_at"]),
                channel=raw["channel"],
                platform_facts=platform_facts,
            )
        )
    request = EvaluationRequest(
        customer=CustomerContext(
            global_facts=global_facts,
            touches=_build_touches(sample, now),
        ),
        candidates=tuple(candidates),
        now=now,
        config=cfg,
    )
    return request, {candidate.candidate_id: candidate for candidate in candidates}


def _config_with_priority_overrides(overrides: dict[str, int]) -> GovernanceConfig:
    """生成带 `platform_priority_overrides` 的**生效配置**。

    ⚠️ **已知缺陷**（见 T-B2 报告的遗留问题）：`src/policy_kernel/config.py` 的 `_flatten`
    把**非空**映射摊平成逐条叶子 → `conflict_resolution.platform_priority_overrides`
    这一 schema 路径随之消失 → `load_and_validate()` 报「缺必填项」。
    因此这里在**已加载**的配置对象上落位覆盖项（等价于「根配置已审批变更后的生效配置」）。
    待 T-B3 修好 config.py 后，本辅助函数应改回「写临时 YAML + load_and_validate」。
    """
    base = load_and_validate(GOVERNANCE_YAML)
    return replace(
        base,
        conflict_resolution=replace(
            base.conflict_resolution,
            platform_priority_overrides=dict(overrides),
        ),
    )


def _config_for_sample(sample: dict[str, Any]) -> GovernanceConfig:
    """样本自带 `governance_overrides` 时（如 B05）按覆盖流程生成配置。"""
    overrides = sample.get("governance_overrides")
    if not overrides:
        return load_and_validate(GOVERNANCE_YAML)
    priority_overrides = dict(overrides.get("platform_priority_overrides", {}))
    return _config_with_priority_overrides(priority_overrides)


def _observed_results(
    outcome: EvaluationOutcome,
    sample: dict[str, Any],
    by_id: dict[str, ActionCandidate],
) -> list[DecisionResult]:
    """取出与标注 `expected` 对照的判定结果集合。"""
    expected = sample["expected"]
    allowed_platform = expected.get("allowed_platform")
    if allowed_platform is not None:
        candidate_id = next(
            cid for cid, candidate in by_id.items() if candidate.platform == allowed_platform
        )
        return [outcome.results[candidate_id]]
    if outcome.allowed_candidate_id is not None:
        return [outcome.results[outcome.allowed_candidate_id]]
    return list(outcome.results.values())


def _benchmark_ids(benchmark: dict[str, Any]) -> list[str]:
    return [str(sample["id"]) for sample in benchmark["benchmarks"]]


def test_benchmark_has_twenty_samples(benchmark: dict[str, Any]) -> None:
    """附录D §3：评测集 = 15 条简单样本 + 5 条边界样本 = **20 条**。"""
    ids = _benchmark_ids(benchmark)
    assert len(ids) == 20, f"评测样本应为 20 条，实际 {len(ids)}：{ids}"
    assert len([i for i in ids if i.startswith("S")]) == 15
    assert len([i for i in ids if i.startswith("B")]) == 5


def _replay_cases() -> list[Any]:
    if not BENCHMARK_YAML.exists():
        pytest.fail(f"找不到 {BENCHMARK_YAML}：评测标注夹具缺失（T-G1 产物），无法回放。")
    data = yaml.safe_load(BENCHMARK_YAML.read_text(encoding="utf-8"))
    return [pytest.param(sample, id=str(sample["id"])) for sample in data["benchmarks"]]


@pytest.mark.parametrize("sample", _replay_cases())
def test_benchmark_sample_replay(sample: dict[str, Any]) -> None:
    """附录D §3「验收口径」：CI 每次跑回放对照，**一致率必须 100%**。

    对照口径（`meta.comparison_rule`）：
    ① `conclusion` 完全一致；
    ② `hit_rule_ids` 集合一致；
    ③ `suppressed_rule_ids` 集合一致 **且** `suppressed_candidates` 数量一致；
    ④ `allowed_platform` 存在时必须与实际放行候选一致（缺省不校验）。

    本用例是「标注先于内核」的落地验证：期望值来自人工标注文件，不来自内核输出。
    """
    cfg = _config_for_sample(sample)
    now = _dt("2026-10-23T12:00:00Z")
    request, by_id = _build_request(sample, cfg, now)
    outcome = evaluate(request)

    expected = sample["expected"]
    results = _observed_results(outcome, sample, by_id)
    assert results, "标注样本没有任何可对照的判定结果"

    expected_conclusion = Decision(expected["conclusion"])
    expected_hits = set(expected.get("hit_rule_ids", []))
    expected_suppressed = set(expected.get("suppressed_rule_ids", []))
    expected_suppressed_count = int(expected.get("suppressed_candidates", 0))

    for result in results:
        assert result.conclusion == expected_conclusion, (
            f"{sample['id']} 结论不一致：期望 {expected_conclusion}，实际 {result.conclusion}"
        )
        assert {rule.value for rule in result.hit_rule_ids} == expected_hits, (
            f"{sample['id']} hit_rule_ids 不一致：期望 {sorted(expected_hits)}，"
            f"实际 {sorted(rule.value for rule in result.hit_rule_ids)}"
        )
        assert {rule.value for rule in result.suppressed_rule_ids} == expected_suppressed, (
            f"{sample['id']} suppressed_rule_ids 不一致：期望 {sorted(expected_suppressed)}，"
            f"实际 {sorted(rule.value for rule in result.suppressed_rule_ids)}"
        )
        assert result.suppressed_candidates == expected_suppressed_count, (
            f"{sample['id']} suppressed_candidates 不一致：期望 {expected_suppressed_count}，"
            f"实际 {result.suppressed_candidates}"
        )

    allowed_platform = expected.get("allowed_platform", "__unset__")
    if allowed_platform != "__unset__":
        if allowed_platform is None:
            assert outcome.allowed_candidate_id is None, (
                f"{sample['id']} 期望无候选放行（升级人工），实际 {outcome.allowed_candidate_id}"
            )
        else:
            assert outcome.allowed_candidate_id is not None, f"{sample['id']} 期望有放行候选"
            assert by_id[outcome.allowed_candidate_id].platform == allowed_platform


def test_benchmark_global_segment_matches_customer_metrics(benchmark: dict[str, Any]) -> None:
    """冻结标注的 `global_segment` 必须与客户原始度量算出的全局层一致（标注自洽性）。"""
    cfg = load_and_validate(GOVERNANCE_YAML)

    for sample in benchmark["benchmarks"]:
        customer = sample["customer"]
        metrics = _metrics(
            int(customer["avg_order_value_90d"] * 100),
            int(customer["last_purchase_days_ago"]),
        )
        segment = segment_global(metrics, cfg.segmentation)
        assert segment.layer_name == sample["global_segment"], (
            f"{sample['id']} 全局层标注与度量不一致："
            f"标注 {sample['global_segment']}，算出 {segment.layer_name}"
        )


# ---------------------------------------------------------------------------
# 七、阈值一致性 / 依赖守卫 / 唯一实现点
# ---------------------------------------------------------------------------


def test_thresholds_match_governance_and_frozen_benchmark(
    config: GovernanceConfig, benchmark: dict[str, Any]
) -> None:
    """治理参数唯一来源 = governance.yaml；它必须与冻结评测基准的 `thresholds` **逐项相等**。

    两处必须同改，单改一处会让本用例变红（防漂移）。
    """
    frozen = yaml.safe_load(FROZEN_BENCHMARK_YAML.read_text(encoding="utf-8"))
    expected = frozen["thresholds"]
    actual = {
        "complaint_window_days": config.thresholds.complaint_window_days,
        "refund_rate_threshold": config.thresholds.refund_rate_threshold,
        "refund_dispute_window_days": config.thresholds.refund_dispute_window_days,
        "high_value_percentile": config.thresholds.high_value_percentile,
        "freq_window_days": config.thresholds.freq_window_days,
        "freq_max_touches": config.thresholds.freq_max_touches,
        "channel_cooldown_hours": config.thresholds.channel_cooldown_hours,
        "conflict_window_hours": config.thresholds.conflict_window_hours,
    }
    assert actual == expected, (
        "governance.yaml 的 thresholds 与冻结评测基准不一致：\n"
        f"  实际 = {actual}\n  期望 = {expected}"
    )
    assert benchmark["thresholds"] == expected, (
        "tests/evaluation/benchmark.yaml 的 thresholds 与冻结件不一致"
    )


def test_every_expected_rule_id_is_registered(benchmark: dict[str, Any]) -> None:
    """附录D §4「禁止自造 ID」：标注里的每个 rule_id 都必须在登记表内。"""
    for sample in benchmark["benchmarks"]:
        expected = sample["expected"]
        for key in ("hit_rule_ids", "suppressed_rule_ids"):
            for rule_id in expected.get(key, []):
                assert rule_id in {rule.value for rule in RuleId}, (
                    f"{sample['id']}.{key} 出现未登记的 rule_id：{rule_id}"
                )


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_rules_module_has_no_forbidden_dependencies() -> None:
    """红线 ① + 红线 ⑤：判定内核**不得** import ORM / LLM SDK / 供应商 SDK / 框架。

    允许的 `src.*` 只有两处：`src.policy_kernel`（数据契约 + 配置）与
    `src.unification.segmentation`（分层算法唯一实现点）。
    """
    assert RULES_PY.exists(), f"内核模块不存在：{RULES_PY}"
    tree = ast.parse(RULES_PY.read_text(encoding="utf-8"), filename=str(RULES_PY))
    forbidden_roots = {"sqlalchemy", "litellm", "fastapi", "pydantic", "langchain", "langgraph"}

    def _allowed(module: str) -> bool:
        if module.startswith("src.policy_kernel"):
            return True
        if module == "src.unification.segmentation":
            return True
        return module.split(".")[0] in sys.stdlib_module_names

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders.extend(alias.name for alias in node.names if not _allowed(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            module = node.module or ""
            if not _allowed(module) or module.split(".")[0] in forbidden_roots:
                offenders.append(module)
    assert not offenders, f"判定内核出现禁止依赖：{sorted(offenders)}"


def test_rules_module_does_not_read_server_clock() -> None:
    """红线 ①：内核**不读服务端当前时间**——所有「距今」判定以入参 `now` 为基准。

    静态扫描禁止 `datetime.now` / `utcnow` / `date.today` / `time.time`。
    """
    source = RULES_PY.read_text(encoding="utf-8")
    banned = ("datetime.now", "utcnow", "date.today", "time.time", "monotonic")
    hits = [token for token in banned if token in source]
    assert not hits, f"内核出现读服务端时钟的调用：{hits}"


def test_rules_module_does_not_reimplement_layer_score() -> None:
    """唯一实现点纪律：分层算法唯一实现点 = `src/unification/segmentation.py`。

    `rules.py` **只调用**它，不得另写一份 layer_score 计算（不得出现档位→分数映射、
    不得出现乘数公式、不得直接读 `layer_score_base`）。
    """
    source = RULES_PY.read_text(encoding="utf-8")
    for token in ("_AMOUNT_SCORES", "_RECENCY_SCORES", "layer_score_base", "amount_score"):
        assert token not in source, f"rules.py 疑似重复实现分层计算：出现 {token}"
    assert "segment_customer" in source, "rules.py 必须调用分层的唯一实现点 segment_customer"
