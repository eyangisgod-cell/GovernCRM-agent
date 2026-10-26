"""策略内核的数据契约（决策枚举 / rule_id 枚举 / 判定结果结构）。

红线 1：本模块属于 `policy_kernel`——**纯函数、零 LLM、零框架依赖、零 ORM**。
只允许标准库；**禁止** import `src.models`（会把 SQLAlchemy 带进纯函数内核）、
禁止 import 任何供应商 SDK（`litellm` 等）。
依据：`.trellis/spec/backend/directory-structure.md`「Module Organization」。

红线 2：`Decision.DENY` 是终态取值——本模块**不提供**任何强制放行 / 绕过的入口。

本任务（T-B1）只定义**类型契约**，六条规则的判定逻辑在 T-B2。

权威依据：
- `Decision` 三值 —— 架构 v5.2 §4.3 `evaluate()` 返回 ALLOW / REQUIRE_APPROVAL / DENY。
- `RuleId` 11 项 —— 附录D §4「规则 ID 登记表（唯一权威）」，顺序 = 登记顺序。
- 判定结果字段 —— 附录D §4 尾注（`hit_rule_ids` 与 `suppressed_rule_ids` 分开记）、
  规则 2（冷却剩余秒数）、规则 3（被抑制候选数）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "Decision",
    "DecisionResult",
    "RuleId",
]


class Decision(StrEnum):
    """判定结论（架构 v5.2 §4.3）。

    恰好三个取值，**不得增删第四个**——例如不存在「强制放行」这种结论，
    红线②（DENY 对所有角色不可绕过）在契约层就没有开口。

    取值是 `str` 子类：落库（`decision.conclusion` / `chk_decision_conclusion`）
    与审计快照可直接使用 `.value`。
    """

    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class RuleId(StrEnum):
    """rule_id 登记表（附录D §4「唯一权威」，11 项，顺序与文档一致）。

    本枚举是 `src/models/rule_ids.py`（给数据库 CHECK 约束用的字面量镜像）的
    **内核侧对应物**。两侧**不得**互相 import：内核侧 import `src.models` 会把
    SQLAlchemy 带进纯函数内核。一致性由契约测试
    （`tests/policy/test_kernel_contracts.py`）用**字面量比对**保证，出现漂移即红。

    附录D §4 尾注：`LAYER_RESOLVE` / `BENEFIT_MAX` 是**过程性记录**，
    不独立产生 DENY / ALLOW 判定。
    """

    SUP_HIGH_VALUE = "SUP_HIGH_VALUE"
    SUP_HIGH_COMPLAINT = "SUP_HIGH_COMPLAINT"
    SUP_HIGH_REFUND = "SUP_HIGH_REFUND"
    SUP_EXPLICIT_REFUSAL = "SUP_EXPLICIT_REFUSAL"
    NO_CONSENT = "NO_CONSENT"
    FREQ_GLOBAL_7D = "FREQ_GLOBAL_7D"
    CONFLICT_WINDOW_72H = "CONFLICT_WINDOW_72H"
    LAYER_RESOLVE = "LAYER_RESOLVE"
    BENEFIT_MAX = "BENEFIT_MAX"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    PATH_ASSERT = "PATH_ASSERT"


@dataclass(frozen=True)
class DecisionResult:
    """单条候选动作的判定结果（不可变）。

    承载附录D §4 尾注要求的**三要素分离**：

    - `conclusion` —— 判定结论；
    - `hit_rule_ids` —— **本候选**命中的规则（多命中全记，见规则 1 求值顺序）；
    - `suppressed_rule_ids` —— **其他候选**被抑制的规则（如 `CONFLICT_WINDOW_72H`），
      **不得混入** `hit_rule_ids`；
    - `suppressed_candidates` —— 被抑制的候选数量（规则 3：只放行 1 条，其余抑制）。

    `evidence` 放**过程性记录**（`LAYER_RESOLVE` 记「层级取了哪套」、
    `BENEFIT_MAX` 记「权益选了哪张券」），不参与判定。
    冷却剩余秒数字段对应规则 2 的延迟类返回值（`suppression_record` 同名列）。
    """

    conclusion: Decision
    hit_rule_ids: tuple[RuleId, ...] = ()
    suppressed_rule_ids: tuple[RuleId, ...] = ()
    suppressed_candidates: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict[str, Any])
    cooling_remaining_seconds: int | None = None
    channel_cooling_remaining_seconds: int | None = None
    reason: str | None = None
