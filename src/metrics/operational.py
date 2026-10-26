"""运营效率四项指标口径（裁决 #21 / PRD FR-35）。

四项口径（冻结，不得各算各的）：

===== ========================================================== ==========================
编号   指标                                                     口径
===== ========================================================== ==========================
①     AI 候选采纳率                                           未经人工修改即发出的候选数 / 候选总数
②     人均处理数                                               当日运营处理（批准/驳回/修改）的客户动作数 / 活跃运营数
③     候选→发出时长                                           候选创建至模拟回执的均值
④     周期                                                     任务批次
===== ========================================================== ==========================

配套口径：
- 「同类型动作」= ``action_type + channel`` 组合（裁决 #22）。
- 模拟成功回执（``status=success``）计入，指标注明含模拟执行（``contains_synthetic``）。
- **四项全部是客观过程指标，不作因果承诺**——只陈述本系统内观察到的过程量，
  不表达「因为用了 AI 所以某项变好」这类结论（PRD FR-35 / FR-34 的相关性口径）。

红线 1：本模块是**纯函数**——零 LLM、零框架、零 IO（不连库）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

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

#: 运营处理动作（② 人均处理数的分子口径）：批准 / 驳回 / 修改。
OPERATOR_ACTIONS: tuple[str, ...] = ("approve", "reject", "modify")

#: 计入时长口径的回执状态（裁决 #22：模拟成功回执计入）。
SUCCESS_RECEIPT_STATUS = "success"

#: 「同类型动作」的键分隔符（``action_type + channel`` 组合）。
_ACTION_TYPE_KEY_SEPARATOR = "::"


@dataclass(frozen=True)
class CandidateMetric:
    """一条候选动作的度量（数据源 = ``action_candidate`` + 发送回执）。

    - ``sent``：候选是否已发出；
    - ``human_modified``：发出前是否被人工修改过（改过就不算「未经修改即发出」）；
    - ``receipt_at`` / ``receipt_status``：模拟回执的时间与状态；
    - ``contains_synthetic``：该条数据是否来自模拟执行（数据带 ``_synthetic: true``）。
    """

    candidate_id: str
    task_id: str | None
    action_type: str
    channel: str
    created_at: datetime
    sent: bool = False
    human_modified: bool = False
    receipt_at: datetime | None = None
    receipt_status: str | None = None
    contains_synthetic: bool = False


@dataclass(frozen=True)
class OperatorAction:
    """一次运营处理动作（批准 / 驳回 / 修改）。

    - ``action_id``：**客户动作**标识（同一客户动作被多次处理只计 1 次）；
    - ``candidate_id``：被处理的候选动作。
    """

    action_id: str
    candidate_id: str
    operator_id: str
    operator_action: str
    action_type: str
    channel: str
    processed_at: datetime


@dataclass(frozen=True)
class TaskBatchPeriod:
    """一个「周期」= 一个任务批次的指标汇总（④ 周期 = 任务批次）。"""

    task_id: str
    candidate_count: int
    adopted_count: int
    adoption_rate: float | None
    candidate_to_send_avg_ms: int | None
    contains_synthetic: bool


# ---------------------------------------------------------------------------
# ① AI 候选采纳率（FR-35 ① / 裁决 #21）
# ---------------------------------------------------------------------------


def adoption_rate(candidates: Sequence[CandidateMetric]) -> float | None:
    """① 未经人工修改即发出的候选数 / **候选总数**。

    分母是候选总数（包含被人工修改过与没发出的）——口径固定，不因结果好看而改分母。
    没有候选时口径**未定义**，返回 ``None``（不返回 0 冒充「采纳率 0%」）。
    """
    total = len(candidates)
    if total == 0:
        return None
    adopted = sum(1 for c in candidates if c.sent and not c.human_modified)
    return adopted / total


# ---------------------------------------------------------------------------
# ② 人均处理数（FR-35 ② / 裁决 #21）
# ---------------------------------------------------------------------------


def per_capita_processing_count(
    processed: Sequence[OperatorAction],
    active_operator_ids: Iterable[str] | None = None,
) -> float | None:
    """② 当日运营处理（批准/驳回/修改）的**客户动作数** / **活跃运营数**。

    - 分子：不同 ``action_id`` 的计数（同一条客户动作被「改后批」多次处理只计 1 次）；
    - 分母：给了 ``active_operator_ids`` 就按名单算（含当日未处理动作的运营），
      否则按「当日实际处理过动作的运营」推导；
    - 只认批准 / 驳回 / 修改三种处理动作，其他动作不计入。

    没有运营处理时口径**未定义**，返回 ``None``。
    """
    actions = [a for a in processed if a.operator_action in OPERATOR_ACTIONS]
    if not actions:
        return None
    customer_action_count = len({a.action_id for a in actions})
    if active_operator_ids is None:
        operator_count = len({a.operator_id for a in actions})
    else:
        operator_count = len(set(active_operator_ids))
    if operator_count == 0:
        return None
    return customer_action_count / operator_count


# ---------------------------------------------------------------------------
# ③ 候选→发出时长（FR-35 ③ / 裁决 #21 + #22）
# ---------------------------------------------------------------------------


def candidate_to_send_avg_ms(candidates: Sequence[CandidateMetric]) -> int | None:
    """③ 候选创建至**模拟回执**的均值（毫秒）。

    只有 ``status=success`` 的回执计入（裁决 #22）；回执时间早于创建时间的样本视为
    数据错误被剔除。没有可用回执时口径**未定义**，返回 ``None``。
    """
    intervals = [
        (c.receipt_at - c.created_at).total_seconds() * 1000
        for c in candidates
        if c.receipt_at is not None
        and c.receipt_status == SUCCESS_RECEIPT_STATUS
        and c.receipt_at >= c.created_at
    ]
    if not intervals:
        return None
    return round(sum(intervals) / len(intervals))


# ---------------------------------------------------------------------------
# ④ 周期 = 任务批次（FR-35 ④ / FR-33「周期」口径）
# ---------------------------------------------------------------------------


def task_batch_periods(candidates: Sequence[CandidateMetric]) -> tuple[TaskBatchPeriod, ...]:
    """④ 按**任务批次**切周期：每个任务批次一个周期，各自带本周期内的指标。

    未归入任务批次的候选（``task_id`` 为空）单列一档（``task_id=''``），不静默丢弃。
    """
    grouped: dict[str, list[CandidateMetric]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.task_id or "", []).append(candidate)

    return tuple(
        TaskBatchPeriod(
            task_id=task_id,
            candidate_count=len(items),
            adopted_count=sum(1 for c in items if c.sent and not c.human_modified),
            adoption_rate=adoption_rate(items),
            candidate_to_send_avg_ms=candidate_to_send_avg_ms(items),
            contains_synthetic=any(c.contains_synthetic for c in items),
        )
        for task_id, items in sorted(grouped.items())
    )


# ---------------------------------------------------------------------------
# 「同类型动作」= action_type + channel（裁决 #22）
# ---------------------------------------------------------------------------


def action_type_key(action_type: str, channel: str) -> str:
    """「同类型动作」的键 = ``action_type + channel`` 组合（裁决 #22）。"""
    return f"{action_type}{_ACTION_TYPE_KEY_SEPARATOR}{channel}"


def group_by_action_type(
    candidates: Sequence[CandidateMetric],
) -> Mapping[str, list[CandidateMetric]]:
    """按「同类型动作」分组（同一 ``action_type + channel`` 归一组）。"""
    grouped: dict[str, list[CandidateMetric]] = {}
    for candidate in candidates:
        key = action_type_key(candidate.action_type, candidate.channel)
        grouped.setdefault(key, []).append(candidate)
    return grouped
