"""T-E2 审批三档路由与超时语义（纯函数，零 IO）。

权威依据：

- **裁决 #25**（架构 v5.2:635-636、governance.yaml ``approval`` 种子参数）：
  影响面（批次涵盖客户数）≤10 → 运营负责人单签；11–100 → 负责人 + 管理员双签；
  >100 → 双签 + 合规知会（知会 = 只读通知，不参与签署，因此进 ``notified``）。
- **E9 / 裁决 #25**：审批 48h（``approval.timeout_hours``）自动失效且**不默认通过**——
  超时态 = 过期，需重新发起；``ensure_not_expired`` 是超时态唯一的出口语义（抛异常）。
- **红线①延伸（三档阈值唯一实现点）**：10/100 阈值只存在于
  ``src/decision/impact.route_by_impact``；本模块只调用它，不复制阈值常量。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.auth.roles import Role
from src.decision.impact import route_by_impact

__all__ = [
    "ApprovalExpiredError",
    "ApprovalRouting",
    "ApprovalTimeout",
    "check_timeout",
    "ensure_not_expired",
    "required_signers",
]


class ApprovalExpiredError(RuntimeError):
    """审批已超时失效（E9：不默认通过，需重新发起）。"""


@dataclass(frozen=True)
class ApprovalRouting:
    """三档路由结果：签署人集合 + 知会集合（裁决 #25）。"""

    tier: str
    #: 参与签署的角色（至少含运营负责人）。
    signers: tuple[Role, ...]
    #: 知会角色：只读通知，不参与签署（>100 时为合规）。
    notified: tuple[Role, ...]


def required_signers(*, unique_customer_count: int) -> ApprovalRouting:
    """按影响面产出三档签署人集合（裁决 #25）。

    阈值判定唯一实现点 = ``src/decision/impact.route_by_impact``；
    本函数只把 ``RouteResult`` 映射为 RBAC 角色集合，不出现 10/100 副本。
    """
    route = route_by_impact(unique_customer_count=unique_customer_count)
    signers = [Role.OPS_LEAD]
    notified: list[Role] = []
    if route.requires_admin:
        signers.append(Role.ADMIN)
    if route.requires_compliance:
        notified.append(Role.COMPLIANCE)
    return ApprovalRouting(tier=route.tier, signers=tuple(signers), notified=tuple(notified))


@dataclass(frozen=True)
class ApprovalTimeout:
    """审批超时核算结果（E9：``expired`` 为唯一判定点，过期不默认通过）。"""

    expired: bool
    remaining_seconds: int


def check_timeout(submitted_at: datetime, now: datetime, *, timeout_hours: int) -> ApprovalTimeout:
    """核算审批是否超时（E9 / 裁决 #25：满 timeout_hours 即失效，含等于时刻）。

    ``timeout_hours`` 由调用方从 governance config（``approval.timeout_hours``）传入，
    本模块不内置 48 字面量副本。
    """
    deadline = submitted_at + timedelta(hours=timeout_hours)
    remaining = int((deadline - now).total_seconds())
    return ApprovalTimeout(expired=remaining <= 0, remaining_seconds=remaining)


def ensure_not_expired(status: ApprovalTimeout, *, timeout_hours: int) -> None:
    """超时态的唯一出口：过期即异常，**不存在默认通过路径**（E9）。"""
    if status.expired:
        raise ApprovalExpiredError(
            f"审批已超时失效（{timeout_hours}h，E9：不默认通过），需重新发起审批"
        )
