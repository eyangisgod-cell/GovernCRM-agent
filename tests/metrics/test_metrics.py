"""运营效率四项指标口径的单测（T-C2）。

权威依据：
- 裁决 #21 / PRD FR-35 —— 四项口径冻结：
  ① AI 候选采纳率 = 未经人工修改即发出的候选数 / 候选总数；
  ② 人均处理数 = 当日运营处理（批准/驳回/修改）的客户动作数 / 活跃运营数；
  ③ 候选→发出时长 = 候选创建至模拟回执的均值；
  ④ 「周期」= 任务批次。
- 裁决 #22 —— 「同类型动作」= ``action_type + channel`` 组合；模拟回执 ``status=success``
  计入，指标口径注明含模拟执行。
- 四项全部为**客观过程指标，不作因果承诺**。

纯函数单测：无 PG、无 LLM、无框架。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.metrics.operational import (
    OPERATOR_ACTIONS,
    SUCCESS_RECEIPT_STATUS,
    CandidateMetric,
    OperatorAction,
    action_type_key,
    adoption_rate,
    candidate_to_send_avg_ms,
    group_by_action_type,
    per_capita_processing_count,
    task_batch_periods,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
METRICS_SOURCE = ROOT / "src" / "metrics" / "operational.py"

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _candidate(
    candidate_id: str,
    *,
    task_id: str | None = "TASK-1",
    action_type: str = "coupon",
    channel: str = "sms",
    created_at: datetime | None = None,
    sent: bool = False,
    human_modified: bool = False,
    receipt_at: datetime | None = None,
    receipt_status: str | None = None,
    contains_synthetic: bool = False,
) -> CandidateMetric:
    return CandidateMetric(
        candidate_id=candidate_id,
        task_id=task_id,
        action_type=action_type,
        channel=channel,
        created_at=created_at if created_at is not None else NOW,
        sent=sent,
        human_modified=human_modified,
        receipt_at=receipt_at,
        receipt_status=receipt_status,
        contains_synthetic=contains_synthetic,
    )


def _operator_action(
    action_id: str,
    operator_id: str,
    operator_action: str = "approve",
    *,
    candidate_id: str | None = None,
    action_type: str = "coupon",
    channel: str = "sms",
) -> OperatorAction:
    return OperatorAction(
        action_id=action_id,
        candidate_id=candidate_id if candidate_id is not None else action_id,
        operator_id=operator_id,
        operator_action=operator_action,
        action_type=action_type,
        channel=channel,
        processed_at=NOW,
    )


# ---------------------------------------------------------------------------
# ① AI 候选采纳率（FR-35 ① / 裁决 #21）
# ---------------------------------------------------------------------------


def test_adoption_rate_is_unmodified_sent_over_total_candidates() -> None:
    """采纳率 = 未经人工修改即发出的候选数 / **候选总数**（分母含被修改与未发出的）。"""
    candidates = [
        _candidate("C1", sent=True),
        _candidate("C2", sent=True),
        _candidate("C3", sent=True, human_modified=True),
        _candidate("C4", sent=False),
    ]
    assert adoption_rate(candidates) == pytest.approx(2 / 4)


def test_adoption_rate_is_undefined_when_there_is_no_candidate() -> None:
    """分母为 0 时返回 None（口径未定义），不得返回 0 冒充「采纳率 0%」。"""
    assert adoption_rate([]) is None


# ---------------------------------------------------------------------------
# ② 人均处理数（FR-35 ② / 裁决 #21）
# ---------------------------------------------------------------------------


def test_per_capita_processing_count_divides_by_active_operators() -> None:
    """人均处理数 = 当日运营处理（批准/驳回/修改）的客户动作数 / **活跃运营数**。"""
    processed = [
        _operator_action("A1", "op-1"),
        _operator_action("A2", "op-1", "reject"),
        _operator_action("A3", "op-1", "modify"),
        _operator_action("A4", "op-2"),
        _operator_action("A5", "op-2", "reject"),
    ]
    assert per_capita_processing_count(processed) == pytest.approx(5 / 2)


def test_per_capita_processing_count_uses_given_active_operator_roster() -> None:
    """给了活跃运营名单就按名单算分母（含当日未处理动作的运营）。"""
    processed = [_operator_action("A1", "op-1")]
    assert per_capita_processing_count(processed, ["op-1", "op-2", "op-3"]) == pytest.approx(1 / 3)


def test_per_capita_processing_count_counts_distinct_customer_actions() -> None:
    """同一条客户动作被多次处理（改后批）只计 1 次。"""
    processed = [
        _operator_action("A1", "op-1", "modify", candidate_id="C1"),
        _operator_action("A1", "op-1", "approve", candidate_id="C1"),
    ]
    assert per_capita_processing_count(processed) == pytest.approx(1 / 1)


def test_per_capita_processing_count_ignores_non_processing_actions() -> None:
    """口径只认「批准/驳回/修改」三种运营处理动作。"""
    processed = [
        _operator_action("A1", "op-1"),
        _operator_action("A2", "op-1", "view"),
    ]
    assert per_capita_processing_count(processed) == pytest.approx(1 / 1)
    assert set(OPERATOR_ACTIONS) == {"approve", "reject", "modify"}


def test_per_capita_processing_count_is_undefined_without_processing() -> None:
    """当日无运营处理 → 口径未定义（None），不返回 0 冒充「人均 0 件」。"""
    assert per_capita_processing_count([]) is None


# ---------------------------------------------------------------------------
# ③ 候选→发出时长（FR-35 ③ / 裁决 #21 + #22）
# ---------------------------------------------------------------------------


def test_candidate_to_send_duration_is_mean_of_intervals() -> None:
    """候选→发出时长 = 候选创建至**模拟回执**的均值（毫秒）。"""
    candidates = [
        _candidate(
            "C1",
            created_at=NOW,
            receipt_at=NOW + timedelta(seconds=1),
            receipt_status=SUCCESS_RECEIPT_STATUS,
        ),
        _candidate(
            "C2",
            created_at=NOW,
            receipt_at=NOW + timedelta(seconds=3),
            receipt_status=SUCCESS_RECEIPT_STATUS,
        ),
    ]
    assert candidate_to_send_avg_ms(candidates) == 2000


def test_candidate_to_send_duration_counts_only_successful_receipts() -> None:
    """裁决 #22：只有 ``status=success`` 的回执计入时长口径。"""
    candidates = [
        _candidate(
            "C1",
            created_at=NOW,
            receipt_at=NOW + timedelta(seconds=1),
            receipt_status=SUCCESS_RECEIPT_STATUS,
        ),
        _candidate(
            "C2",
            created_at=NOW,
            receipt_at=NOW + timedelta(seconds=99),
            receipt_status="failed",
        ),
    ]
    assert candidate_to_send_avg_ms(candidates) == 1000


