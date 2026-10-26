"""T-D1 冲突消解引擎验收用例。

权威依据：

- **附录D 规则 3**：冲突窗口三键排序（平台局部 layer_score → 权益净值 → 早创建）；
  被抑制候选写 ``suppression_record``，rule_id=`CONFLICT_WINDOW_72H`。
- **附录D 规则 4**：层级分维度（权益取更高 layer_score，抑制/频控取更严）。
- **附录D 规则 6**：三键全平 → 升级人工（REQUIRE_APPROVAL），不默认放行。
- **T-D1 prd.md 验收**：同客户双平台 → 只放行 1 条；另一条 DENY 且留痕。

纯函数部分标 ``@pytest.mark.unit``；需 DB 的标 ``@pytest.mark.pg``（真 PostgreSQL）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.policy_kernel.config import GovernanceConfig, load_and_validate
from src.policy_kernel.rules import (
    ActionCandidate,
    CustomerContext,
    EvaluationRequest,
    ScopeFacts,
)
from src.unification.segmentation import CustomerLayerMetrics

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _config() -> GovernanceConfig:
    """加载 governance.yaml（唯一配置来源）。"""
    return load_and_validate("governance.yaml")


def _metrics(avg_cents: int = 50000, days: int = 10) -> CustomerLayerMetrics:
    return CustomerLayerMetrics(avg_order_value_cents=avg_cents, days_since_last_purchase=days)


def _facts(avg_cents: int = 50000, days: int = 10, **overrides: object) -> ScopeFacts:
    defaults: dict[str, object] = {
        "metrics": _metrics(avg_cents, days),
        "total_spend_percentile": 50.0,
        "has_marketing_consent": True,
    }
    defaults.update(overrides)
    return ScopeFacts(**defaults)  # type: ignore[arg-type]


def _candidate(cid: str, platform: str, *, value_cents: int = 5000) -> ActionCandidate:
    return ActionCandidate(
        candidate_id=cid,
        platform=platform,
        coupon_value_cents=value_cents,
        coupon_threshold_cents=10000,
        created_at=_NOW,
        channel="sms",
    )


# ---------------------------------------------------------------------------
# 冲突消解：同客户双平台 → 只放行 1 条
# ---------------------------------------------------------------------------


def test_conflict_resolution_allows_only_one_candidate() -> None:
    """同客户双平台在冲突窗口内 → 只放行 1 条，另一条 DENY（附录D 规则 3）。

    依据：T-D1 验收「同客户双平台 → 只放行 1 条；另一条 DENY 且留痕」。
    """
    from src.decision.resolver import resolve_conflicts

    # 差异化 layer_score：C-1 高（amount_high_recent）→ 胜出；C-2 低 → 被抑制
    c_winner = ActionCandidate(
        candidate_id="C-1",
        platform="taobao",
        coupon_value_cents=5000,
        coupon_threshold_cents=10000,
        created_at=_NOW,
        channel="sms",
        platform_facts=_facts(avg_cents=50000, days=10),
    )
    c_loser = ActionCandidate(
        candidate_id="C-2",
        platform="douyin",
        coupon_value_cents=5000,
        coupon_threshold_cents=10000,
        created_at=_NOW,
        channel="sms",
        platform_facts=_facts(avg_cents=5000, days=200),
    )

    request = EvaluationRequest(
        customer=CustomerContext(global_facts=_facts()),
        candidates=(c_winner, c_loser),
        now=_NOW,
        config=_config(),
    )

    result = resolve_conflicts(request)

    assert len(result.survivors) == 1, f"应只放行 1 条，实际 {len(result.survivors)} 条"
    assert result.survivors[0] == "C-1", "layer_score 高者应胜出"
    assert len(result.suppressed) == 1, f"应有 1 条被抑制，实际 {len(result.suppressed)} 条"
    assert result.suppressed[0].rule_id == "CONFLICT_WINDOW_72H"


def test_conflict_resolution_winner_has_higher_layer_score() -> None:
    """三键排序：平台局部 layer_score 高者胜出（附录D 规则 3 第一键）。"""
    from src.decision.resolver import resolve_conflicts

    high_facts = _facts(avg_cents=50000, days=10)  # amount_high_recent → layer_score 33
    low_facts = _facts(avg_cents=5000, days=200)  # amount_low_stale → layer_score 11

    c_high = ActionCandidate(
        candidate_id="C-HIGH",
        platform="taobao",
        coupon_value_cents=5000,
        coupon_threshold_cents=10000,
        created_at=_NOW,
        channel="sms",
        platform_facts=high_facts,
    )
    c_low = ActionCandidate(
        candidate_id="C-LOW",
        platform="douyin",
        coupon_value_cents=5000,
        coupon_threshold_cents=10000,
        created_at=_NOW,
        channel="sms",
        platform_facts=low_facts,
    )

    request = EvaluationRequest(
        customer=CustomerContext(global_facts=_facts()),
        candidates=(c_low, c_high),
        now=_NOW,
        config=_config(),
    )

    result = resolve_conflicts(request)

    assert result.survivors[0] == "C-HIGH", "layer_score 高者应胜出"
    assert result.suppressed[0].candidate_id == "C-LOW"


def test_conflict_all_tied_triggers_escalation() -> None:
    """三键全平 → 规则 6 升级人工，不默认放行（附录D 规则 6）。"""
    from src.decision.resolver import resolve_conflicts

    c1 = _candidate("C-1", "taobao")
    c2 = _candidate("C-2", "douyin")

    request = EvaluationRequest(
        customer=CustomerContext(global_facts=_facts()),
        candidates=(c1, c2),
        now=_NOW,
        config=_config(),
    )

    result = resolve_conflicts(request)

    # 三键全平：两个候选参数完全一致 → 升级人工
    assert result.escalated is True, "三键全平应升级人工"
    assert result.survivors == (), "升级人工时不应有放行候选"


def test_suppressed_candidate_record_has_required_fields() -> None:
    """被抑制候选的记录必须包含 rule_id 和 reason_detail（可审计）。"""
    from src.decision.resolver import resolve_conflicts

    c_winner = ActionCandidate(
        candidate_id="C-WIN",
        platform="taobao",
        coupon_value_cents=5000,
        coupon_threshold_cents=10000,
        created_at=_NOW,
        channel="sms",
        platform_facts=_facts(avg_cents=50000, days=10),
    )
    c_loser = ActionCandidate(
        candidate_id="C-LOSE",
        platform="douyin",
        coupon_value_cents=5000,
        coupon_threshold_cents=10000,
        created_at=_NOW,
        channel="sms",
        platform_facts=_facts(avg_cents=5000, days=200),
    )

    request = EvaluationRequest(
        customer=CustomerContext(global_facts=_facts()),
        candidates=(c_winner, c_loser),
        now=_NOW,
        config=_config(),
    )

    result = resolve_conflicts(request)

    record = result.suppressed[0]
    assert record.rule_id == "CONFLICT_WINDOW_72H"
    assert record.candidate_id == "C-LOSE"
    assert record.reason_detail is not None
    assert len(record.reason_detail) > 0
