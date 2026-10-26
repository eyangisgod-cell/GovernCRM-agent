"""T-D2 抑制与频控数据接入验收用例。

权威依据：

- **附录D 规则 1b**：NO_CONSENT 两步（无授权 → NO_CONSENT → DENY）。
- **附录D 规则 2**：全局频控（3 次 → DENY）+ 同通道 ≥72h。
- **T-D2 prd.md**：只做数据接入（读 customer/suppression_record/tool_call 喂给内核），
  **不重复实现规则**——判定逻辑唯一在 `src/policy_kernel/rules.py`。
- **验收**：① 投诉客户 → DENY ② 频控第 3 次 → DENY ③ 同通道 <72h → DENY。

纯函数部分标 ``@pytest.mark.unit``；需 DB 的标 ``@pytest.mark.pg``。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 数据接入：从原始行构造 ScopeFacts（喂给内核的输入）
# ---------------------------------------------------------------------------


def test_build_scope_facts_from_customer_row() -> None:
    """从 customer 行构造 ScopeFacts（含投诉/拒收/授权字段）。

    依据：T-D2「读 customer 并喂给内核」。
    """
    from src.decision.suppression import build_scope_facts

    customer_row = {
        "complaint_count_90d": 3,
        "has_refund_dispute": False,
        "refund_rate": 0.05,
        "explicit_refusal": False,
        "consent_status": "granted",
        "total_spend_percentile": 50.0,
        "avg_order_value_90d": 100,
        "last_order_days_ago": 30,
    }

    facts = build_scope_facts(customer_row)

    assert facts.metrics.avg_order_value_cents == 10000  # 元 → 分
    assert facts.metrics.days_since_last_purchase == 30
    assert len(facts.complaints) == 3, "投诉次数应展开为 datetime 元组"
    assert facts.has_marketing_consent is True


def test_build_scope_facts_no_consent() -> None:
    """无授权客户 → has_marketing_consent=False（规则 1b 触发 NO_CONSENT）。"""
    from src.decision.suppression import build_scope_facts

    customer_row = {
        "complaint_count_90d": 0,
        "has_refund_dispute": False,
        "refund_rate": 0.0,
        "explicit_refusal": False,
        "consent_status": "denied",
        "total_spend_percentile": 50.0,
        "avg_order_value_90d": 100,
        "last_order_days_ago": 30,
    }

    facts = build_scope_facts(customer_row)

    assert facts.has_marketing_consent is False


# ---------------------------------------------------------------------------
# 数据接入：触达历史（喂给内核做频控）
# ---------------------------------------------------------------------------


def test_build_touches_from_tool_call_rows() -> None:
    """从 tool_call 行构造 TouchEvent 元组（频控输入）。

    依据：附录D 规则 2「全局频控 ≤2 次」+「同通道 ≥72h」。
    """
    from src.decision.suppression import build_touches

    tool_call_rows = [
        {"channel": "sms", "called_at": _NOW - timedelta(days=1)},
        {"channel": "sms", "called_at": _NOW - timedelta(days=3)},
        {"channel": "wechat", "called_at": _NOW - timedelta(days=5)},
    ]

    touches = build_touches(tool_call_rows)

    assert len(touches) == 3
    assert touches[0].channel == "sms"
    assert touches[2].channel == "wechat"


def test_build_touches_empty() -> None:
    """无触达记录 → 空元组（首触不受频控限制）。"""
    from src.decision.suppression import build_touches

    touches = build_touches([])
    assert touches == ()


# ---------------------------------------------------------------------------
# 数据接入：抑制记录（喂给内核的 suppression_hits）
# ---------------------------------------------------------------------------


def test_build_suppression_hits_from_records() -> None:
    """从 suppression_record 行构造 suppression_hits（显式已命中规则）。

    依据：T-D2「读 suppression_record 并喂给内核」。
    """
    from src.decision.suppression import build_suppression_hits

    records = [
        {"rule_id": "SUP_HIGH_VALUE"},
        {"rule_id": "SUP_HIGH_COMPLAINT"},
    ]

    hits = build_suppression_hits(records)

    assert len(hits) == 2
    assert "SUP_HIGH_VALUE" in [h.value for h in hits]
    assert "SUP_HIGH_COMPLAINT" in [h.value for h in hits]


def test_build_suppression_hits_empty() -> None:
    """无抑制记录 → 空元组。"""
    from src.decision.suppression import build_suppression_hits

    hits = build_suppression_hits([])
    assert hits == ()
