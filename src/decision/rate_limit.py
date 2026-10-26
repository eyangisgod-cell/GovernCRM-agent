"""T-D2 频控数据接入（决策层）。

只做**数据接入**：统计触达次数（全局 / 同通道），喂给 `policy_kernel.rules.evaluate()`。

**不在此重复实现规则**——判定逻辑唯一在 `src/policy_kernel/rules.py`（T-B2）。
`src/decision/` 下不得出现第二份抑制/频控判定逻辑。

红线①：纯函数、零 IO。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

__all__ = [
    "count_channel_touches",
    "count_global_touches",
]


def count_global_touches(
    touches: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    now: datetime,
    window_days: int,
) -> int:
    """统计窗口内的全局触达次数（规则 2：≤2 次）。"""
    cutoff = now.timestamp() - window_days * 86400
    return sum(1 for t in touches if t["called_at"].timestamp() >= cutoff)


def count_channel_touches(
    touches: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    channel: str,
    now: datetime,
    window_hours: int,
) -> int:
    """统计同通道窗口内的触达次数（规则 2：同通道 ≥72h）。"""
    cutoff = now.timestamp() - window_hours * 3600
    return sum(
        1 for t in touches if t["channel"] == channel and t["called_at"].timestamp() >= cutoff
    )
