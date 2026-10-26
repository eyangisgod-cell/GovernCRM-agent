"""运营指标口径包（裁决 #21 / PRD FR-35）。

公开入口 = ``src.metrics.operational``：四项口径的纯函数实现，
零 LLM、零框架、零 IO（不连库）。
"""

from __future__ import annotations

from src.metrics.operational import (
    OPERATOR_ACTIONS,
    SUCCESS_RECEIPT_STATUS,
    CandidateMetric,
    OperatorAction,
    TaskBatchPeriod,
    action_type_key,
    adoption_rate,
    candidate_to_send_avg_ms,
    group_by_action_type,
    per_capita_processing_count,
    task_batch_periods,
)

__all__ = [
    "OPERATOR_ACTIONS",
    "SUCCESS_RECEIPT_STATUS",
    "CandidateMetric",
    "OperatorAction",
    "TaskBatchPeriod",
    "action_type_key",
    "adoption_rate",
    "candidate_to_send_avg_ms",
    "group_by_action_type",
    "per_capita_processing_count",
    "task_batch_periods",
]
