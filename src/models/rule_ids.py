"""rule_id 登记表（静态镜像）。

权威源只有一个：**`docs/specs/GovernCRM_附录D_冻结版.md` §4「规则 ID 登记表（唯一权威）」**。
本模块是该表的**静态镜像**，用途是给数据库 CHECK 约束提供字面量，
从结构上杜绝「自造 rule_id」。

> 策略内核的 ``RuleId`` 枚举由 **T-B1** 在 ``src/policy_kernel/kernel.py`` 定义（纯函数、
> 零依赖，不得 import 本模块——本包会连带引入 SQLAlchemy）。两侧同源于冻结版文档，
> 由契约测试（``tests/policy/test_kernel_contracts.py``）逐条比对，出现漂移即阻断合并。
"""

from __future__ import annotations

#: 附录 D §4 登记的全部 rule_id（顺序与文档一致）
REGISTERED_RULE_IDS: tuple[str, ...] = (
    "SUP_HIGH_VALUE",  # 规则 1a：抑制红线（Top 1% 高价值）→ DENY + 转人工
    "SUP_HIGH_COMPLAINT",  # 规则 1a：近 90 天投诉 ≥1 → DENY
    "SUP_HIGH_REFUND",  # 规则 1a：纠纷或退款率超阈值 → DENY
    "SUP_EXPLICIT_REFUSAL",  # 规则 1a：明示拒绝营销 → DENY
    "NO_CONSENT",  # 规则 1b：无有效营销授权 → DENY（发送前校验）
    "FREQ_GLOBAL_7D",  # 规则 2：全局频控 → DENY（延迟类）
    "CONFLICT_WINDOW_72H",  # 规则 3：冲突窗口被抑制 → DENY
    "LAYER_RESOLVE",  # 规则 4：过程性记录（不独立决定判定）
    "BENEFIT_MAX",  # 规则 5：过程性记录（不独立决定判定）
    "ESCALATE_HUMAN",  # 规则 6：升级人工 → REQUIRE_APPROVAL
    "PATH_ASSERT",  # 路径完整性断言缺失 → DENY
)


def rule_id_subset_check(column: str) -> str:
    """生成「列值必须是登记表子集」的 CHECK 表达式。

    例：``rule_id_subset_check('hit_rule_ids')``
    → ``hit_rule_ids <@ ARRAY['SUP_HIGH_VALUE', ...]::text[]``
    """
    literal = ", ".join(f"'{rule_id}'" for rule_id in REGISTERED_RULE_IDS)
    return f"{column} <@ ARRAY[{literal}]::text[]"