def test_candidate_to_send_duration_is_undefined_without_receipt() -> None:
    """没有成功回执 → 口径未定义（None）。"""
    assert candidate_to_send_avg_ms([_candidate("C1")]) is None


# ---------------------------------------------------------------------------
# ④ 周期 = 任务批次（FR-35 ④ / FR-33「周期」口径 / 裁决 #21）
# ---------------------------------------------------------------------------


def test_period_is_task_batch() -> None:
    """「周期」= 任务批次：同一天的两个任务批次是**两个周期**。"""
    candidates = [
        _candidate("C1", task_id="TASK-A", sent=True),
        _candidate("C2", task_id="TASK-A", sent=False),
        _candidate("C3", task_id="TASK-B", sent=True),
    ]
    periods = task_batch_periods(candidates)
    assert [period.task_id for period in periods] == ["TASK-A", "TASK-B"]
    assert periods[0].candidate_count == 2
    assert periods[0].adopted_count == 1
    assert periods[0].adoption_rate == pytest.approx(0.5)
    assert periods[1].candidate_count == 1


def test_period_keeps_unbatched_candidates_in_their_own_bucket() -> None:
    """未归入任务批次的候选单列一档，不静默丢弃。"""
    periods = task_batch_periods([_candidate("C1", task_id=None)])
    assert len(periods) == 1
    assert periods[0].candidate_count == 1


# ---------------------------------------------------------------------------
# 「同类型动作」= action_type + channel（裁决 #22）
# ---------------------------------------------------------------------------


def test_same_action_type_means_type_plus_channel() -> None:
    """同类型动作 = ``action_type + channel`` 组合——类型相同但通道不同即不同类。"""
    assert action_type_key("coupon", "sms") == action_type_key("coupon", "sms")
    assert action_type_key("coupon", "sms") != action_type_key("coupon", "email")
    assert action_type_key("coupon", "sms") != action_type_key("content", "sms")


def test_group_by_action_type_splits_on_channel() -> None:
    """按「同类型动作」分组：3 条候选分到 2 个组合。"""
    candidates = [
        _candidate("C1", action_type="coupon", channel="sms"),
        _candidate("C2", action_type="coupon", channel="sms"),
        _candidate("C3", action_type="coupon", channel="email"),
    ]
    groups = group_by_action_type(candidates)
    assert set(groups) == {action_type_key("coupon", "sms"), action_type_key("coupon", "email")}
    assert len(groups[action_type_key("coupon", "sms")]) == 2
    assert len(groups[action_type_key("coupon", "email")]) == 1


# ---------------------------------------------------------------------------
# 模拟执行标注与「不作因果承诺」
# ---------------------------------------------------------------------------


def test_metrics_flag_contains_synthetic() -> None:
    """含模拟执行时必须标注（裁决 #22），供看板区分模拟与真实数据。"""
    candidates = [
        _candidate("C1", task_id="TASK-A", sent=True, contains_synthetic=True),
        _candidate("C2", task_id="TASK-A", sent=True),
    ]
    periods = task_batch_periods(candidates)
    assert periods[0].contains_synthetic is True

    real_only = task_batch_periods([_candidate("C3", task_id="TASK-B", sent=True)])
    assert real_only[0].contains_synthetic is False


def test_metrics_module_makes_no_causal_claims() -> None:
    """四项指标是客观过程指标：源码不得出现「因 AI 提升 X%」这类因果表述。"""
    source = METRICS_SOURCE.read_text(encoding="utf-8")
    for banned in ("提升", "因为 AI", "因 AI", "improve", "boost"):
        assert banned not in source, f"指标口径不得出现因果表述：{banned}"
