"""T-D2 频控数据接入验收用例。

权威依据：

- **附录D 规则 2**：全局频控（≤2 次）+ 同通道 ≥72h。
- **T-D2 prd.md**：只做数据接入，不重复实现规则。

纯函数部分标 ``@pytest.mark.unit``。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def test_count_global_touches_in_window() -> None:
    """统计 7 天窗口内的触达次数（规则 2：≤2 次）。

    依据：附录D 规则 2「全局频控 3 次 → DENY」。
    """
    from src.decision.rate_limit import count_global_touches

    touches = (
        {"called_at": _NOW - timedelta(days=1), "channel": "sms"},
        {"called_at": _NOW - timedelta(days=3), "channel": "wechat"},
        {"called_at": _NOW - timedelta(days=5), "channel": "sms"},
        {"called_at": _NOW - timedelta(days=30), "channel": "email"},  # 超窗口
    )

    count = count_global_touches(touches, now=_NOW, window_days=7)

    assert count == 3, f"7 天内应有 3 次，实际 {count}"


def test_count_channel_touches_in_window() -> None:
    """统计同通道 72h 内的触达次数（规则 2：同通道 ≥72h）。

    依据：附录D 规则 2「同通道 ≥72h」。
    """
    from src.decision.rate_limit import count_channel_touches

    touches = (
        {"called_at": _NOW - timedelta(hours=1), "channel": "sms"},
        {"called_at": _NOW - timedelta(hours=24), "channel": "sms"},
        {"called_at": _NOW - timedelta(hours=100), "channel": "sms"},  # 超 72h
    )

    count = count_channel_touches(touches, channel="sms", now=_NOW, window_hours=72)

    assert count == 2, f"sms 72h 内应有 2 次，实际 {count}"


def test_count_touches_empty() -> None:
    """无触达记录 → 0 次。"""
    from src.decision.rate_limit import count_channel_touches, count_global_touches

    assert count_global_touches((), now=_NOW, window_days=7) == 0
    assert count_channel_touches((), channel="sms", now=_NOW, window_hours=72) == 0
