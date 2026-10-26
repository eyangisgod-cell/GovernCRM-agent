"""T-D3 影响面计算验收用例。

权威依据：

- **G-02**：同一客户多条动作只计 1 次（按客户数去重）。
- **裁决 #25**：三档路由（≤10 负责人单签 / 11–100 负责人+管理员双签 / >100 双签+合规知会）。
- **T-D3 prd.md 验收**：``pytest tests/decision/impact``。
- **唯一实现点**：三档路由判定在本模块；T-E2 审批只调用，不另写一份。

纯函数测试标 ``@pytest.mark.unit``。
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.unit

_TENANT = uuid.uuid4()


def _cid(n: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-4000-8000-{n:012d}")


# ---------------------------------------------------------------------------
# G-02：按客户数去重
# ---------------------------------------------------------------------------


def test_impact_counts_unique_customers_not_actions() -> None:
    """同一客户多条动作只计 1 次（G-02）。

    依据：G-02「同一客户多条动作只计 1 次」。
    """
    from src.decision.impact import calculate_impact

    customer_id = _cid(1)
    actions = [
        {"customer_id": customer_id, "candidate_id": "A-1"},
        {"customer_id": customer_id, "candidate_id": "A-2"},
        {"customer_id": customer_id, "candidate_id": "A-3"},
    ]

    result = calculate_impact(actions)

    assert result.unique_customer_count == 1, f"应计 1 个客户，实际 {result.unique_customer_count}"
    assert result.total_action_count == 3


def test_impact_counts_multiple_customers_correctly() -> None:
    """多个客户各自计 1 次，动作数不重复计。"""
    from src.decision.impact import calculate_impact

    actions = [
        {"customer_id": _cid(1), "candidate_id": "A-1"},
        {"customer_id": _cid(2), "candidate_id": "A-2"},
        {"customer_id": _cid(3), "candidate_id": "A-3"},
        {"customer_id": _cid(1), "candidate_id": "A-4"},  # 重复客户
    ]

    result = calculate_impact(actions)

    assert result.unique_customer_count == 3
    assert result.total_action_count == 4


# ---------------------------------------------------------------------------
# 裁决 #25：三档路由
# ---------------------------------------------------------------------------


def test_tier1_up_to_10_customers_owner_signs_alone() -> None:
    """≤10 客户 → 负责人单签（裁决 #25 第一档）。"""
    from src.decision.impact import route_by_impact

    result = route_by_impact(unique_customer_count=10)

    assert result.tier == "tier1"
    assert result.requires_admin is False
    assert result.requires_compliance is False


def test_tier2_11_to_100_customers_dual_sign() -> None:
    """11–100 客户 → 负责人+管理员双签（裁决 #25 第二档）。"""
    from src.decision.impact import route_by_impact

    result = route_by_impact(unique_customer_count=50)

    assert result.tier == "tier2"
    assert result.requires_admin is True
    assert result.requires_compliance is False


def test_tier3_over_100_customers_dual_sign_plus_compliance() -> None:
    """>100 客户 → 双签+合规知会（裁决 #25 第三档）。"""
    from src.decision.impact import route_by_impact

    result = route_by_impact(unique_customer_count=101)

    assert result.tier == "tier3"
    assert result.requires_admin is True
    assert result.requires_compliance is True


def test_tier_boundaries() -> None:
    """边界值精确：10/11/100/101 分档正确。"""
    from src.decision.impact import route_by_impact

    assert route_by_impact(unique_customer_count=10).tier == "tier1"
    assert route_by_impact(unique_customer_count=11).tier == "tier2"
    assert route_by_impact(unique_customer_count=100).tier == "tier2"
    assert route_by_impact(unique_customer_count=101).tier == "tier3"
