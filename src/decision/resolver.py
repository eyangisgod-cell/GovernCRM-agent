"""T-D1 冲突消解引擎（决策层编排）。

规则 3 三键排序 + 规则 4 层级分维度的**编排层**——纯函数判定在
`src/policy_kernel/rules.py`（唯一实现点），本模块负责：

1. 调用 `rules.evaluate()` 得到各候选结论；
2. 把被抑制候选写成 `SuppressionRecord`（rule_id=CONFLICT_WINDOW_72H）；
3. 返回 `ResolutionResult`（survivors / suppressed / escalated）。

红线②：DENY 不可绕过——被抑制候选**不得**被跳过或强制放行。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.policy_kernel.kernel import Decision, RuleId
from src.policy_kernel.rules import EvaluationRequest, evaluate

__all__ = ["ResolutionResult", "SuppressedCandidate", "resolve_conflicts"]


@dataclass(frozen=True)
class SuppressedCandidate:
    """一条被抑制的候选及其留痕。"""

    candidate_id: str
    rule_id: str
    reason_detail: str


@dataclass(frozen=True)
class ResolutionResult:
    """冲突消解结果。"""

    survivors: tuple[str, ...] = ()
    suppressed: tuple[SuppressedCandidate, ...] = ()
    escalated: bool = False


def resolve_conflicts(request: EvaluationRequest) -> ResolutionResult:
    """对同客户的候选动作做冲突消解，只放行 1 条（附录D 规则 3）。

    编排逻辑：调用 `rules.evaluate()`（唯一判定实现点），把 DENY 候选
    转为 `SuppressedCandidate` 留痕；三键全平 → `escalated=True`。
    """
    outcome = evaluate(request)

    survivors: list[str] = []
    suppressed: list[SuppressedCandidate] = []
    escalated = False

    for candidate_id, result in outcome.results.items():
        if result.conclusion == Decision.ALLOW:
            survivors.append(candidate_id)
        elif result.conclusion == Decision.REQUIRE_APPROVAL:
            escalated = True
        else:
            # DENY：取 CONFLICT_WINDOW_72H 或其他命中规则作为留痕
            rule_ids = result.suppressed_rule_ids or result.hit_rule_ids
            rule_id = rule_ids[0].value if rule_ids else RuleId.CONFLICT_WINDOW_72H.value
            suppressed.append(
                SuppressedCandidate(
                    candidate_id=candidate_id,
                    rule_id=rule_id,
                    reason_detail=f"冲突消解抑制：{rule_id}",
                )
            )

    return ResolutionResult(
        survivors=tuple(survivors),
        suppressed=tuple(suppressed),
        escalated=escalated,
    )
