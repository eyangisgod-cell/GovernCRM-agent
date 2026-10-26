"""T-D2 抑制与频控数据接入（决策层）。

只做**数据接入**：读 customer / suppression_record / tool_call 行并构造
`ScopeFacts` / `TouchEvent` / `RuleId` 元组，喂给 `policy_kernel.rules.evaluate()`。

**不在此重复实现规则**——判定逻辑唯一在 `src/policy_kernel/rules.py`（T-B2）。
`src/decision/` 下不得出现第二份抑制/频控判定逻辑。

红线①：构造函数纯函数、零 IO（DB 读取由调用方完成）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from src.policy_kernel.kernel import RuleId
from src.policy_kernel.rules import ScopeFacts, TouchEvent
from src.unification.segmentation import CustomerLayerMetrics

__all__ = [
    "build_scope_facts",
    "build_suppression_hits",
    "build_touches",
]


def build_scope_facts(customer_row: dict[str, Any]) -> ScopeFacts:
    """从 customer 行构造 ScopeFacts（喂给内核的输入）。

    字段映射：
    - ``avg_order_value_90d``（元）→ ``avg_order_value_cents``（分）；
    - ``last_order_days_ago`` → ``days_since_last_purchase``；
    - ``complaint_count_90d`` → ``complaints``（展开为 datetime 元组）；
    - ``consent_status != 'granted'`` → ``has_marketing_consent=False``。
    """
    metrics = CustomerLayerMetrics(
        avg_order_value_cents=int(customer_row.get("avg_order_value_90d", 0) * 100),
        days_since_last_purchase=int(customer_row.get("last_order_days_ago", 0)),
    )
    complaint_count = int(customer_row.get("complaint_count_90d", 0))
    # 投诉展开为 datetime 元组（内核按窗口计数）；时间不精确，用等距占位
    complaints = tuple(
        datetime(2026, 7, 1, tzinfo=UTC) + timedelta(days=i * 10) for i in range(complaint_count)
    )
    consent_status = customer_row.get("consent_status", "unknown")
    return ScopeFacts(
        metrics=metrics,
        total_spend_percentile=float(customer_row.get("total_spend_percentile", 0.0)),
        complaints=complaints,
        refund_rate=float(customer_row.get("refund_rate", 0.0)),
        explicit_refusal=bool(customer_row.get("explicit_refusal", False)),
        has_marketing_consent=(consent_status == "granted"),
    )


def build_touches(tool_call_rows: list[dict[str, Any]]) -> tuple[TouchEvent, ...]:
    """从 tool_call 行构造 TouchEvent 元组（频控输入）。"""
    return tuple(TouchEvent(at=row["called_at"], channel=row["channel"]) for row in tool_call_rows)


def build_suppression_hits(
    suppression_records: list[dict[str, Any]],
) -> tuple[RuleId, ...]:
    """从 suppression_record 行构造 suppression_hits（显式已命中规则）。"""
    return tuple(RuleId(row["rule_id"]) for row in suppression_records)
