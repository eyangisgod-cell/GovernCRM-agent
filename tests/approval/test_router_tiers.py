"""T-E2 审批三档路由与 48h 超时验收用例。

权威依据：

- **裁决 #25**（架构 v5.2:635-636、governance.yaml ``approval`` 种子参数）：
  影响面（批次涵盖客户数）≤10 → 运营负责人单签；11–100 → 负责人 + 管理员双签；
  >100 → 双签 + 合规知会（知会 = 只读通知，不参与签署）。
- **E9 / 裁决 #25**：审批 48h 自动失效且**不默认通过**——超时态 = 过期，
  需重新发起审批，不存在"超时即批准"路径。
- **裁决 #25 唯一实现点纪律**（红线①延伸）：三档阈值唯一实现点 =
  ``src/decision/impact.route_by_impact``；``src/approval/router.py`` 只调用它，
  不得复制 10/100 阈值常量副本。

纯函数用例标 ``pytest.mark.unit``（默认 ``-m "not pg"`` 放行）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.approval.router import (
    ApprovalExpiredError,
    check_timeout,
    ensure_not_expired,
    required_signers,
)
from src.auth.roles import Role

pytestmark = pytest.mark.unit

#: governance.yaml ``approval.timeout_hours`` 种子值（E9 / 裁决 #25：48h 失效不默认通过）。
_TIMEOUT_HOURS = 48

_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROUTER_SOURCE = (_REPO_ROOT / "src" / "approval" / "router.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 三档签署人集合（裁决 #25，先红后绿）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unique_customer_count", "expected_signers", "expected_notified"),
    [
        (0, (Role.OPS_LEAD,), ()),
        (1, (Role.OPS_LEAD,), ()),
        (10, (Role.OPS_LEAD,), ()),
        (11, (Role.OPS_LEAD, Role.ADMIN), ()),
        (50, (Role.OPS_LEAD, Role.ADMIN), ()),
        (100, (Role.OPS_LEAD, Role.ADMIN), ()),
        (101, (Role.OPS_LEAD, Role.ADMIN), (Role.COMPLIANCE,)),
        (10_000, (Role.OPS_LEAD, Role.ADMIN), (Role.COMPLIANCE,)),
    ],
)
def test_required_signers_follows_impact_tiers(
    unique_customer_count: int,
    expected_signers: tuple[Role, ...],
    expected_notified: tuple[Role, ...],
) -> None:
    """参数化三档（裁决 #25）：≤10 单签 / 11–100 双签 / >100 双签 + 合规知会。

    知会（compliance）只读通知、不参与签署——因此进 ``notified`` 而非 ``signers``。
    """
    routing = required_signers(unique_customer_count=unique_customer_count)

    assert routing.signers == expected_signers
    assert routing.notified == expected_notified
    # 签署人不得为空——任何档位至少有运营负责人（裁决 #25）
    assert routing.signers, "任何影响面档位都必须有至少一名签署人"


def test_router_delegates_tier_decision_to_impact_route_by_impact() -> None:
    """源码纪律（裁决 #25 / 红线①）：三档阈值唯一实现点 = ``src/decision/impact.py``。

    router 只允许调用 ``route_by_impact``，不得出现：
    - 对 ``unique_customer_count`` 的阈值比较（10/100 副本）；
    - 硬编码档位字面量 ``tier1/tier2/tier3``（值由 impact 产出）。
    """
    assert "src.decision.impact" in _ROUTER_SOURCE, "router 必须调用 src.decision.impact"
    assert "route_by_impact" in _ROUTER_SOURCE, "router 必须委托 route_by_impact"
    assert not re.search(r"unique_customer_count\s*(<=|<|>=|>|==)", _ROUTER_SOURCE), (
        "router 内出现三档阈值比较副本（唯一实现点在 impact.py）"
    )
    for tier in ("tier1", "tier2", "tier3"):
        assert tier not in _ROUTER_SOURCE, f"router 内出现硬编码档位 {tier}"


# ---------------------------------------------------------------------------
# 审批超时 48h（E9：过期且不默认通过，先红后绿）
# ---------------------------------------------------------------------------


def test_check_timeout_active_before_deadline() -> None:
    """E9 正向：提交后 47h（剩余 1h）仍未过期，剩余秒数可核算。"""
    submitted_at = _NOW - timedelta(hours=47)

    status = check_timeout(submitted_at, _NOW, timeout_hours=_TIMEOUT_HOURS)

    assert status.expired is False
    assert status.remaining_seconds == 3600


def test_check_timeout_expires_at_exact_deadline() -> None:
    """E9 边界：满 48h 即失效（"48h 自动失效"，含等于时刻）。"""
    submitted_at = _NOW - timedelta(hours=48)

    status = check_timeout(submitted_at, _NOW, timeout_hours=_TIMEOUT_HOURS)

    assert status.expired is True
    assert status.remaining_seconds == 0


def test_check_timeout_expires_after_deadline() -> None:
    """E9 负向：超过 48h → 过期。"""
    submitted_at = _NOW - timedelta(hours=49)

    status = check_timeout(submitted_at, _NOW, timeout_hours=_TIMEOUT_HOURS)

    assert status.expired is True


def test_expired_timeout_is_never_treated_as_approved() -> None:
    """E9 核心负向（不默认通过）：过期状态被 ``ensure_not_expired`` 拦截为异常。

    超时态 = 拒绝/过期，需重新发起——不存在把过期解读为"批准"的路径。
    """
    status = check_timeout(_NOW - timedelta(hours=49), _NOW, timeout_hours=_TIMEOUT_HOURS)
    assert status.expired is True

    with pytest.raises(ApprovalExpiredError, match="48"):
        ensure_not_expired(status, timeout_hours=_TIMEOUT_HOURS)


def test_active_timeout_passes_ensure_not_expired() -> None:
    """E9 正向：未过期的审批不被超时闸拦截。"""
    status = check_timeout(_NOW - timedelta(hours=1), _NOW, timeout_hours=_TIMEOUT_HOURS)

    ensure_not_expired(status, timeout_hours=_TIMEOUT_HOURS)  # 不抛即通过
