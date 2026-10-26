"""T-D3 影响面计算（决策层）。

权威依据：

- **G-02**：同一客户多条动作只计 1 次（按客户数去重）。
- **裁决 #25**：三档路由（≤10 负责人单签 / 11–100 负责人+管理员双签 / >100 双签+合规知会）。
- **唯一实现点**：三档路由判定在本模块；T-E2 审批只调用，不另写一份。

红线①：纯函数、零 IO、零时钟依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["ImpactResult", "RouteResult", "calculate_impact", "route_by_impact"]


@dataclass(frozen=True)
class ImpactResult:
    """影响面计算结果。"""

    unique_customer_count: int
    total_action_count: int


@dataclass(frozen=True)
class RouteResult:
    """三档路由结果。"""

    tier: str
    requires_admin: bool
    requires_compliance: bool


def calculate_impact(actions: list[dict[str, Any]]) -> ImpactResult:
    """按客户数计算影响面（G-02：同一客户多条动作只计 1 次）。"""
    customer_ids = {a["customer_id"] for a in actions}
    return ImpactResult(
        unique_customer_count=len(customer_ids),
        total_action_count=len(actions),
    )


def route_by_impact(*, unique_customer_count: int) -> RouteResult:
    """三档路由（裁决 #25）。

    - ≤10 → tier1：负责人单签；
    - 11–100 → tier2：负责人+管理员双签；
    - >100 → tier3：双签+合规知会。
    """
    if unique_customer_count <= 10:
        return RouteResult(tier="tier1", requires_admin=False, requires_compliance=False)
    if unique_customer_count <= 100:
        return RouteResult(tier="tier2", requires_admin=True, requires_compliance=False)
    return RouteResult(tier="tier3", requires_admin=True, requires_compliance=True)
